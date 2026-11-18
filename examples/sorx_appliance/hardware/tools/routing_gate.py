"""Compare two copper-only routing checkpoints; never authorize manufacture or promotion."""
import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from replay_copper_delta import inventory
from verification_core import digest, parse_drc, source_identity


HERE = Path(__file__).resolve()
HW = HERE.parents[1]
SECTION = "unconnected pads"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def counts(report):
    opens, other = Counter(), Counter()
    for finding in report["findings"]:
        if finding["section"] == SECTION:
            names = set(re.findall(r"\[([^\]]+)\]", finding["objects"]))
            require(len(names) == 1, f"ambiguous net in finding: {finding['objects']}")
            opens[names.pop()] += 1
        else:
            other[finding["fingerprint"]] += 1
    return opens, other


def drc_delta(before, after):
    a, b = counts(before), counts(after)
    return dict(b[0] - a[0]), list((b[1] - a[1]).elements())


def partition_index(groups):
    index = {}
    for net, islands in groups.items():
        for i, members in enumerate(islands):
            require(bool(members), "empty pad group")
            for uid in members:
                require(uid not in index, f"pad in multiple groups: {uid}")
                index[uid] = (net, i)
    require(bool(index), "no connected-pad inventory")
    return index


def compare(before, after, drcs, joins, allowed):
    a, b = (partition_index(r["groups"]) for r in (before, after))
    labels = before["labels"]
    require(bool(joins), "at least one target join required")
    require(before["fixed_geometry"] == after["fixed_geometry"], "board changed outside routed copper")
    require(labels == after["labels"] and a.keys() == b.keys() == labels.keys(), "pad inventory changed")
    for role, snap, drc in zip(("parent", "candidate"), (before, after), drcs):
        islands = {net: len(parts) - 1 for net, parts in snap["groups"].items() if len(parts) > 1}
        physical, _ = counts(drc)
        disagree = sorted(net for net in islands.keys() | physical.keys() if islands.get(net, 0) != physical[net])
        require(not disagree, f"{role} pad groups disagree with native DRC: {disagree}; inspect orphan copper or geometry")
    for net, groups in before["groups"].items():
        for group in groups:
            dest = {b[uid] for uid in group}
            require(len(dest) == 1 and next(iter(dest))[0] == net,
                    f"{net} pad group split or reassigned: " + ", ".join(
                        labels[uid] for uid in sorted(group, key=lambda u: (sum(b[x] == b[u] for x in group), labels[u]))[:4]))
    permitted = set(allowed)
    for left, right in joins:
        pair = []
        for label in (left, right):
            match = [uid for uid, name in labels.items() if name == label or uid == label]
            require(len(match) == 1, f"missing or ambiguous pad '{label}' (use its UUID if ambiguous)")
            pair.append(match[0])
        x, y = pair
        require(a[x][0] == a[y][0] and a[x] != a[y], f"{left}, {right}: no original same-net open")
        require(b[x] == b[y], f"{left}, {right}: target still open")
        permitted.add(a[x][0])
    old, new = before["copper"], after["copper"]
    touched = set()
    for uid in old.keys() | new.keys():
        p, q = old.get(uid), new.get(uid)
        if p == q:
            continue
        if p and q:
            require(p["net"] == q["net"], f"retained copper changed net: {uid}")
        for obj in (p, q):
            if obj:
                require(not obj["locked"], f"locked copper changed: {uid}")
                touched.add(obj["net"])
    require(bool(touched) and touched <= permitted, f"unexpected copper changes: {sorted(touched - permitted)}")
    require(not after["duplicate_vias"], "duplicate drill sites in candidate")
    increased, new_findings = drc_delta(*drcs)
    require(not increased, f"increased per-net opens: {increased}")
    require(not new_findings, f"new non-connectivity findings: {new_findings}")
    require(drcs[1]["counts"][SECTION] < drcs[0]["counts"][SECTION], "no net reduction in required opens")
    return sorted(touched)


def pad_islands(board):
    """Use actual filled zone subpolygons on each enabled copper layer; don't assume every GND via reaches a plane."""
    import pcbnew
    from shapely.geometry import Point, LineString
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    from ground_stitch import polygons
    layers = [l for l in board.GetEnabledLayers().Seq() if l <= pcbnew.B_Cu]
    nodes = defaultdict(list)
    labels = {}
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if not p.GetNetname():
                continue
            uid = p.m_Uuid.AsString()
            require(uid not in labels, f"duplicate pad UUID {uid}")
            labels[uid] = f"{fp.GetReference()}.{p.GetNumber()}"
            on = [l for l in layers if p.IsOnLayer(l)]
            require(len(on) == 1 or p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH,
                    f"unsupported multilayer pad {labels[uid]}")
            g = unary_union(list(polygons(p.GetEffectivePolygon())))
            require(bool(on) and not g.is_empty, f"missing pad geometry {labels[uid]}")
            nodes[p.GetNetname()].append((g, on, uid))
    for t in board.GetTracks():
        if isinstance(t, pcbnew.PCB_VIA):
            require(t.GetViaType() == pcbnew.VIATYPE_THROUGH, "unsupported via type")
            q = t.GetPosition()
            shape = Point(q.x / 1e6, q.y / 1e6).buffer(t.GetWidth() / 2e6)
            on = [l for l in layers if t.IsOnLayer(l)]
        else:
            require(not isinstance(t, pcbnew.PCB_ARC), "arc requires separate geometry review")
            a, z = t.GetStart(), t.GetEnd()
            shape = LineString([(a.x / 1e6, a.y / 1e6), (z.x / 1e6, z.y / 1e6)]).buffer(t.GetWidth() / 2e6)
            on = [t.GetLayer()]
        nodes[t.GetNetname()].append((shape, on, None))
    for zone in board.Zones():
        if zone.GetIsRuleArea():
            continue
        for layer in layers:
            if zone.IsOnLayer(layer):
                for g in polygons(zone.GetFilledPolysList(layer)):
                    nodes[zone.GetNetname()].extend((piece, [layer], None) for piece in getattr(g, "geoms", [g]))
    groups = {}
    for net, parts in nodes.items():
        if not any(uid for _, _, uid in parts):
            continue
        parent = list(range(len(parts)))
        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for layer in layers:
            ids = [i for i, (_, on, _) in enumerate(parts) if layer in on]
            if ids:
                shapes = [parts[i][0] for i in ids]
                tree = STRtree(shapes)
                for i, j in zip(*tree.query(shapes, predicate="intersects")):
                    if i < j:
                        parent[root(ids[i])] = root(ids[j])
        islands = defaultdict(list)
        for i, (_, _, uid) in enumerate(parts):
            if uid:
                islands[root(i)].append(uid)
        groups[net] = sorted(sorted(g) for g in islands.values())
    return labels, groups


def fixed_geometry(pcb):
    from sx import parse
    form = parse(pcb.read_text())
    require(form[0] == "kicad_pcb", "not a KiCad board")
    fixed = []
    for token in form[1:]:
        if token[0] in ("segment", "via", "arc"):
            continue
        if token[0] == "zone":
            token = [t for t in token if not isinstance(t, list) or t[0] not in ("filled_polygon", "fill_segments")]
        fixed.append(token)
    return source_identity(fixed)


def inspect(pcb, out):
    import pcbnew
    require(pcbnew.Version() == "7.0.11", f"unexpected KiCad {pcbnew.Version()}")
    board = pcbnew.LoadBoard(str(pcb))
    copper = inventory(board)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    require(inventory(board) == copper, "zone refill altered copper identities")
    labels, groups = pad_islands(board)
    drc = out / "drc.txt"
    require(pcbnew.WriteDRCReport(board, str(drc), pcbnew.EDA_UNITS_MILLIMETRES, True), "DRC failed")
    parse_drc(drc.read_text())
    sites = Counter(tuple(v["position_nm"]) for v in copper.values() if v["kind"] == "via")
    write_json(out / "snapshot.json", {
        "board": digest(pcb), "drc": digest(drc), "fixed_geometry": fixed_geometry(pcb),
        "groups": groups, "labels": labels, "copper": copper, "duplicate_vias": [p for p, n in sites.items() if n > 1],
    })


def bindings(paths):
    return {str(p): digest(p) if p.is_file() else None for p in paths}


def check_native_binding(src):
    """On the active board only, refuse a source/rules/library snapshot differing from the accepted queue."""
    if src != (HW / "hw/sorx.kicad_pcb").resolve():
        return []
    prefix = "hardware/"
    ledger = HW.parent / "docs/routing-ledger.json"
    project = HW / "hw"
    paths = [src, project / "sorx.kicad_pro", project / "fp-lib-table", HW / "tools/env/image-id.txt"]
    paths += list(project.glob("*.kicad_dru")) + list((project / "sorx.pretty").glob("*.kicad_mod"))
    actual = {prefix + p.relative_to(HW).as_posix(): digest(p) for p in paths}
    require(json.loads(ledger.read_text())["inputs"] == actual,
            "native PCB/rules/libraries differ from the accepted routing ledger")
    return [ledger]


def run(args):
    image = (HERE.parent / "env/image-id.txt").read_text().strip()
    require(os.environ.get("SORX_IMAGE_ID") == image, "run via hardware/tools/env/run.ps1 (image mismatch)")
    src, dst, out = args.parent.resolve(), args.candidate.resolve(), args.out.resolve()
    require(src != dst and out != src and out != dst, "use separate source, candidate and evidence paths")
    require(digest(src) == args.parent_sha256, "stale accepted parent")
    project = lambda p: p.with_suffix(".kicad_pro")
    rules = lambda p: p.with_suffix(".kicad_dru")
    require(digest(project(src)) == digest(project(dst)), "missing or different project rules")
    required = [src, dst, project(src), project(dst), rules(src), rules(dst), HW / "hw/fp-lib-table", HERE]
    required.extend(check_native_binding(src))
    required += sorted((HW / "hw/sorx.pretty").glob("*.kicad_mod")) + [
        HERE.with_name(name) for name in ("sx.py", "verification_core.py", "replay_copper_delta.py", "ground_stitch.py")]
    original = bindings(required)
    require(original[str(rules(src))] == original[str(rules(dst))], "custom rules differ")
    require(all(v is not None for k, v in original.items() if not k.endswith(".kicad_dru")), "missing checker input")
    out.mkdir(parents=True, exist_ok=False)
    snapshots, reports, evidence = [], [], []
    for label, board in (("before", src), ("after", dst)):
        result = out / label
        result.mkdir()
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            for path in (board, project(board), rules(board)):
                if path.exists():
                    copy = work / ("sorx" + path.suffix)
                    shutil.copyfile(path, copy)
                    require(digest(copy) == original[str(path)], "input changed during copy")
            shutil.copyfile(HW / "hw/fp-lib-table", work / "fp-lib-table")
            shutil.copytree(HW / "hw/sorx.pretty", work / "sorx.pretty")
            command = [sys.executable, str(HERE), "_inspect", str(work / "sorx.kicad_pcb"), str(result)]
            with (result / "worker.log").open("w") as log:
                subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
        snap = json.loads((result / "snapshot.json").read_text())
        require(snap["board"] == original[str(board)] and snap["drc"] == digest(result / "drc.txt"), "stale worker output")
        snapshots.append(snap)
        reports.append(parse_drc((result / "drc.txt").read_text()))
        evidence.append({"board_sha256": snap["board"], "drc_sha256": snap["drc"],
                         "snapshot_sha256": digest(result / "snapshot.json"), "counts": reports[-1]["counts"]})
    require(bindings(required) == original, "inputs changed during checks")
    touched = compare(*snapshots, reports, args.join, args.allow_net)
    write_json(out / "comparison.json", {"status": "PASS_CONNECTIVITY_ONLY", "image": image,
               "inputs": original, "join": args.join, "touched_nets": touched, "evidence": evidence})
    print("PASS_CONNECTIVITY_ONLY; no board promoted. Check interface/power constraints, then recheck promoted bytes.")


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "_inspect":
        inspect(Path(sys.argv[2]), Path(sys.argv[3]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parent", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--out", required=True, type=Path, help="new directory; must not exist")
    parser.add_argument("--join", nargs=2, required=True, action="append", metavar=("PAD_A", "PAD_B"))
    parser.add_argument("--allow-net", action="append", default=[], help="additional pre-reviewed detour net")
    parser.add_argument("--parent-sha256", required=True, help="accepted parent hash from the task ticket")
    try:
        run(parser.parse_args())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"CHECK FAILED: {exc}\n")


if __name__ == "__main__":
    main()

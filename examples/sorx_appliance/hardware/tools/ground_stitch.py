#!/usr/bin/env python3
"""Stitch disconnected GND copper islands using their full filled geometry.

This is a conservative placement heuristic, not a replacement for KiCad DRC.
It does not move signal routes or put vias into pads. Optional In2 supply
detours preserve power-track widths and endpoints. Output is a new
checkpoint; raw before/after DRC and exact additions are retained.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import sys
import shutil
from pathlib import Path
from shapely.geometry import Polygon, Point, LineString, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

VIA_DIAMETER = 0.6
VIA_DRILL = 0.3
COPPER_CLEARANCE = 0.20
PAD_MARGIN = 0.12
HOLE_CLEARANCE = 0.25


def chain_points(chain):
    return [(chain.CPoint(i).x / 1e6, chain.CPoint(i).y / 1e6) for i in range(chain.PointCount())]


def polygons(polyset):
    for i in range(polyset.OutlineCount()):
        outline = chain_points(polyset.Outline(i))
        holes = [chain_points(polyset.Hole(i, j)) for j in range(polyset.HoleCount(i))]
        if len(outline) < 3:
            continue
        geom = Polygon(outline, [h for h in holes if len(h) >= 3])
        if not geom.is_valid:
            geom = geom.buffer(0)  # KiCad fracture bridges become explicit polygon holes.
        if not geom.is_empty:
            yield geom


def ground_groups(board):
    import pcbnew
    layers = [pcbnew.F_Cu, pcbnew.B_Cu]
    if board.GetCopperLayerCount() >= 6:
        layers.append(pcbnew.In3_Cu)
    nodes = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() != "GND":
                continue
            on = [l for l in layers if pad.IsOnLayer(l)]
            if not on:
                continue
            shape = unary_union(list(polygons(pad.GetEffectivePolygon())))
            if shape.is_empty:
                raise ValueError(f"Missing GND pad geometry: {fp.GetReference()}.{pad.GetNumber()}")
            pos = pad.GetPosition()
            identity = {"reference": fp.GetReference(), "pad": pad.GetNumber(),
                        "x_mm": pos.x / 1e6, "y_mm": pos.y / 1e6}
            nodes.append((shape, on, pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH, identity))
    for track in board.GetTracks():
        if track.GetNetname() != "GND":
            continue
        if isinstance(track, pcbnew.PCB_VIA):
            p = track.GetPosition()
            nodes.append((Point(p.x / 1e6, p.y / 1e6).buffer(track.GetWidth() / 2e6), layers, True, None))
        elif track.GetLayer() in layers:
            if isinstance(track, pcbnew.PCB_ARC):
                raise ValueError("Ground stitching does not approximate curved tracks")
            a, b = track.GetStart(), track.GetEnd()
            shape = LineString([(a.x / 1e6, a.y / 1e6), (b.x / 1e6, b.y / 1e6)]).buffer(track.GetWidth() / 2e6)
            nodes.append((shape, [track.GetLayer()], False, None))
    for zone in board.Zones():
        if zone.GetIsRuleArea() or zone.GetNetname() != "GND":
            continue
        for layer in layers:
            if zone.IsOnLayer(layer):
                nodes.extend((g, [layer], False, None) for g in polygons(zone.GetFilledPolysList(layer)))
    parent = list(range(len(nodes)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for layer in layers:
        indices = [i for i, n in enumerate(nodes) if layer in n[1]]
        shapes = [nodes[i][0] for i in indices]
        if not shapes:
            continue
        tree = STRtree(shapes)
        for i, shape in enumerate(shapes):
            for j in tree.query(shape):
                j = int(j)
                if j > i and shape.intersects(shapes[j]):
                    parent[find(indices[i])] = find(indices[j])
    groups = {}
    for i in range(len(nodes)):
        groups.setdefault(find(i), []).append(i)
    result = []
    for members in groups.values():
        pads = [nodes[i][3] for i in members if nodes[i][3] is not None]
        if not pads or any(nodes[i][2] for i in members):
            continue
        result.append({"pads": pads, "layers": {
            layer: unary_union([nodes[i][0] for i in members if layer in nodes[i][1]])
            for layer in layers if any(layer in nodes[i][1] for i in members)}})
    return result


def blocked_centers(board, allow_supply_detours=False):
    """All physical copper/holes across layers plus no-via rule areas; no via-in-pad."""
    import pcbnew
    blocked = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            shapes = list(polygons(pad.GetEffectivePolygon()))
            margin = VIA_DIAMETER / 2 + (PAD_MARGIN if pad.GetNetname() == "GND" else COPPER_CLEARANCE)
            blocked.extend(g.buffer(margin) for g in shapes)
            drill = pad.GetDrillSize()
            if drill.x > 0:
                p = pad.GetPosition()
                # Circumscribed circle is deliberately conservative for rotated/oval holes.
                radius = max(drill.x, drill.y) / 2e6 + VIA_DRILL / 2 + HOLE_CLEARANCE
                blocked.append(Point(p.x / 1e6, p.y / 1e6).buffer(radius))
        for zone in fp.Zones():
            if zone.GetIsRuleArea() and zone.GetDoNotAllowVias():
                blocked.extend(g.buffer(VIA_DIAMETER / 2 + 0.02) for g in polygons(zone.Outline()))
    for track in board.GetTracks():
        if isinstance(track, pcbnew.PCB_VIA):
            p = track.GetPosition()
            radius = (track.GetDrill() / 1e6 + VIA_DRILL) / 2 + HOLE_CLEARANCE
            if track.GetNetname() != "GND":
                radius = max(radius, track.GetWidth() / 2e6 + VIA_DIAMETER / 2 + COPPER_CLEARANCE)
            blocked.append(Point(p.x / 1e6, p.y / 1e6).buffer(radius))
        elif track.GetNetname() != "GND":
            if allow_supply_detours and track.GetLayer() == pcbnew.In2_Cu and not track.IsLocked():
                import proj
                if proj.classify(track.GetNetname()) in ("PWR", "PWR_HI"):
                    continue
            if isinstance(track, pcbnew.PCB_ARC):
                raise ValueError("Ground stitching requires explicit arc geometry support")
            a, b = track.GetStart(), track.GetEnd()
            shape = LineString([(a.x / 1e6, a.y / 1e6), (b.x / 1e6, b.y / 1e6)])
            blocked.append(shape.buffer(track.GetWidth() / 2e6 + VIA_DIAMETER / 2 + COPPER_CLEARANCE))
    for zone in board.Zones():
        if zone.GetIsRuleArea() and zone.GetDoNotAllowVias():
            blocked.extend(g.buffer(VIA_DIAMETER / 2 + 0.02) for g in polygons(zone.Outline()))
    return unary_union(blocked)


def choose_site(copper, forbidden, inside_board):
    """Prefer full land containment, otherwise require a finite land overlap."""
    for inset, strategy in ((-(VIA_DIAMETER / 2 + 0.03), "contained"), (VIA_DIAMETER / 2 - 0.10, "overlap")):
        region = copper.buffer(inset).intersection(inside_board).difference(forbidden)
        candidates = list(region.geoms) if hasattr(region, "geoms") else [region]
        candidates = sorted((g for g in candidates if not g.is_empty and g.area > 1e-5), key=lambda g: -g.area)
        for geom in candidates:
            p = geom.representative_point()
            site = Point(round(p.x, 4), round(p.y, 4))
            if region.contains(site) and site.buffer(VIA_DIAMETER / 2).intersection(copper).area > 0.005:
                return (site.x, site.y), strategy
    return None


def detour_polyline(a, b, center, width, obstacles, inside_board):
    """Preserve endpoints/width while doglegging around a new through via.

    Only accepts complete, exactly collision-checked candidate paths. Returning
    None leaves the original route unchanged; this is deliberately not a global
    autorouter and never relaxes the board's process limits.
    """
    length = math.dist(a, b)
    radius = VIA_DIAMETER / 2 + COPPER_CLEARANCE + width / 2 + 0.08
    if length < 1e-6 or min(math.dist(a, center), math.dist(b, center)) <= radius:
        return None
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    vx, vy = -uy, ux
    along = (center[0] - a[0]) * ux + (center[1] - a[1]) * uy
    offset = (center[0] - a[0]) * vx + (center[1] - a[1]) * vy
    land = Point(center).buffer(VIA_DIAMETER / 2)
    candidates = []
    for sign in (-1, 1):
        for scale in (1.0, 1.5, 2.0, 3.0):
            r = radius * scale
            if not (along - r > 0 and along + r < length):
                continue
            lateral = offset + sign * r
            def xy(t, v):
                return (round(a[0] + t * ux + v * vx, 6), round(a[1] + t * uy + v * vy, 6))
            points = [a, xy(max(0, along - 2 * r), 0),
                      xy(along - r, lateral), xy(along + r, lateral),
                      xy(min(length, along + 2 * r), 0), b]
            points = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
            path = LineString(points)
            copper = path.buffer(width / 2)
            if (inside_board.covers(copper) and copper.distance(land) >= COPPER_CLEARANCE + 0.01
                    and (obstacles.is_empty or copper.distance(obstacles) >= COPPER_CLEARANCE + 0.01)):
                candidates.append((path.length, points))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def supply_detours(board, site, inside_board):
    """Propose In2 power-only detours. Do not mutate anything on failure."""
    import pcbnew
    import proj
    land = Point(site).buffer(VIA_DIAMETER / 2)
    targets = []
    for track in board.GetTracks():
        if isinstance(track, pcbnew.PCB_VIA) or track.GetLayer() != pcbnew.In2_Cu:
            continue
        a, b = track.GetStart(), track.GetEnd()
        shape = LineString([(a.x / 1e6, a.y / 1e6), (b.x / 1e6, b.y / 1e6)]).buffer(track.GetWidth() / 2e6)
        if shape.distance(land) >= COPPER_CLEARANCE + 0.01:
            continue
        if track.IsLocked() or isinstance(track, pcbnew.PCB_ARC) or proj.classify(track.GetNetname()) not in ("PWR", "PWR_HI"):
            return None
        targets.append(track)
    if not targets:
        return []
    proposals = []
    for target in targets:
        other = []
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if pad.GetNetname() != target.GetNetname() and pad.IsOnLayer(pcbnew.In2_Cu):
                    other.extend(polygons(pad.GetEffectivePolygon()))
                if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH and pad.GetDrillSize().x:
                    p, d = pad.GetPosition(), pad.GetDrillSize()
                    other.append(Point(p.x / 1e6, p.y / 1e6).buffer(max(d.x, d.y) / 2e6))
        proposed_by_uuid = {p["track"].m_Uuid.AsString(): p for p in proposals}
        for track in board.GetTracks():
            if track.GetNetname() == target.GetNetname():
                continue
            if track.m_Uuid.AsString() in proposed_by_uuid:
                p = proposed_by_uuid[track.m_Uuid.AsString()]
                other.append(LineString(p["points"]).buffer(p["width_mm"] / 2))
            elif isinstance(track, pcbnew.PCB_VIA):
                p = track.GetPosition()
                other.append(Point(p.x / 1e6, p.y / 1e6).buffer(track.GetWidth() / 2e6))
            elif track.GetLayer() == pcbnew.In2_Cu:
                if isinstance(track, pcbnew.PCB_ARC):
                    return None
                a, b = track.GetStart(), track.GetEnd()
                other.append(LineString([(a.x / 1e6, a.y / 1e6), (b.x / 1e6, b.y / 1e6)]).buffer(track.GetWidth() / 2e6))
        for zone in list(board.Zones()) + [z for f in board.GetFootprints() for z in f.Zones()]:
            if zone.IsOnLayer(pcbnew.In2_Cu) and zone.GetIsRuleArea() and zone.GetDoNotAllowTracks():
                other.extend(polygons(zone.Outline()))
        a, b = target.GetStart(), target.GetEnd()
        points = detour_polyline((a.x / 1e6, a.y / 1e6), (b.x / 1e6, b.y / 1e6),
                                 site, target.GetWidth() / 1e6, unary_union(other), inside_board)
        if points is None:
            return None
        proposals.append({"track": target, "points": points, "width_mm": target.GetWidth() / 1e6})
    return proposals


def commit_detours(board, proposals):
    import pcbnew
    records = []
    for proposal in proposals:
        old = proposal["track"]
        record = {"old_uuid": old.m_Uuid.AsString(), "net": old.GetNetname(),
                  "layer": board.GetLayerName(old.GetLayer()),
                  "width_mm": proposal["width_mm"], "new_points_mm": proposal["points"],
                  "new_uuids": []}
        for a, b in zip(proposal["points"], proposal["points"][1:]):
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
            track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
            track.SetWidth(old.GetWidth()); track.SetLayer(old.GetLayer()); track.SetNet(old.GetNet())
            board.Add(track); record["new_uuids"].append(track.m_Uuid.AsString())
        board.Remove(old)
        records.append(record)
    return records


def supply_route_grid(board):
    """Search GND escapes with movable In2 power reserved for later detours."""
    import pcbnew
    import proj
    from route_completion import Grid
    grid = Grid(board)
    grid.load(board)
    for net, items in grid.items.items():
        if proj.classify(net) not in ("PWR", "PWR_HI"):
            continue
        for layers, shape, hole, track in items:
            if layers == "I":
                # All supply tracks are separately checked by supply_detours;
                # a search path is never committed merely because it is found.
                grid.add(net, layers, shape, hole, trk=track, sign=-1)
    return grid


def route_group(board, grid, group, inside_board, forbidden, allow_supply_detours=False):
    """Return a checked escape route and any necessary power detours."""
    import pcbnew
    layers = {pcbnew.F_Cu: "F", pcbnew.B_Cu: "B", pcbnew.In3_Cu: "C"}
    fpmap = {fp.GetReference(): fp for fp in board.GetFootprints()}
    for identity in group["pads"]:
        pad = next(p for p in fpmap[identity["reference"]].Pads() if p.GetNumber() == identity["pad"])
        start_layers = "".join(label for layer, label in layers.items() if pad.IsOnLayer(layer))
        reject = forbidden
        for attempt in range(4):
            candidate = grid.route("GND", (identity["x_mm"], identity["y_mm"]), start_layers,
                                   via_goal=True, via_forbidden=reject)
            if candidate is None:
                break
            runs, vias = candidate
            if not vias:
                return runs, vias, []
            if len(vias) != 1:
                raise ValueError("Ground escape unexpectedly needs multiple vias")
            proposals = supply_detours(board, vias[0], inside_board) if allow_supply_detours else []
            if proposals is not None:
                return runs, vias, proposals
            # Try another physically separated site instead of repeatedly
            # proposing the same infeasible supply collision.
            reject = reject.union(Point(vias[0]).buffer(0.30))
    return None


def stitch(src, dst, report_dir, iterations=3, allow_supply_detours=False, trace_escapes=False):
    import pcbnew
    from verification_core import parse_drc
    src, dst, report_dir = Path(src).resolve(), Path(dst).resolve(), Path(report_dir).resolve()
    if src == dst:
        raise ValueError("Input and output must differ; write a new routing checkpoint")
    if dst.exists():
        raise ValueError("Output already exists; use a new checkpoint name")
    report_dir.mkdir(parents=True, exist_ok=False)
    source_inputs = {str(src): hashlib.sha256(src.read_bytes()).hexdigest()}
    for dependency in (src.with_suffix(".kicad_pro"), src.with_suffix(".kicad_dru"), Path(__file__), Path(__file__).with_name("route_completion.py"), Path(__file__).with_name("proj.py")):
        if dependency.is_file():
            source_inputs[str(dependency)] = hashlib.sha256(dependency.read_bytes()).hexdigest()
    board = pcbnew.LoadBoard(str(src))
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    before = report_dir / "drc-before.txt"
    pcbnew.WriteDRCReport(board, str(before), pcbnew.EDA_UNITS_MILLIMETRES, True)
    before_data = parse_drc(before.read_text(encoding="utf-8"))
    groups = ground_groups(board)
    initial_groups = len(groups)
    bb = board.GetBoardEdgesBoundingBox()
    inside = box(bb.GetLeft() / 1e6 + 0.6, bb.GetTop() / 1e6 + 0.6,
                 bb.GetRight() / 1e6 - 0.6, bb.GetBottom() / 1e6 - 0.6)
    additions = []
    detour_records = []
    escape_records = []
    for iteration in range(iterations):
        if not groups:
            break
        forbidden = blocked_centers(board, allow_supply_detours)
        strict_forbidden = blocked_centers(board) if allow_supply_detours else forbidden
        count = 0
        grid = supply_route_grid(board) if allow_supply_detours else None
        if grid is None and trace_escapes:
            from route_completion import Grid
            grid = Grid(board)
            grid.load(board)
        for group in groups:
            print("Planning ground group", group["pads"][0]["reference"], group["pads"][0]["pad"], flush=True)
            # Preserve already-legal sites before considering supply moves.
            masks = [strict_forbidden, forbidden] if allow_supply_detours else [forbidden]
            choice = None
            for mask in masks:
                for layer, copper in group["layers"].items():
                    site_result = choose_site(copper, mask, inside)
                    if site_result is None:
                        continue
                    proposals = supply_detours(board, site_result[0], inside) if allow_supply_detours else []
                    if proposals is not None:
                        choice = (layer, site_result, proposals)
                        break
                if choice is not None:
                    break
            if choice is None:
                if grid is not None:
                    escaped = route_group(board, grid, group, inside, forbidden, allow_supply_detours)
                    if escaped is not None:
                        from route_completion import commit
                        runs, vias, proposals = escaped
                        detour_records.extend(commit_detours(board, proposals))
                        commit(board, grid, "GND", board.GetNetsByName()["GND"], runs, vias)
                        escape_records.append({"target_pads": group["pads"], "runs": runs, "vias_mm": vias})
                        for xy in vias:
                            forbidden = forbidden.union(Point(xy).buffer(VIA_DRILL + HOLE_CLEARANCE))
                            strict_forbidden = strict_forbidden.union(Point(xy).buffer(VIA_DRILL + HOLE_CLEARANCE))
                        count += 1
                continue
            layer, (site, strategy), proposals = choice
            detour_records.extend(commit_detours(board, proposals))
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(site[0]), pcbnew.FromMM(site[1])))
            via.SetWidth(pcbnew.FromMM(VIA_DIAMETER)); via.SetDrill(pcbnew.FromMM(VIA_DRILL))
            via.SetViaType(pcbnew.VIATYPE_THROUGH); via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetNet(board.GetNetsByName()["GND"]); board.Add(via)
            forbidden = forbidden.union(Point(site).buffer(VIA_DRILL + HOLE_CLEARANCE))
            strict_forbidden = strict_forbidden.union(Point(site).buffer(VIA_DRILL + HOLE_CLEARANCE))
            if grid is not None:
                from route_completion import COPPER
                grid.add("GND", COPPER, Point(site).buffer(VIA_DIAMETER / 2))
                grid.block_drill(site[0], site[1], VIA_DRILL)
            additions.append({"position_mm": site, "layer_connected": board.GetLayerName(layer),
                              "strategy": strategy, "target_pads": group["pads"], "uuid": via.m_Uuid.AsString()})
            count += 1
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        groups = ground_groups(board)
        print(f"Ground stitching pass {iteration + 1}: added {count}, groups remaining {len(groups)}", flush=True)
        if count == 0:
            break
    dst.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(dst), board)
    if src.with_suffix(".kicad_pro").is_file():
        shutil.copy2(src.with_suffix(".kicad_pro"), dst.with_suffix(".kicad_pro"))
    after = report_dir / "drc-after.txt"
    pcbnew.WriteDRCReport(board, str(after), pcbnew.EDA_UNITS_MILLIMETRES, True)
    after_data = parse_drc(after.read_text(encoding="utf-8"))
    previous = {f["fingerprint"] for f in before_data["findings"] if f["section"] != "unconnected pads"}
    added_findings = [f for f in after_data["findings"] if f["section"] != "unconnected pads" and f["fingerprint"] not in previous]
    native_ground_opens = [finding for finding in after_data["findings"] if finding["section"] == "unconnected pads" and "[GND]" in finding["objects"]]
    sources_unchanged = all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in source_inputs.items())
    result = {"schema_version": 1, "source_inputs_sha256": source_inputs,
              "sources_unchanged": sources_unchanged,
              "tool_environment": {"python": sys.version, "kicad": pcbnew.Version(),
                                   "container_image": os.environ.get("SORX_IMAGE_ID"), "command": sys.argv},
              "input": str(src), "input_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
              "output": str(dst), "output_sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
              "initial_ground_groups": initial_groups, "remaining_ground_groups": len(groups), "native_ground_open_items": len(native_ground_opens),
              "supply_detours": detour_records,
              "ground_escape_routes": escape_records,
              "vias_added": additions, "unresolved_pads": [g["pads"] for g in groups],
              "before_counts": before_data["counts"], "after_counts": after_data["counts"],
              "new_drc_findings": added_findings, "fabrication_approval": False}
    (report_dir / "ground-stitch.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("initial_ground_groups", "remaining_ground_groups", "before_counts", "after_counts")}, indent=2))
    print("New non-connectivity DRC findings:", len(added_findings))
    return 2 if added_findings or not sources_unchanged else 1 if groups or native_ground_opens else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--detour-supply", action="store_true", help="Permit collision-checked In2 power detours, retaining widths and endpoints")
    parser.add_argument("--trace-escapes", action="store_true", help="Route GND escape traces without moving or removing any other net")
    args = parser.parse_args()
    return stitch(args.input, args.output, args.report_dir, args.iterations, args.detour_supply, args.trace_escapes)


if __name__ == "__main__":
    raise SystemExit(main())

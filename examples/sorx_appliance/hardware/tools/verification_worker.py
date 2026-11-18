"""KiCad-backed checks. Invoked only in a disposable source copy by verify.py."""
import argparse
import hashlib
import json
from pathlib import Path


def check_board(pcb, drc, result):
    import pcbnew
    from design import DESIGN
    from gen_sch import resolve
    board = pcbnew.LoadBoard(str(pcb))
    footprints = {f.GetReference(): f for f in board.GetFootprints()}
    expected_refs = {p.ref for p in DESIGN.parts.values() if p.fp}
    issues = []
    for ref in sorted(set(footprints) - expected_refs):
        issues.append(f"Unexpected PCB footprint {ref}")
    pads = 0
    for part in DESIGN.parts.values():
        if not part.fp:
            continue
        fp = footprints.get(part.ref)
        if fp is None:
            issues.append(f"Missing footprint {part.ref}")
            continue
        identity = fp.GetFPID()
        actual_footprint = str(identity.GetLibNickname()) + ":" + str(identity.GetLibItemName())
        if actual_footprint != part.fp:
            issues.append(f"{part.ref}: wrong footprint {actual_footprint!r} != {part.fp!r}")
        if fp.GetValue() != part.value:
            issues.append(f"{part.ref}: value {fp.GetValue()!r} != {part.value!r}")
        expected = resolve(part)
        numbers = {p.GetNumber() for p in fp.Pads()}
        for number, net in expected.items():
            if net and number not in numbers:
                issues.append(f"{part.ref}.{number}: required pad absent ({net})")
        for pad in fp.Pads():
            pads += 1
            net = expected.get(pad.GetNumber()) or ""
            if pad.GetNetname() != net:
                issues.append(f"{part.ref}.{pad.GetNumber()}: {pad.GetNetname()} != {net}")
    sites = {}
    vias = tracks = 0
    for item in board.GetTracks():
        if isinstance(item, pcbnew.PCB_VIA):
            vias += 1
            p = item.GetPosition()
            sites.setdefault((p.x, p.y), []).append(item.GetNetname())
        else:
            tracks += 1
    duplicates = [{"x_mm": x / 1e6, "y_mm": y / 1e6, "nets": nets}
                  for (x, y), nets in sites.items() if len(nets) > 1]
    pcbnew.WriteDRCReport(board, str(drc), pcbnew.EDA_UNITS_MILLIMETRES, True)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    refilled = drc.with_name("drc-refilled.txt")
    pcbnew.WriteDRCReport(board, str(refilled), pcbnew.EDA_UNITS_MILLIMETRES, True)
    data = {"footprints": len(footprints), "pads": pads, "track_segments": tracks,
            "vias": vias, "model_mismatches": issues, "duplicate_via_sites": duplicates}
    result.write_text(json.dumps(data, indent=2), encoding="utf-8")


def check_netlist(netlist, result):
    from sx import parse, find, first
    from gen_sch import netmap
    tree = parse(netlist.read_text(encoding="utf-8"))
    actual = {}
    for net in find(first(tree, "nets"), "net"):
        name = first(net, "name")[1].lstrip("/")
        actual[name] = {(first(node, "ref")[1], first(node, "pin")[1])
                        for node in find(net, "node")}
    model = netmap()
    mismatches = []
    for name, nodes in model.items():
        if nodes != actual.get(name):
            mismatches.append({"net": name, "model_only": sorted(nodes - actual.get(name, set())),
                               "schematic_only": sorted(actual.get(name, set()) - nodes)})
    extra = [name for name, nodes in actual.items() if name not in model and len(nodes) > 1]
    result.write_text(json.dumps({"model_nets": len(model), "mismatches": mismatches,
                                 "extra_multi_node_nets": extra}, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["board", "netlist"])
    parser.add_argument("input", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("--drc", type=Path)
    args = parser.parse_args()
    if args.operation == "board":
        check_board(args.input, args.drc, args.result)
    else:
        check_netlist(args.input, args.result)


if __name__ == "__main__":
    main()

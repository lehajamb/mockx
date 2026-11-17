#!/usr/bin/env python3
"""Make one explicit GND escape, rerouting only ordinary 0.15 mm signal segments.

Always writes a separate candidate and retains native before/after DRC. It never
removes vias, power, differential or single-ended timing routes. A rejected
candidate remains evidence only and must not be promoted.
"""
import argparse, hashlib, json, os, sys
from pathlib import Path
import pcbnew
from shapely.geometry import Point, LineString
from ground_stitch import ground_groups
from route_completion import Grid, LAY, W_TRK, VIA_D, VIA_DR, commit, route_policy
from verification_core import parse_drc

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def run(args):
    src, dst, report = args.input.resolve(), args.output.resolve(), args.report_dir.resolve()
    if src == dst or dst.exists():
        raise ValueError("Use a new output checkpoint")
    if not src.with_suffix(".kicad_pro").is_file():
        raise ValueError("Source project rules are required")
    dependencies=[src,src.with_suffix(".kicad_pro"),Path(__file__)] + [Path(__file__).with_name(n) for n in ("route_completion.py","ground_stitch.py","proj.py")]
    source_hashes={str(p):sha(p) for p in dependencies}
    report.mkdir(parents=True, exist_ok=False)
    board = pcbnew.LoadBoard(str(src))
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.WriteDRCReport(board, str(report/"before.txt"), pcbnew.EDA_UNITS_MILLIMETRES, True)
    before = parse_drc((report/"before.txt").read_text())
    fp = next(f for f in board.GetFootprints() if f.GetReference() == args.reference)
    pad = next(p for p in fp.Pads() if p.GetNumber() == args.pad)
    if pad.GetNetname() != "GND" or not pad.IsOnLayer(pcbnew.F_Cu):
        raise ValueError("Explicit target must be a front GND pad")
    was_open = any(any(p["reference"] == args.reference and p["pad"] == args.pad for p in g["pads"]) for g in ground_groups(board))
    if not was_open:
        raise ValueError("Target is not an open GND group")
    pos = pad.GetPosition()
    start, site = (pos.x/1e6,pos.y/1e6), (args.x,args.y)
    wire = LineString([start,site]).buffer(W_TRK/2)
    land = Point(site).buffer(VIA_D/2)
    removed = []
    for track in board.GetTracks():
        if track.GetNetname() == "GND":
            continue
        if isinstance(track, pcbnew.PCB_VIA):
            p=track.GetPosition();shape=Point(p.x/1e6,p.y/1e6).buffer(track.GetWidth()/2e6)
            collision = shape.distance(land)<.20 or shape.distance(wire)<.20
        else:
            if isinstance(track,pcbnew.PCB_ARC):
                raise ValueError("Arc routing is unsupported")
            a,b=track.GetStart(),track.GetEnd()
            shape=LineString([(a.x/1e6,a.y/1e6),(b.x/1e6,b.y/1e6)]).buffer(track.GetWidth()/2e6)
            collision=shape.distance(land)<.20 or (track.GetLayer()==pcbnew.F_Cu and shape.distance(wire)<.20)
        if not collision:
            continue
        allowed, reason=route_policy(track.GetNetname())
        if (not allowed or track.IsLocked() or isinstance(track,pcbnew.PCB_VIA)
                or abs(track.GetWidth()/1e6-W_TRK)>1e-6 or track.GetLayer() not in LAY.values()):
            raise ValueError(f"Protected collision: {track.GetNetname()} {track.m_Uuid.AsString()} ({reason})")
        a,b=track.GetStart(),track.GetEnd()
        removed.append({"object":track,"uuid":track.m_Uuid.AsString(),"net":track.GetNetname(),
                        "a":(a.x/1e6,a.y/1e6),"b":(b.x/1e6,b.y/1e6),
                        "layer":next(k for k,v in LAY.items() if v==track.GetLayer()),"width_mm":track.GetWidth()/1e6})
    if len(removed)>8:
        raise ValueError("Refusing a broad rip-up: more than eight ordinary segments")
    for old in removed:
        board.Remove(old["object"])
    grid=Grid(board);grid.load(board)
    commit(board,grid,"GND",board.GetNetsByName()["GND"],[("F",[start,site])],[site])
    reroutes=[]
    for old in removed:
        path=grid.route(old["net"],old["a"],old["layer"],old["b"],old["layer"])
        if path is None:
            raise RuntimeError(f"Cannot reconnect {old['net']} segment {old['uuid']}; input remains unchanged")
        commit(board,grid,old["net"],board.GetNetsByName()[old["net"]],*path)
        reroutes.append({"old_uuid":old["uuid"],"net":old["net"],"width_mm":old["width_mm"],
                         "old_endpoints":[old["a"],old["b"]],"new_runs":path[0],"new_vias":path[1]})
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.WriteDRCReport(board,str(report/"after.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True)
    after=parse_drc((report/"after.txt").read_text())
    prior={f["fingerprint"] for f in before["findings"] if f["section"]!="unconnected pads"}
    new=[f for f in after["findings"] if f["section"]!="unconnected pads" and f["fingerprint"] not in prior]
    affected={r["net"] for r in reroutes}
    signal_opens=[f for f in after["findings"] if f["section"]=="unconnected pads" and any("["+net+"]" in f["objects"] for net in affected)]
    still_open=any(any(p["reference"]==args.reference and p["pad"]==args.pad for p in g["pads"]) for g in ground_groups(board))
    pcbnew.SaveBoard(str(dst),board)
    import shutil
    for suffix in (".kicad_pro",".kicad_dru"):
        if src.with_suffix(suffix).is_file():
            shutil.copy2(src.with_suffix(suffix),dst.with_suffix(suffix))
    sources_unchanged=all(Path(p).is_file() and sha(p)==digest for p,digest in source_hashes.items())
    accepted=sources_unchanged and not new and not signal_opens and not still_open and after["counts"]["unconnected pads"]<before["counts"]["unconnected pads"]
    result={"schema_version":1,"source_inputs_sha256":source_hashes,"sources_unchanged":sources_unchanged,"input":str(src),"input_sha256":sha(src),"output":str(dst),"output_sha256":sha(dst),
            "tool_sha256":sha(__file__),"environment":{"python":sys.version,"kicad":pcbnew.Version(),"image":os.environ.get("SORX_IMAGE_ID")},
            "target":{"reference":args.reference,"pad":args.pad,"start_mm":start,"via_mm":site},
            "before_counts":before["counts"],"after_counts":after["counts"],"reroutes":reroutes,
            "new_non_connectivity_findings":new,"remaining_affected_signal_opens":signal_opens,
            "target_still_open":still_open,"accepted_for_further_development":accepted,"fabrication_approval":False}
    (report/"result.json").write_text(json.dumps(result,indent=2))
    print(json.dumps({k:result[k] for k in ("before_counts","after_counts","target_still_open","accepted_for_further_development")},indent=2))
    return 0 if accepted else 2

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("input",type=Path);p.add_argument("output",type=Path)
    p.add_argument("--report-dir",type=Path,required=True)
    p.add_argument("--reference",required=True);p.add_argument("--pad",required=True)
    p.add_argument("--x",type=float,required=True);p.add_argument("--y",type=float,required=True)
    return run(p.parse_args())

if __name__=="__main__":
    raise SystemExit(main())

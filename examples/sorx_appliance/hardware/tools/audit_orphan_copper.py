#!/usr/bin/env python3
"""Audit/remove ordinary-signal copper islands containing no component pad.

Power, GND, differential and timing nets are excluded. This removes unused
copper, not required endpoints. Pad connectivity groups must remain identical.
"""
import argparse,hashlib,json,shutil,sys
from pathlib import Path
import pcbnew
from shapely.geometry import Point,LineString
from shapely.ops import unary_union
from shapely.strtree import STRtree
from ground_stitch import polygons
from route_completion import route_policy
from verification_core import parse_drc

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def audit(board):
    by_net={}
    zoned={z.GetNetname() for z in board.Zones() if not z.GetIsRuleArea()}
    def eligible(net): return bool(net) and net not in zoned and route_policy(net)[0]
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            net=pad.GetNetname()
            if not eligible(net):continue
            shape=unary_union(list(polygons(pad.GetEffectivePolygon())))
            if shape.is_empty:raise ValueError("Missing pad geometry")
            layers=[l for l in board.GetEnabledLayers().Seq() if l<=31 and pad.IsOnLayer(l)]
            by_net.setdefault(net,[]).append((shape,layers,{"kind":"pad","reference":fp.GetReference(),"pad":pad.GetNumber()}))
    for t in board.GetTracks():
        net=t.GetNetname()
        if not eligible(net):continue
        if isinstance(t,pcbnew.PCB_VIA):
            p=t.GetPosition();shape=Point(p.x/1e6,p.y/1e6).buffer(t.GetWidth()/2e6)
            layers=[l for l in board.GetEnabledLayers().Seq() if l<=31 and t.IsOnLayer(l)]
            record={"kind":"via","uuid":t.m_Uuid.AsString(),"position_mm":[p.x/1e6,p.y/1e6],"locked":t.IsLocked()}
        else:
            if isinstance(t,pcbnew.PCB_ARC):raise ValueError("Arc geometry is unsupported")
            a,b=t.GetStart(),t.GetEnd()
            shape=LineString([(a.x/1e6,a.y/1e6),(b.x/1e6,b.y/1e6)]).buffer(t.GetWidth()/2e6)
            layers=[t.GetLayer()]
            record={"kind":"track","uuid":t.m_Uuid.AsString(),"start_mm":[a.x/1e6,a.y/1e6],"end_mm":[b.x/1e6,b.y/1e6],
                    "width_mm":t.GetWidth()/1e6,"layer":board.GetLayerName(t.GetLayer()),"locked":t.IsLocked()}
        by_net.setdefault(net,[]).append((shape,layers,record))
    orphaned=[];pad_groups={}
    for net,nodes in by_net.items():
        parent=list(range(len(nodes)))
        def find(i):
            while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
            return i
        for layer in sorted({l for _,layers,_ in nodes for l in layers}):
            ids=[i for i,n in enumerate(nodes) if layer in n[1]]
            shapes=[nodes[i][0] for i in ids];tree=STRtree(shapes)
            for i,g in enumerate(shapes):
                for j in tree.query(g.buffer(1e-7)):
                    j=int(j)
                    if j>i and g.distance(shapes[j])<=1e-7:parent[find(ids[i])]=find(ids[j])
        groups={}
        for i in range(len(nodes)):groups.setdefault(find(i),[]).append(nodes[i][2])
        pad_groups[net]=[]
        for members in groups.values():
            pads=sorted((m["reference"],m["pad"]) for m in members if m["kind"]=="pad")
            if pads:pad_groups[net].append(pads)
            elif not any(m["locked"] for m in members):
                orphaned.append({"net":net,"objects":members})
        pad_groups[net].sort()
    return orphaned,pad_groups

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input",type=Path);ap.add_argument("--report-dir",type=Path,required=True)
    ap.add_argument("--candidate",type=Path)
    args=ap.parse_args()
    source=args.input.resolve();report=args.report_dir.resolve()
    report.mkdir(parents=True,exist_ok=False)
    deps=[source,source.with_suffix(".kicad_pro"),Path(__file__),Path(__file__).with_name("route_completion.py")]
    hashes={str(p):sha(p) for p in deps}
    board=pcbnew.LoadBoard(str(source))
    orphans,pad_groups=audit(board)
    result={"input":str(source),"source_inputs_sha256":hashes,"orphan_islands":orphans,
            "pad_connectivity_groups":pad_groups,"fabrication_approval":False}
    if args.candidate:
        target=args.candidate.resolve()
        if target==source or target.exists():raise ValueError("Use a fresh candidate path")
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        pcbnew.WriteDRCReport(board,str(report/"before.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True)
        before=parse_drc((report/"before.txt").read_text())
        remove={o["uuid"] for island in orphans for o in island["objects"]}
        for item in list(board.GetTracks()):
            if item.m_Uuid.AsString() in remove:board.Remove(item)
        remaining,groups_after=audit(board)
        if pad_groups!=groups_after:raise RuntimeError("Pad connectivity changed; no candidate accepted")
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        pcbnew.WriteDRCReport(board,str(report/"after.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True)
        after=parse_drc((report/"after.txt").read_text())
        previous={f["fingerprint"] for f in before["findings"] if f["section"]!="unconnected pads"}
        new=[f for f in after["findings"] if f["section"]!="unconnected pads" and f["fingerprint"] not in previous]
        unchanged=all(sha(p)==h for p,h in hashes.items())
        pcbnew.SaveBoard(str(target),board)
        for suffix in (".kicad_pro",".kicad_dru"):
            if source.with_suffix(suffix).is_file():shutil.copy2(source.with_suffix(suffix),target.with_suffix(suffix))
        result.update({"output":str(target),"output_sha256":sha(target),"before_counts":before["counts"],"after_counts":after["counts"],
                       "removed_objects":len(remove),"pad_connectivity_unchanged":True,"sources_unchanged":unchanged,
                       "new_non_connectivity_findings":new,"accepted_for_further_development":not new and unchanged})
    (report/"result.json").write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ("orphan_islands","pad_connectivity_groups","source_inputs_sha256","new_non_connectivity_findings")},indent=2))
    print("Orphan islands:",len(orphans),"objects:",sum(len(i["objects"]) for i in orphans))
    return 2 if args.candidate and not result["accepted_for_further_development"] else 0

if __name__=="__main__":raise SystemExit(main())

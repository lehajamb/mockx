#!/usr/bin/env python3
"""Trim proved ordinary-net stubs without changing any actual pad connectivity.

Every net with a required open, power/analog/timing net, locked object and
zoned net is excluded. Native before/after DRC and exact object evidence
are mandatory; a warning alone never authorizes deleting copper.
"""
import argparse,hashlib,json,os,re,shutil,sys
from pathlib import Path
from shapely.geometry import Point,LineString
from shapely.ops import unary_union
from shapely.strtree import STRtree

def pad_groups(nodes):
 parent=list(range(len(nodes)))
 def find(i):
  while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
  return i
 for layer in {l for n in nodes for l in n["layers"]}:
  ids=[i for i,n in enumerate(nodes) if layer in n["layers"]];shapes=[nodes[i]["shape"] for i in ids];tree=STRtree(shapes)
  for i,shape in enumerate(shapes):
   for j in tree.query(shape.buffer(1e-7)):
    j=int(j)
    if j>i and shape.distance(shapes[j])<=1e-7:parent[find(ids[i])]=find(ids[j])
 groups={}
 for i,n in enumerate(nodes):
  if n["kind"]=="pad":groups.setdefault(find(i),[]).append(n["id"])
 return sorted(sorted(pads) for pads in groups.values())

def plan_trim(nodes,has_required_open=False):
 if has_required_open:return []
 baseline=pad_groups(nodes);removed=[]
 while True:
  accepted=None
  for node in nodes:
   if node["kind"]=="pad" or node.get("locked") or node["id"] in removed:continue
   others=[n for n in nodes if n["id"]!=node["id"] and n["id"] not in removed]
   if node["kind"]=="via":
    touches={l for n in others for l in set(node["layers"])&set(n["layers"]) if n["shape"].distance(node["shape"])<=1e-7}
    candidate=len(touches)<2
   else:
    candidate=any(not any(set(node["layers"])&set(n["layers"]) and n["shape"].distance(Point(xy))<=1e-7 for n in others) for xy in node["ends"])
   if candidate and pad_groups(others)==baseline:accepted=node["id"];break
  if accepted is None:break
  removed.append(accepted)
 return removed

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def main():
 import pcbnew
 from ground_stitch import polygons
 from route_completion import route_policy
 from verification_core import parse_drc
 from audit_orphan_copper import audit
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("input",type=Path);ap.add_argument("output",type=Path);ap.add_argument("--report-dir",type=Path,required=True);a=ap.parse_args()
 src=a.input.resolve();dst=a.output.resolve();report=a.report_dir.resolve()
 if dst==src or dst.exists() or report.exists():raise ValueError("Use fresh candidate/report paths")
 deps=[src,src.with_suffix(".kicad_pro"),Path(__file__)]+[Path(__file__).with_name(n) for n in ("route_completion.py","proj.py","audit_orphan_copper.py")]
 bound={str(p):sha(p) for p in deps};report.mkdir(parents=True);board=pcbnew.LoadBoard(str(src));pcbnew.ZONE_FILLER(board).Fill(board.Zones())
 pcbnew.WriteDRCReport(board,str(report/"before.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True);before=parse_drc((report/"before.txt").read_text());_,groups_before=audit(board)
 def nets_in(f):return set(re.findall(r"\[([^\]]+)\]",f["objects"]))
 open_nets=set().union(*(nets_in(f) for f in before["findings"] if f["section"]=="unconnected pads"))
 warnings=[f for f in before["findings"] if f["category"] in ("track_dangling","via_dangling")];targets=set().union(*(nets_in(f) for f in warnings));zoned={z.GetNetname() for z in board.Zones() if not z.GetIsRuleArea()}
 decisions={};nodes={};actual={}
 for net in sorted(targets):
  allowed,reason=route_policy(net)
  if net in open_nets:allowed=False;reason="Required open on this net: no deletion is permitted"
  if net in zoned:allowed=False;reason="Zone connectivity requires separate review"
  decisions[net]={"eligible":allowed,"reason":reason,"removed":[]}
  if allowed:nodes[net]=[]
 for fp in board.GetFootprints():
  for p in fp.Pads():
   net=p.GetNetname()
   if net in nodes:
    nodes[net].append({"id":fp.GetReference()+"."+p.GetNumber()+":"+p.m_Uuid.AsString(),"kind":"pad","shape":unary_union(list(polygons(p.GetEffectivePolygon()))),"layers":[l for l in board.GetEnabledLayers().Seq() if l<=31 and p.IsOnLayer(l)]})
 for t in board.GetTracks():
  net=t.GetNetname()
  if net not in nodes:continue
  uid=t.m_Uuid.AsString();actual[uid]=t
  if isinstance(t,pcbnew.PCB_VIA):
   q=t.GetPosition();shape=Point(q.x/1e6,q.y/1e6).buffer(t.GetWidth()/2e6);kind="via";ends=[]
  else:
   if isinstance(t,pcbnew.PCB_ARC):raise ValueError("Arc trimming requires explicit review")
   x,y=t.GetStart(),t.GetEnd();ends=[(x.x/1e6,x.y/1e6),(y.x/1e6,y.y/1e6)];shape=LineString(ends).buffer(t.GetWidth()/2e6);kind="track"
  nodes[net].append({"id":uid,"kind":kind,"shape":shape,"layers":[l for l in board.GetEnabledLayers().Seq() if l<=31 and t.IsOnLayer(l)],"ends":ends,"locked":t.IsLocked(),"width_mm":t.GetWidth()/1e6})
 removed=[]
 for net,items in nodes.items():
  kill=plan_trim(items)
  for uid in kill:
   item=next(n for n in items if n["id"]==uid);decisions[net]["removed"].append({k:v for k,v in item.items() if k!="shape"});removed.append(actual[uid])
 for t in removed:board.Remove(t)
 # Retain removed wrappers through all native work: KiCad7 ownership requires it.
 _,groups_after=audit(board);equal={k:v for k,v in groups_before.items() if v}=={k:v for k,v in groups_after.items() if v}
 if not equal:raise ValueError("Actual ordinary pad connectivity changed; candidate refused")
 pcbnew.ZONE_FILLER(board).Fill(board.Zones());pcbnew.WriteDRCReport(board,str(report/"after.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True);after=parse_drc((report/"after.txt").read_text());old={f["fingerprint"] for f in before["findings"] if f["section"]!="unconnected pads"};new=[f for f in after["findings"] if f["section"]!="unconnected pads" and f["fingerprint"] not in old]
 unchanged=all(Path(p).is_file() and sha(p)==h for p,h in bound.items());accepted=unchanged and not new and equal and after["counts"]["unconnected pads"]==before["counts"]["unconnected pads"]
 pcbnew.SaveBoard(str(dst),board)
 for ext in (".kicad_pro",".kicad_dru"):
  if src.with_suffix(ext).is_file():shutil.copy2(src.with_suffix(ext),dst.with_suffix(ext))
 result={"schema_version":1,"source_hashes":bound,"sources_unchanged":unchanged,"source":str(src),"output":str(dst),"output_sha256":sha(dst),"before_counts":before["counts"],"after_counts":after["counts"],"pad_groups_before":groups_before,"pad_groups_after":groups_after,"pad_connectivity_unchanged":equal,"decisions":decisions,"new_non_connectivity_findings":new,"accepted_for_further_development":accepted,"fabrication_approval":False,"environment":{"image":os.environ.get("SORX_IMAGE_ID"),"KICAD7_FOOTPRINT_DIR":os.environ.get("KICAD7_FOOTPRINT_DIR"),"kicad":pcbnew.Version(),"command":sys.argv}}
 (report/"result.json").write_text(json.dumps(result,indent=2)+"\n");print(json.dumps({k:result[k] for k in ("before_counts","after_counts","accepted_for_further_development")},indent=2));print("Objects removed",len(removed),flush=True);return 0 if accepted else 2

if __name__=="__main__":raise SystemExit(main())

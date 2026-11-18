#!/usr/bin/env python3
"""Replay exact reviewed copper edits onto a separate candidate; fail conflicts.

A replay is accepted for further development only after fresh native DRC,
source-hash stability, and no additional opens on any net.
"""
import argparse, hashlib, json, os, shutil, sys
from collections import Counter
from pathlib import Path
from verification_core import parse_drc

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def delta(base, changed):
 removed={uid:data for uid,data in base.items() if changed.get(uid)!=data}
 added={uid:data for uid,data in changed.items() if base.get(uid)!=data}
 return removed,added

def check_conflicts(current,removed,added):
 conflicts=["Removed or changed source copper: "+uid for uid,record in removed.items() if current.get(uid)!=record]
 conflicts += ["Added UUID already exists: "+uid for uid in added if uid in current and uid not in removed]
 if conflicts:raise ValueError("; ".join(conflicts))

def inventory(board):
 import pcbnew
 result={}
 for t in board.GetTracks():
  data={"net":t.GetNetname(),"width_nm":t.GetWidth(),"locked":t.IsLocked()}
  if isinstance(t,pcbnew.PCB_VIA):
   if t.GetViaType()!=pcbnew.VIATYPE_THROUGH:raise ValueError("Only through vias are supported")
   p=t.GetPosition();data.update(kind="via",position_nm=[p.x,p.y],drill_nm=t.GetDrill())
  elif isinstance(t,pcbnew.PCB_ARC):raise ValueError("Arc delta requires explicit geometry support")
  else:
   a,z=t.GetStart(),t.GetEnd();data.update(kind="track",start_nm=[a.x,a.y],end_nm=[z.x,z.y],layer=t.GetLayer())
  result[t.m_Uuid.AsString()]=data
 return result

def footprint_inventory(board):
 result={}
 for fp in board.GetFootprints():
  pads=[]
  for p in fp.Pads():
   q=p.GetPosition();s=p.GetSize();d=p.GetDrillSize()
   pads.append([p.m_Uuid.AsString(),p.GetNumber(),p.GetNetname(),q.x,q.y,s.x,s.y,d.x,d.y,p.GetShape(),p.GetAttribute(),list(p.GetLayerSet().Seq())])
  q=fp.GetPosition();i=fp.GetFPID()
  result[fp.GetReference()]={"uuid":fp.m_Uuid.AsString(),"value":fp.GetValue(),"library":str(i.GetLibNickname())+":"+str(i.GetLibItemName()),"position_nm":[q.x,q.y],"angle":fp.GetOrientationDegrees(),"layer":fp.GetLayer(),"pads":sorted(pads)}
 return result

def open_counts(report):
 import re
 counts=Counter()
 for finding in report["findings"]:
  if finding["section"]!="unconnected pads":continue
  nets=set(re.findall(r"\[([^\]]+)\]",finding["objects"]))
  for net in nets:counts[net]+=1
 return counts

def main():
 import pcbnew
 ap=argparse.ArgumentParser(description=__doc__)
 for name in ("base","proof","current","output"):ap.add_argument(name,type=Path)
 ap.add_argument("--report-dir",type=Path,required=True);a=ap.parse_args()
 paths=[a.base.resolve(),a.proof.resolve(),a.current.resolve()];dst=a.output.resolve();report=a.report_dir.resolve()
 if dst in paths or dst.exists() or report.exists():raise ValueError("Use new output/report paths")
 deps=paths+[a.current.resolve().with_suffix(".kicad_pro"),Path(__file__)]
 hashes={str(p):sha(p) for p in deps};report.mkdir(parents=True)
 base,proof,current=[pcbnew.LoadBoard(str(p)) for p in paths]
 if footprint_inventory(base)!=footprint_inventory(proof):raise ValueError("Proof changed footprint/pad geometry or connectivity; copper-only replay refused")
 if base.GetCopperLayerCount()!=proof.GetCopperLayerCount() or current.GetCopperLayerCount()!=base.GetCopperLayerCount():raise ValueError("Layer stack count changed")
 removed,added=delta(inventory(base),inventory(proof));current_before=inventory(current);check_conflicts(current_before,removed,added)
 if not added and not removed:raise ValueError("No copper edits to replay")
 pcbnew.ZONE_FILLER(current).Fill(current.Zones());pcbnew.WriteDRCReport(current,str(report/"before.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True)
 before=parse_drc((report/"before.txt").read_text())
 for t in list(current.GetTracks()):
  if t.m_Uuid.AsString() in removed:current.Remove(t)
 nets=current.GetNetsByName()
 for uid,r in added.items():
  if r["net"] not in nets:raise ValueError("Missing current net "+r["net"])
  if r["kind"]=="via":
   t=pcbnew.PCB_VIA(current);t.SetPosition(pcbnew.VECTOR2I(*r["position_nm"]));t.SetDrill(r["drill_nm"]);t.SetViaType(pcbnew.VIATYPE_THROUGH);t.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu)
  else:
   t=pcbnew.PCB_TRACK(current);t.SetStart(pcbnew.VECTOR2I(*r["start_nm"]));t.SetEnd(pcbnew.VECTOR2I(*r["end_nm"]));t.SetLayer(r["layer"])
  t.SetWidth(r["width_nm"]);t.SetLocked(r["locked"]);t.SetNet(nets[r["net"]]);t.m_Uuid=pcbnew.KIID(uid);current.Add(t)
 current_after=inventory(current)
 for uid,r in current_before.items():
  if uid not in removed and current_after.get(uid)!=r:raise ValueError("Unrelated current copper changed "+uid)
 pcbnew.ZONE_FILLER(current).Fill(current.Zones());pcbnew.WriteDRCReport(current,str(report/"after.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True)
 after=parse_drc((report/"after.txt").read_text());old={f["fingerprint"] for f in before["findings"] if f["section"]!="unconnected pads"}
 new=[f for f in after["findings"] if f["section"]!="unconnected pads" and f["fingerprint"] not in old]
 bc,ac=open_counts(before),open_counts(after);worse={net:[bc[net],n] for net,n in ac.items() if n>bc[net]}
 unchanged=all(Path(p).is_file() and sha(p)==h for p,h in hashes.items());accepted=unchanged and not new and not worse and after["counts"]["unconnected pads"]<before["counts"]["unconnected pads"]
 dst.parent.mkdir(parents=True,exist_ok=True);pcbnew.SaveBoard(str(dst),current)
 for ext in (".kicad_pro",".kicad_dru"):
  if paths[2].with_suffix(ext).is_file():shutil.copy2(paths[2].with_suffix(ext),dst.with_suffix(ext))
 result={"schema_version":1,"source_hashes":hashes,"sources_unchanged":unchanged,"base":str(paths[0]),"proof":str(paths[1]),"current":str(paths[2]),"output":str(dst),"output_sha256":sha(dst),"removed":removed,"added":added,"before_counts":before["counts"],"after_counts":after["counts"],"new_non_connectivity_findings":new,"increased_net_open_counts":worse,"accepted_for_further_development":accepted,"fabrication_approval":False,"environment":{"image":os.environ.get("SORX_IMAGE_ID"),"KICAD7_FOOTPRINT_DIR":os.environ.get("KICAD7_FOOTPRINT_DIR"),"command":sys.argv}}
 (report/"result.json").write_text(json.dumps(result,indent=2)+"\n");print(json.dumps({k:result[k] for k in ("before_counts","after_counts","accepted_for_further_development")},indent=2),flush=True)
 return 0 if accepted else 2

if __name__=="__main__":raise SystemExit(main())

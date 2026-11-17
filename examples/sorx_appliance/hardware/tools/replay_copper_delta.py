#!/usr/bin/env python3
"""Replay exact reviewed copper edits onto a separate candidate; fail conflicts.

A replay is accepted for further development only after fresh native DRC,
source-hash stability, and no additional opens on any net.
"""
import argparse, hashlib, json, os, shutil, subprocess, sys
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

def apply_omissions(added, manifest, proof_hash, directory):
 if manifest.get("schema_version")!=1 or manifest.get("proof_sha256")!=proof_hash:
  raise ValueError("Omission manifest must match the exact proof")
 filtered=dict(added);records=[];evidence={}
 for item in manifest.get("omissions",[]):
  uid=item.get("uuid");record=filtered.get(uid)
  if record is None or record.get("kind")!="via" or record.get("net")!="GND":
   raise ValueError("Only an exact proposed GND-via addition may be omitted")
  path=(Path(directory)/item["evidence_path"]).resolve()
  if not item.get("reason") or not path.is_file() or sha(path)!=item.get("evidence_sha256"):
   raise ValueError("Omission requires unchanged object-specific evidence")
  evidence[str(path)]=sha(path);records.append(dict(item,addition=filtered.pop(uid)))
 if not records:raise ValueError("Empty omission manifest")
 return filtered,records,evidence

def copper_differences(before,after):
 return [uid for uid in sorted(set(before)|set(after)) if before.get(uid)!=after.get(uid)]

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

def read_inventory(path):
 command=[sys.executable,str(Path(__file__).resolve()),"--inventory",str(path)]
 completed=subprocess.run(command,check=True,capture_output=True,text=True)
 return json.loads(completed.stdout)

def main():
 import pcbnew
 if len(sys.argv)==3 and sys.argv[1]=="--inventory":
  board=pcbnew.LoadBoard(sys.argv[2])
  print(json.dumps({"tracks":inventory(board),"footprints":footprint_inventory(board),"copper_layers":board.GetCopperLayerCount()}))
  return 0
 ap=argparse.ArgumentParser(description=__doc__)
 for name in ("base","proof","current","output"):ap.add_argument(name,type=Path)
 ap.add_argument("--report-dir",type=Path,required=True);ap.add_argument("--omissions",type=Path);a=ap.parse_args()
 paths=[a.base.resolve(),a.proof.resolve(),a.current.resolve()];dst=a.output.resolve();report=a.report_dir.resolve()
 if dst in paths or dst.exists() or report.exists():raise ValueError("Use new output/report paths")
 deps=paths+[a.current.resolve().with_suffix(".kicad_pro"),Path(__file__)]
 hashes={str(p):sha(p) for p in deps};report.mkdir(parents=True)
 # KiCad7 SWIG cannot safely keep several loaded board lifetimes here.
 # Each source inventory is extracted in its own process before mutation.
 base,proof=[read_inventory(p) for p in paths[:2]]
 current=pcbnew.LoadBoard(str(paths[2]))
 if base["footprints"]!=proof["footprints"]:raise ValueError("Proof changed footprint/pad geometry or connectivity; copper-only replay refused")
 if base["copper_layers"]!=proof["copper_layers"] or current.GetCopperLayerCount()!=base["copper_layers"]:raise ValueError("Layer stack count changed")
 removed,added=delta(base["tracks"],proof["tracks"])
 omissions=[]
 if a.omissions:
  manifest_path=a.omissions.resolve();hashes[str(manifest_path)]=sha(manifest_path)
  added,omissions,evidence=apply_omissions(added,json.loads(manifest_path.read_text()),sha(paths[1]),manifest_path.parent);hashes.update(evidence)
 current_before=inventory(current);check_conflicts(current_before,removed,added)
 if not added and not removed:raise ValueError("No copper edits to replay")
 pcbnew.ZONE_FILLER(current).Fill(current.Zones());pcbnew.WriteDRCReport(current,str(report/"before.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True)
 before=parse_drc((report/"before.txt").read_text())
 # Keep detached SWIG wrappers alive until all native board work completes.
 # Releasing them immediately after Remove corrupts KiCad7 ownership state.
 removed_objects=[t for t in current.GetTracks() if t.m_Uuid.AsString() in removed]
 for t in removed_objects:current.Remove(t)
 nets=current.GetNetsByName()
 replayed_uuids={}
 for uid,r in added.items():
  if r["net"] not in nets:raise ValueError("Missing current net "+r["net"])
  if r["kind"]=="via":
   t=pcbnew.PCB_VIA(current);t.SetPosition(pcbnew.VECTOR2I(*r["position_nm"]));t.SetDrill(r["drill_nm"]);t.SetViaType(pcbnew.VIATYPE_THROUGH);t.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu)
  else:
   t=pcbnew.PCB_TRACK(current);t.SetStart(pcbnew.VECTOR2I(*r["start_nm"]));t.SetEnd(pcbnew.VECTOR2I(*r["end_nm"]));t.SetLayer(r["layer"])
  t.SetWidth(r["width_nm"]);t.SetLocked(r["locked"]);t.SetNet(nets[r["net"]]);current.Add(t)
  replayed_uuids[uid]=t.m_Uuid.AsString()
 current_after=inventory(current)
 for uid,r in added.items():
  if current_after.get(replayed_uuids[uid])!=r:raise ValueError("Replayed geometry differs from proof "+uid)
 for uid,r in current_before.items():
  if uid not in removed and current_after.get(uid)!=r:raise ValueError("Unrelated current copper changed "+uid)
 pcbnew.ZONE_FILLER(current).Fill(current.Zones());pcbnew.WriteDRCReport(current,str(report/"after.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True)
 postfill_changes=copper_differences(current_after,inventory(current))
 after=parse_drc((report/"after.txt").read_text());old={f["fingerprint"] for f in before["findings"] if f["section"]!="unconnected pads"}
 new=[f for f in after["findings"] if f["section"]!="unconnected pads" and f["fingerprint"] not in old]
 bc,ac=open_counts(before),open_counts(after);worse={net:[bc[net],n] for net,n in ac.items() if n>bc[net]}
 unchanged=all(Path(p).is_file() and sha(p)==h for p,h in hashes.items());accepted=unchanged and not new and not worse and not postfill_changes and after["counts"]["unconnected pads"]<before["counts"]["unconnected pads"]
 dst.parent.mkdir(parents=True,exist_ok=True);pcbnew.SaveBoard(str(dst),current)
 for ext in (".kicad_pro",".kicad_dru"):
  if paths[2].with_suffix(ext).is_file():shutil.copy2(paths[2].with_suffix(ext),dst.with_suffix(ext))
 result={"schema_version":1,"source_hashes":hashes,"sources_unchanged":unchanged,"base":str(paths[0]),"proof":str(paths[1]),"current":str(paths[2]),"output":str(dst),"output_sha256":sha(dst),"removed":removed,"added":added,"proof_to_output_uuid":replayed_uuids,"omitted_additions":omissions,"postfill_copper_changes":postfill_changes,"before_counts":before["counts"],"after_counts":after["counts"],"new_non_connectivity_findings":new,"increased_net_open_counts":worse,"accepted_for_further_development":accepted,"fabrication_approval":False,"environment":{"python":sys.version,"kicad":pcbnew.Version(),"image":os.environ.get("SORX_IMAGE_ID"),"KICAD7_FOOTPRINT_DIR":os.environ.get("KICAD7_FOOTPRINT_DIR"),"command":sys.argv}}
 (report/"result.json").write_text(json.dumps(result,indent=2)+"\n");print(json.dumps({k:result[k] for k in ("before_counts","after_counts","accepted_for_further_development")},indent=2),flush=True)
 return 0 if accepted else 2

if __name__=="__main__":raise SystemExit(main())

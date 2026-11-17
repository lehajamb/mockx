#!/usr/bin/env python3
"""Add explicit GND thermal-pad vias to reviewed U13/U2 lands only.

This candidate requires a filled/capped via-in-pad fabrication process. It
never grants a general signal-pad exemption or moves existing copper.
"""
import argparse, hashlib, json, math, os, shutil, sys
from pathlib import Path
import pcbnew
from shapely.geometry import Point, LineString
from shapely.ops import unary_union
from ground_stitch import polygons
from verification_core import parse_drc

POLICY={
 "U13":{"pad":"49","value":"KSZ9131RNX","offsets":[-2,-1,0,1,2],"basis":"Microchip DS00002841D Fig7-3 p145: 5x5, diameter0.30mm,pitch1.00mm; filled or tented against solder loss","reference":"https://ww1.microchip.com/downloads/en/DeviceDoc/00002841D.pdf"},
 "U2":{"pad":"25","value":"STUSB4500","offsets":[-.5,.5],"basis":"STUSB4500 Rev8 Table1 requires EP to GND; 2x2 via pattern is an engineering assumption, not an ST specification","reference":"https://www.st.com/resource/en/datasheet/stusb4500.pdf"}}

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def forbidden_sites(board,target):
 blocked=[]
 target_owner=next(fp.GetReference() for fp in board.GetFootprints() if any(p.m_Uuid.AsString()==target.m_Uuid.AsString() for p in fp.Pads()))
 for fp in board.GetFootprints():
  for pad in fp.Pads():
   # The reviewed filled/capped EP may overlap its own paste apertures.
   # Other footprints and all real copper pads retain their exclusions.
   own_paste_only=fp.GetReference()==target_owner and not any(pad.IsOnLayer(l) for l in range(32))
   if pad.m_Uuid.AsString()!=target.m_Uuid.AsString() and not own_paste_only:
    blocked.extend(g.buffer(.5) for g in polygons(pad.GetEffectivePolygon()))
   drill=pad.GetDrillSize()
   if drill.x or drill.y:
    q=pad.GetPosition();blocked.append(Point(q.x/1e6,q.y/1e6).buffer(max(drill.x,drill.y)/2e6+.15+.25))
  for z in fp.Zones():
   if z.GetIsRuleArea() and z.GetDoNotAllowVias():blocked.extend(g.buffer(.32) for g in polygons(z.Outline()))
 for track in board.GetTracks():
  if isinstance(track,pcbnew.PCB_VIA):
   q=track.GetPosition();r=track.GetDrill()/2e6+.15+.25
   if track.GetNetname()!="GND":r=max(r,track.GetWidth()/2e6+.5)
   blocked.append(Point(q.x/1e6,q.y/1e6).buffer(r))
  elif track.GetNetname()!="GND":
   if isinstance(track,pcbnew.PCB_ARC):raise ValueError("Arc geometry requires explicit review")
   a,z=track.GetStart(),track.GetEnd();blocked.append(LineString([(a.x/1e6,a.y/1e6),(z.x/1e6,z.y/1e6)]).buffer(track.GetWidth()/2e6+.5))
 for z in board.Zones():
  if z.GetIsRuleArea() and z.GetDoNotAllowVias():blocked.extend(g.buffer(.32) for g in polygons(z.Outline()))
  elif z.GetNetname()!="GND":
   for l in board.GetEnabledLayers().Seq():
    if l<=31 and z.IsOnLayer(l):blocked.extend(g.buffer(.5) for g in polygons(z.GetFilledPolysList(l)))
 return unary_union(blocked)

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument("input",type=Path);ap.add_argument("output",type=Path);ap.add_argument("--report-dir",type=Path,required=True)
 args=ap.parse_args();src=args.input.resolve();dst=args.output.resolve();report=args.report_dir.resolve()
 if src==dst or dst.exists() or report.exists():raise ValueError("Use fresh candidate and report paths")
 dependencies=[src,src.with_suffix(".kicad_pro"),Path(__file__),Path(__file__).with_name("ground_stitch.py")]
 bound={str(p):sha(p) for p in dependencies}
 report.mkdir(parents=True);b=pcbnew.LoadBoard(str(src));pcbnew.ZONE_FILLER(b).Fill(b.Zones())
 pcbnew.WriteDRCReport(b,str(report/"before.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True)
 before=parse_drc((report/"before.txt").read_text());records=[]
 for ref,policy in POLICY.items():
  fp=b.FindFootprintByReference(ref)
  if fp is None or fp.GetValue()!=policy["value"]:raise ValueError("Requalify changed target "+ref)
  pad=next(p for p in fp.Pads() if p.GetNumber()==policy["pad"])
  if pad.GetNetname()!="GND" or pad.GetAttribute()!=pcbnew.PAD_ATTRIB_SMD:raise ValueError("Only the explicit SMD ground EP is authorized")
  land=unary_union(list(polygons(pad.GetEffectivePolygon())))
  blocked=forbidden_sites(b,pad);q=pad.GetPosition();cx,cy=q.x/1e6,q.y/1e6
  angle=math.radians(-fp.GetOrientationDegrees());entry={"reference":ref,"pad":policy["pad"],"policy":policy,"added":[],"blocked_sites":[]}
  for dx in policy["offsets"]:
   for dy in policy["offsets"]:
    x=round(cx+dx*math.cos(angle)-dy*math.sin(angle),6);y=round(cy+dx*math.sin(angle)+dy*math.cos(angle),6);point=Point(x,y)
    if not land.contains(point.buffer(.31)) or blocked.intersects(point):
     entry["blocked_sites"].append([x,y]);continue
    v=pcbnew.PCB_VIA(b);v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x),pcbnew.FromMM(y)));v.SetWidth(pcbnew.FromMM(.6));v.SetDrill(pcbnew.FromMM(.3));v.SetViaType(pcbnew.VIATYPE_THROUGH);v.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu);v.SetNet(b.GetNetsByName()["GND"]);b.Add(v)
    entry["added"].append({"xy_mm":[x,y],"uuid":v.m_Uuid.AsString(),"diameter_mm":.6,"drill_mm":.3})
    blocked=blocked.union(point.buffer(.55))
  entry["complete_requested_pattern"]=not entry["blocked_sites"];records.append(entry)
 pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.WriteDRCReport(b,str(report/"after.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True)
 after=parse_drc((report/"after.txt").read_text());old={f["fingerprint"] for f in before["findings"] if f["section"]!="unconnected pads"}
 new=[f for f in after["findings"] if f["section"]!="unconnected pads" and f["fingerprint"] not in old]
 unchanged=all(Path(p).is_file() and sha(p)==h for p,h in bound.items())
 accepted=unchanged and not new and after["counts"]["unconnected pads"]<before["counts"]["unconnected pads"]
 dst.parent.mkdir(parents=True,exist_ok=True);pcbnew.SaveBoard(str(dst),b)
 for ext in (".kicad_pro",".kicad_dru"):
  if src.with_suffix(ext).is_file():shutil.copy2(src.with_suffix(ext),dst.with_suffix(ext))
 result={"schema_version":1,"source_inputs_sha256":bound,"sources_unchanged":unchanged,"output":str(dst),"output_sha256":sha(dst),"before_counts":before["counts"],"after_counts":after["counts"],"new_non_connectivity_findings":new,"thermal_vias":records,"accepted_for_further_development":accepted,"fabrication_approval":False,"required_fabrication_process":"filled and copper-capped via-in-pad, assembler/fabricator qualification still open","thermal_performance_qualified":False,"environment":{"image":os.environ.get("SORX_IMAGE_ID"),"KICAD7_FOOTPRINT_DIR":os.environ.get("KICAD7_FOOTPRINT_DIR"),"kicad":pcbnew.Version(),"command":sys.argv}}
 (report/"result.json").write_text(json.dumps(result,indent=2)+"\n")
 print(json.dumps({k:result[k] for k in ("before_counts","after_counts","accepted_for_further_development")},indent=2),flush=True)
 print("Thermal vias",[(r["reference"],len(r["added"]),len(r["blocked_sites"])) for r in records],flush=True)
 return 0 if accepted else 2

if __name__=="__main__":raise SystemExit(main())

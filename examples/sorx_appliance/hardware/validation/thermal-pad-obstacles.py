import sys
from pathlib import Path
sys.path.insert(0,"/work/hardware/tools")
import pcbnew
from shapely.geometry import Point,LineString
from ground_stitch import polygons
b=pcbnew.LoadBoard("/work/hardware/runs/route-verifier-15-ground-fanout-proof.kicad_pcb")
for ref,num in [("U13","49"),("U2","25")]:
 fp=b.FindFootprintByReference(ref);target=next(p for p in fp.Pads() if p.GetNumber()==num);q=target.GetPosition();point=Point(q.x/1e6,q.y/1e6);print(ref,flush=True)
 for f in b.GetFootprints():
  for pad in f.Pads():
   if pad.m_Uuid.AsString()==target.m_Uuid.AsString():continue
   if any(g.distance(point)<.5 for g in polygons(pad.GetEffectivePolygon())):print("PAD",f.GetReference(),pad.GetNumber(),pad.GetNetname(),[b.GetLayerName(l) for l in pad.GetLayerSet().Seq()],flush=True)
  for z in f.Zones():
   if z.GetIsRuleArea() and z.GetDoNotAllowVias() and any(g.buffer(.32).intersects(point) for g in polygons(z.Outline())):print("FP KEEP",f.GetReference(),flush=True)
 for t in b.GetTracks():
  if t.GetNetname()=="GND":continue
  a,z=t.GetStart(),t.GetEnd();s=Point(a.x/1e6,a.y/1e6) if isinstance(t,pcbnew.PCB_VIA) else LineString([(a.x/1e6,a.y/1e6),(z.x/1e6,z.y/1e6)])
  if s.distance(point)<t.GetWidth()/2e6+.5:print("TRACK",t.GetNetname(),b.GetLayerName(t.GetLayer()),t.m_Uuid.AsString(),flush=True)
 for z in b.Zones():
  if z.GetIsRuleArea() and z.GetDoNotAllowVias() and any(g.buffer(.32).intersects(point) for g in polygons(z.Outline())):print("BOARD KEEP",z.GetName(),flush=True)

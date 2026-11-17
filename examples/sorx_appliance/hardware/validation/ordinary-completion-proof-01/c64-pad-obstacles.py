import pcbnew,sys,json
from pathlib import Path
sys.path.insert(0,"/work/hardware/tools")
from ground_stitch import polygons
from shapely.geometry import Point,LineString
b=pcbnew.LoadBoard("/work/hardware/runs/ordinary-ground-repair/ordinary-input.kicad_pcb");fp=b.FindFootprintByReference("C64");p=next(p for p in fp.Pads() if p.GetNumber()=="2");q=p.GetPosition();center=Point(q.x/1e6,q.y/1e6);rows=[]
for f in b.GetFootprints():
 for pad in f.Pads():
  if any(s.distance(center)<.5 for s in polygons(pad.GetEffectivePolygon())):rows.append({"ref":f.GetReference(),"pad":pad.GetNumber(),"net":pad.GetNetname(),"layers":[b.GetLayerName(l) for l in pad.GetLayerSet().Seq()],"size_mm":[pad.GetSize().x/1e6,pad.GetSize().y/1e6]})
print(json.dumps(rows,indent=2),flush=True);Path(__file__).with_suffix(".json").write_text(json.dumps(rows,indent=2)+"\n")

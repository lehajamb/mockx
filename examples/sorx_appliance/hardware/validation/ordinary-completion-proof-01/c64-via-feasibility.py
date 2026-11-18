import pcbnew,sys,json,hashlib
from pathlib import Path
sys.path.insert(0,"/work/hardware/tools")
from ground_stitch import polygons
from thermal_ground_vias import forbidden_sites
from shapely.geometry import Point
from shapely.ops import unary_union
src=Path("/work/hardware/runs/ordinary-ground-repair/ordinary-input.kicad_pcb");b=pcbnew.LoadBoard(str(src));f=b.FindFootprintByReference("C64");pad=next(p for p in f.Pads() if p.GetNumber()=="2");q=pad.GetPosition();land=unary_union(list(polygons(pad.GetEffectivePolygon())));blocked=forbidden_sites(b,pad);sites=[]
for dx,dy in [(0,0),(-.05,0),(.05,0),(0,-.05),(0,.05)]:
 xy=(q.x/1e6+dx,q.y/1e6+dy);p=Point(xy);sites.append({"xy_mm":xy,"drill_inside_target_with_0p02_margin":land.covers(p.buffer(.17)),"foreign_copper_or_drill_conflict":blocked.intersects(p)})
r={"source_pcb_sha256":hashlib.sha256(src.read_bytes()).hexdigest(),"reference":"C64","pad":"2","net":pad.GetNetname(),"pad_uuid":pad.m_Uuid.AsString(),"pad_size_mm":[pad.GetSize().x/1e6,pad.GetSize().y/1e6],"proposed_land_drill_mm":[.6,.3],"sites":sites,"fabrication_approval":False};print(json.dumps(r,indent=2));Path(__file__).with_suffix(".json").write_text(json.dumps(r,indent=2)+"\n")

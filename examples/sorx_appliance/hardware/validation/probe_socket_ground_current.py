import hashlib,json,sys
from pathlib import Path
import pcbnew
from shapely.geometry import Point,LineString
from shapely.ops import unary_union
from shapely.strtree import STRtree
sys.path.insert(0,"/work/hardware/tools")
from ground_stitch import ground_groups,polygons
from route_completion import route_policy
src=Path("/work/hardware/runs/route-verifier-04b-signals.kicad_pcb")
b=pcbnew.LoadBoard(str(src));items=[];geoms=[]
def add(geo,meta):
 geoms.append(geo);items.append(meta)
for fp in b.GetFootprints():
 for p in fp.Pads():
  layers=[b.GetLayerName(i) for i in b.GetEnabledLayers().Seq() if i<=31 and p.IsOnLayer(i)]
  g=unary_union(list(polygons(p.GetEffectivePolygon())))
  add(g,{"kind":"pad","reference":fp.GetReference(),"pad":p.GetNumber(),"net":p.GetNetname(),"layers":layers,"uuid":p.m_Uuid.AsString()})
for t in b.GetTracks():
 if isinstance(t,pcbnew.PCB_VIA):
  p=t.GetPosition();g=Point(p.x/1e6,p.y/1e6).buffer(t.GetWidth()/2e6)
  layers=["F.Cu","B.Cu","In1.Cu","In2.Cu","In3.Cu","In4.Cu"]
 else:
  a,z=t.GetStart(),t.GetEnd();g=LineString([(a.x/1e6,a.y/1e6),(z.x/1e6,z.y/1e6)]).buffer(t.GetWidth()/2e6);layers=[b.GetLayerName(t.GetLayer())]
 add(g,{"kind":"via" if isinstance(t,pcbnew.PCB_VIA) else "track","net":t.GetNetname(),"layers":layers,"uuid":t.m_Uuid.AsString(),"generic_route_eligible":route_policy(t.GetNetname())[0]})
tree=STRtree(geoms);records=[]
for group in ground_groups(b):
 for pad in group["pads"]:
  if pad["reference"]!="J1":continue
  x,y=pad["x_mm"],pad["y_mm"];direction=-1 if y<56.074 else 1
  options=[]
  for dy in (.8,1.1,1.4,1.8):
   for dx in (0,-.5,.5):
    site=(x+dx,y+direction*dy);pt=Point(site);line=LineString([(x,y),site]);hits={}
    for idx in tree.query(pt.buffer(.8)):
     idx=int(idx);m=items[idx];clearance=.42 if m["kind"]=="pad" and m["net"]=="GND" else .50
     if m["net"]=="GND" and m["kind"]!="pad":continue
     distance=pt.distance(geoms[idx])
     if distance<clearance:hits[idx]={"object":m,"via_distance_mm":round(distance,4),"required_mm":clearance}
    for idx in tree.query(line.buffer(.3)):
     idx=int(idx);m=items[idx]
     if m["net"]=="GND" or "F.Cu" not in m["layers"]:continue
     distance=line.distance(geoms[idx])
     if distance<.275:hits.setdefault(idx,{"object":m,"trace_distance_mm":round(distance,4),"required_mm":.275})
    options.append({"site_mm":site,"blocking_objects":list(hits.values())})
  options.sort(key=lambda o:len(o["blocking_objects"]))
  records.append({"ground_pad":pad,"least_obstructed_survey_sites":options[:3]})
out=Path("/work/hardware/validation/socket-ground-constraints-current.json")
out.write_text(json.dumps({"source_pcb_sha256":hashlib.sha256(src.read_bytes()).hexdigest(),"method":"Read-only straight escape/via space survey; not a routing or manufacturability proof. Positions sampled from pad center outward; copper only, independent drill/zone/rule-area checks still required.","pads_surveyed":len(records),"records":records,"fabrication_approval":False},indent=2))
print("socket ground pads surveyed",len(records))

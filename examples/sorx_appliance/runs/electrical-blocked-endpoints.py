import sys,json
from pathlib import Path
sys.path.insert(0,'/hardware/tools')
import pcbnew,route_completion as r
from shapely.geometry import Point
b=pcbnew.LoadBoard('/hardware/runs/route-electrical-09-branches.kicad_pcb');r.W_TRK=.15;g=r.Grid(b);g.load(b)
path='/hardware/validation/power-routing-02/after-drc.txt';out=[]
for ends in r.parse_unconnected(path):
 net=ends[0][2]
 if not net.startswith(('+','VBUS')):continue
 for xy,l,n,d in ends:
  matches=[]
  for other,items in g.items.items():
   if other==net:continue
   for layers,shape,hole,trk in items:
    if not set(l)&set(layers):continue
    distance=shape.distance(Point(xy))
    if distance<.15+.15/2+.045+(r.HOLE_EXTRA if hole else 0):
     matches.append(dict(net=other,layers=layers,distance_mm=distance,hole=hole,object_uuid=trk.m_Uuid.AsString() if trk else None,bounds=shape.bounds))
  if matches:out.append(dict(description=d,xy=xy,layer=l,blockers=matches))
print(json.dumps(out,indent=2))
Path('/hardware/validation/power-routing-02/blocked-endpoints.json').write_text(json.dumps(out,indent=2)+'\n')

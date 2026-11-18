import sys,json
sys.path.insert(0,'tools')
import pcbnew,route_completion as router
from shapely.geometry import LineString,Point
b=pcbnew.LoadBoard('runs/route-electrical-14-pcie.kicad_pcb');g=router.Grid(b);g.load(b)
line=LineString([(67.1625,39.75),(64.5,39.75)])
for net,items in g.items.items():
 for layers,shape,hole,item in items:
  if 'F' in layers and line.distance(shape)<.4:
   print(net,'distance',round(line.distance(shape),4),'bounds',shape.bounds,'uuid',item.m_Uuid.AsString() if item else None)

for t in b.GetTracks():
 if t.GetNetname()=='+1V2_PHY' and t.GetClass()=='PCB_TRACK' and t.GetLayer()==pcbnew.F_Cu:
  a=(t.GetStart().x/1e6,t.GetStart().y/1e6);z=(t.GetEnd().x/1e6,t.GetEnd().y/1e6)
  if 64<a[0]<68 and 64<z[0]<68 and 38.5<a[1]<40 and 38.5<z[1]<40:print('supply',t.m_Uuid.AsString(),a,z,t.GetWidth()/1e6)

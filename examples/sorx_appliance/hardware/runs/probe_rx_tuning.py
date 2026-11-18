import sys,json,math
sys.path.insert(0,'tools')
import pcbnew,route_completion as router
from shapely.geometry import LineString,Point
b=pcbnew.LoadBoard('runs/route-electrical-14-pcie.kicad_pcb');grid=router.Grid(b);grid.load(b)
for t in b.GetTracks():
 if t.GetClass()!='PCB_TRACK' or t.GetNetname() not in ['PCIE_RX_N','OE_PET_N']:continue
 a=(t.GetStart().x/1e6,t.GetStart().y/1e6);c=(t.GetEnd().x/1e6,t.GetEnd().y/1e6);length=math.dist(a,c);amp=.90/(2*(math.sqrt(2)-1));layer='F' if t.GetLayer()==pcbnew.F_Cu else 'B'
 if length<2*amp+.75:continue
 dx,dy=(c[0]-a[0])/length,(c[1]-a[1])/length
 for sign in [-1,1]:
  nx,ny=-dy*sign,dx*sign;v=(a[0]+.35*dx,a[1]+.35*dy);w=(c[0]-.35*dx,c[1]-.35*dy)
  points=[a,v,(v[0]+amp*(dx+nx),v[1]+amp*(dy+ny)),(w[0]+amp*(-dx+nx),w[1]+amp*(-dy+ny)),w,c];line=LineString(points);conflicts=[]
  for net,items in grid.items.items():
   if net==t.GetNetname():continue
   for layers,shape,hole,item in items:
    if layer in layers and line.distance(shape)<.155/2+.18+.01+(router.HOLE_EXTRA if hole else 0):conflicts.append(net)
  print(json.dumps({'uuid':str(t.m_Uuid.AsString()),'net':t.GetNetname(),'layer':layer,'points':points,'added_mm':line.length-length,'conflicts':sorted(set(conflicts))}))

import sys,numpy as np
sys.path.insert(0,'/hardware/tools')
import pcbnew,route_completion as r
from shapely.geometry import Point
b=pcbnew.LoadBoard('/hardware/runs/route-combined-08-signals.kicad_pcb');r.W_TRK=.15;g=r.Grid(b);g.load(b)
net='+1V8';a=(40.07,55.12);z=(37.8175,54.64)
for p in [a,z,(39,55),(38.5,54.8)]:
 print('POINT',p,'raw',g.T['B'][g.cell(*p)[1],g.cell(*p)[0]])
 print('NEIGHBORS',[(n,round(s.distance(Point(p)),3),layers,round(s.area,3)) for n,items in g.items.items() for layers,s,h,t in items if 'B' in layers and s.distance(Point(p))<.28])
print('route',g.route(net,a,'B',z,'B',rip=False))

import pcbnew,sys,numpy as np
from shapely.geometry import box
from route_completion import Grid
b=pcbnew.LoadBoard('../runs/route-00-six-layer.kicad_pcb')
g=Grid(b);g.padshapes={'GND':[(['F'],box(9.8,9.8,10.2,10.2))]};g.add('GND',['F'],box(9.8,9.8,10.2,10.2))
print('synthetic',g.route('GND',(10,10),'F',via_goal=True))
g=Grid(b);g.load(b)
def trace(frame,event,arg):
 if frame.f_code.co_name=='route' and event=='return':
  q=frame.f_locals; x=q.get('sx');y=q.get('sy')
  print('route return',arg is not None,'visited',q.get('n'),'free',[(k,int(v.sum())) for k,v in q.get('free',{}).items()],'vfree',int(q.get('vfree',np.array([])).sum()))
  if x is not None:
   for k,v in q['free'].items():print(k,v[y-3:y+4,x-3:x+4].astype(int).tolist())
 return trace
sys.settrace(trace)
print('C123',g.route('GND',(122.32,89.1),'F',via_goal=True))

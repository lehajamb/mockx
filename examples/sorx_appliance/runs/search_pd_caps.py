import sys,json,math,itertools
sys.path.insert(0,'/hardware/tools')
import pcbnew
from high_speed_audit import load_geometry
from shapely.geometry import Point,LineString
from shapely.affinity import rotate,translate
from shapely.ops import unary_union
b=pcbnew.LoadBoard('/hardware/runs/route-electrical-13c-trimmed.kicad_pcb');g=load_geometry('',b)
refs=['C14','C15'];nets=['PD_1V2','PD_2V7'];removed=set(nets)
options={}
for ref,net,pin in zip(refs,nets,['21','23']):
 f=b.FindFootprintByReference(ref);fx,fy=f.GetPosition().x/1e6,f.GetPosition().y/1e6;ang=f.GetOrientationDegrees()
 parts=[p for p in g['pads']if p['ref']==ref];signal=next(p for p in g['pads']if p['id']=='U2:'+pin)
 obstacles={}
 for n in [net,'GND']:
  shapes=[p['shape']for p in g['pads']if 'F.Cu'in p['layers']and p['ref']not in refs and p['net']!=n]
  shapes +=[t['line'].buffer(t['width_mm']/2,16)for t in g['tracks']if t['layer']=='F.Cu'and t['net']not in removed and t['net']!=n]
  shapes +=[Point(v['xy']).buffer(v['diameter_mm']/2,16)for v in g['vias']if v['net']not in removed and v['net']!=n]
  obstacles[n]=unary_union(shapes)
 results=[]
 for x,y,a in itertools.product([i/4 for i in range(44,81)],[i/4 for i in range(32,43)],[0,90,180,270]):
  transformed=[];bad=False
  for pad in parts:
   shape=translate(rotate(pad['shape'],ang-a,origin=(fx,fy)),xoff=x-fx,yoff=y-fy)
   if shape.distance(obstacles[pad['net']])<.20:bad=True;break
   transformed.append((pad['net'],shape))
  if bad:continue
  sigshape=next(s for n,s in transformed if n==net);q=sigshape.centroid
  line=LineString([signal['xy'],(q.x,q.y)])
  # Exclude own U2 pad from obstacles by same net; direct copper must fit.
  if line.distance(obstacles[net])<.275:continue
  results.append(dict(x=x,y=y,angle=a,length=line.length,signal=[q.x,q.y],bounds=[list(s.bounds)for n,s in transformed]))
 results.sort(key=lambda o:o['length']);options[ref]=results[:25]
 print(ref,len(results),json.dumps(results[:12]),flush=True)
open('/hardware/validation/power-network-01/pd-cap-placement-search.json','w').write(json.dumps(options,indent=2)+'\n')

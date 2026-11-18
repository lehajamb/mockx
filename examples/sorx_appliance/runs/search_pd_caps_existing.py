import sys,json,math,itertools
sys.path.insert(0,'/hardware/tools')
import pcbnew
from high_speed_audit import load_geometry
from shapely.geometry import Point,LineString
from shapely.affinity import rotate,translate
from shapely.ops import unary_union
b=pcbnew.LoadBoard('/hardware/runs/route-electrical-13c-trimmed.kicad_pcb');g=load_geometry('',b)
refs=['C14','C15'];nets=['PD_1V2','PD_2V7'];removed=set(nets);options={}
allpads=unary_union([p['shape']for p in g['pads']if p['ref']not in refs])
for ref,net,pin in zip(refs,nets,['21','23']):
 f=b.FindFootprintByReference(ref);fx,fy=f.GetPosition().x/1e6,f.GetPosition().y/1e6;ang=f.GetOrientationDegrees()
 parts=[p for p in g['pads']if p['ref']==ref];signal=next(p for p in g['pads']if p['id']=='U2:'+pin)
 def obstacles(n,layer=None):
  shapes=[p['shape']for p in g['pads']if (layer is None or layer in p['layers'])and p['ref']not in refs and p['net']!=n]
  shapes +=[t['line'].buffer(t['width_mm']/2,16)for t in g['tracks']if (layer is None or t['layer']==layer)and t['net']not in removed and t['net']!=n]
  shapes +=[Point(v['xy']).buffer(v['diameter_mm']/2,16)for v in g['vias']if v['net']not in removed and v['net']!=n]
  return unary_union(shapes)
 back={n:obstacles(n,'B.Cu')for n in [net,'GND']};front=obstacles(net,'F.Cu');allob=obstacles(net)
 viaoptions=[(17.7119,11.0326)] if ref=='C14' else [(18.9945,11.5596)]
 for v in viaoptions:print('anchor foreign clearance',ref,Point(v).distance(allob),flush=True)
 print(ref,'viaoptions',len(viaoptions),flush=True)
 results=[]
 for x,y,a in itertools.product([i/4 for i in range(60,97)],[i/4 for i in range(30,53)],[0,90,180,270]):
  transformed=[];bad=False
  for pad in parts:
   shape=translate(rotate(pad['shape'],ang-a,origin=(fx,fy)),xoff=x-fx,yoff=y-fy)
   if shape.distance(back[pad['net']])<.20:bad=True;break
   transformed.append((pad['net'],shape))
  if bad:continue
  sigshape=next(s for n,s in transformed if n==net);q=sigshape.centroid
  for via in viaoptions:
   if Point(via).distance(unary_union([s for n,s in transformed]))<.42:continue
   line=LineString([via,(q.x,q.y)])
   if line.distance(back[net])<.275:continue
   length=line.length+math.dist(via,signal['xy'])
   results.append(dict(x=x,y=y,angle=a,length=length,signal=[q.x,q.y],via=via,bounds=[list(s.bounds)for n,s in transformed]));break
 results.sort(key=lambda o:o['length']);options[ref]=results[:40]
 print(ref,len(results),json.dumps(results[:8]),flush=True)
open('/hardware/validation/power-network-01/pd-cap-existing-anchor-search.json','w').write(json.dumps(options,indent=2)+'\n')

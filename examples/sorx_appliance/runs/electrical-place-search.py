import sys,json,math
sys.path.insert(0,'/hardware/tools')
import pcbnew
from shapely.geometry import Point,box
from shapely.affinity import translate
from high_speed_audit import load_geometry
from gen_pcb import api_rect
b=pcbnew.LoadBoard('/hardware/runs/route-electrical-09-branches.kicad_pcb');g=load_geometry('',b);fps={f.GetReference():f for f in b.GetFootprints()}
ref='U12';fp=fps[ref];old=(fp.GetPosition().x/1e6,fp.GetPosition().y/1e6);rect=box(*api_rect(fp));side=b.GetLayerName(fp.GetLayer());shapes=[p for p in g['pads'] if p['ref']==ref]
others=[(r,box(*api_rect(f))) for r,f in fps.items() if r not in ('U12','C76') and f.GetLayer()==fp.GetLayer() and not r.startswith(('TP','FID'))]
holes=[(f.GetReference(),Point(p.GetPosition().x/1e6,p.GetPosition().y/1e6).buffer(max(p.GetDrillSize().x,p.GetDrillSize().y)/2e6,32)) for f in b.GetFootprints() for p in f.Pads() if p.GetAttribute()==pcbnew.PAD_ATTRIB_NPTH]
foreign=[t for t in g['tracks'] if t['layer']==side and t['line'].intersects(box(25,45,47,68))]
g['vias']=[v for v in g['vias'] if 25<v['xy'][0]<47 and 45<v['xy'][1]<68]
found=[]
for ix in range(56,89):
 for iy in range(96,131):
  xy=(ix/2,iy/2);dx,dy=xy[0]-old[0],xy[1]-old[1];r=translate(rect,dx,dy)
  if any(r.distance(s)<.1 for _,s in others):continue
  hits=[];ok=True
  for p in shapes:
   s=translate(p['shape'],dx,dy)
   if any(s.distance(h)<.25 for _,h in holes):ok=False;break
   for t in foreign:
    if t['net']!=p['net'] and s.distance(t['line'])<t['width_mm']/2+.18:hits.append(t['id'])
   for v in g['vias']:
    if v['net']!=p['net'] and s.distance(Point(v['xy']))<v['diameter_mm']/2+.18:hits.append(v['id'])
  if ok:found.append(dict(xy=xy,angle=fp.GetOrientationDegrees(),side=side,foreign_copper_conflicts=len(set(hits)),distance=math.dist(xy,old)))
found.sort(key=lambda x:(x['foreign_copper_conflicts'],x['distance']))
print('OLD',old,fp.GetOrientationDegrees(),side);print(json.dumps(found[:20],indent=2))

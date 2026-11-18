import sys,json,math
sys.path.insert(0,'/hardware/tools')
import pcbnew
from shapely.geometry import Point,box
from shapely.affinity import translate
from high_speed_audit import load_geometry
from gen_pcb import api_rect
b=pcbnew.LoadBoard('/hardware/runs/route-electrical-10-usb3.kicad_pcb');fps={f.GetReference():f for f in b.GetFootprints()}
fps['U12'].SetPosition(pcbnew.VECTOR2I(30000000,55500000))
g=load_geometry('',b)
for ref,region,target in [('C76',(28,53,35,60),(31.1375,56.45)),('TP21',(16,23,24,30),(19,26.5))]:
 fp=fps[ref];old=(fp.GetPosition().x/1e6,fp.GetPosition().y/1e6);rect=box(*api_rect(fp));side=b.GetLayerName(fp.GetLayer());shapes=[p for p in g['pads'] if p['ref']==ref]
 others=[(r,box(*api_rect(f))) for r,f in fps.items() if r!=ref and f.GetLayer()==fp.GetLayer() and not r.startswith(('TP','FID'))]
 pads=[p for p in g['pads'] if p['ref']!=ref and side in p['layers']]
 holes=[Point(p.GetPosition().x/1e6,p.GetPosition().y/1e6).buffer(max(p.GetDrillSize().x,p.GetDrillSize().y)/2e6,32) for f in b.GetFootprints() for p in f.Pads() if p.GetAttribute()==pcbnew.PAD_ATTRIB_NPTH]
 foreign=[t for t in g['tracks'] if t['layer']==side and t['line'].intersects(box(*region).buffer(2))]
 vias=[v for v in g['vias'] if box(*region).buffer(2).contains(Point(v['xy']))]
 found=[]
 for ix in range(int(region[0]*4),int(region[2]*4)+1):
  for iy in range(int(region[1]*4),int(region[3]*4)+1):
   xy=(ix/4,iy/4);dx,dy=xy[0]-old[0],xy[1]-old[1];r=translate(rect,dx,dy)
   if any(r.distance(s)<.1 for _,s in others):continue
   hits=[];ok=True
   for p in shapes:
    s=translate(p['shape'],dx,dy)
    if any(s.distance(h)<.25 for h in holes):ok=False;break
    if any(s.distance(q['shape'])<.18 for q in pads if q['net']!=p['net']):ok=False;break
    for t in foreign:
     if t['net']!=p['net'] and s.distance(t['line'])<t['width_mm']/2+.18:hits.append(t['id'])
    for v in vias:
     if v['net']!=p['net'] and s.distance(Point(v['xy']))<v['diameter_mm']/2+.18:hits.append(v['id'])
   if ok:found.append(dict(xy=xy,angle=fp.GetOrientationDegrees(),side=side,foreign_copper_conflicts=len(set(hits)),distance=math.dist(xy,target)))
 found.sort(key=lambda x:(x['foreign_copper_conflicts'],x['distance']))
 print(ref,'OLD',old,fp.GetOrientationDegrees(),side,flush=True);print(json.dumps(found[:12],indent=2),flush=True)

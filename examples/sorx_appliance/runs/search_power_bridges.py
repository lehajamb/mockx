import sys,json,math
from pathlib import Path
sys.path.insert(0,'/hardware/tools')
import pcbnew
from high_speed_audit import load_geometry,polygons
from shapely.geometry import Point,LineString
from shapely.ops import unary_union,nearest_points
b=pcbnew.LoadBoard('/hardware/runs/electrical-final17/route-electrical-17-sma.kicad_pcb');g=load_geometry('',b);islands=json.loads(Path('/hardware/validation/electrical-integration-16/power-islands.json').read_text());shapes={}
for t in g['tracks']:shapes[t['id']]=dict(net=t['net'],layers=[t['layer']],shape=t['line'].buffer(t['width_mm']/2))
for p in g['pads']:shapes[p['id']]=dict(net=p['net'],layers=p['layers'],shape=p['shape'])
for v in g['vias']:shapes[v['id']]=dict(net=v['net'],layers=v['layers'],shape=Point(v['xy']).buffer(v['diameter_mm']/2))
holes=[]
for f in b.GetFootprints():
 for p in f.Pads():
  if p.GetAttribute()==pcbnew.PAD_ATTRIB_NPTH:holes.extend(polygons(p.GetEffectivePolygon()))
holes=unary_union(holes);results={}
for net in ['+1V8','VBUS_IN','+1V2_PHY']:
 obs={l:unary_union([o['shape']for o in shapes.values()if l in o['layers']and o['net']!=net])for l in g['layers']}
 groups=islands[net];joins=[]
 for a in range(len(groups)):
  for z in range(a+1,len(groups)):
   aa=[(i,shapes[i])for i in groups[a]['objects']+groups[a]['pads']];zz=[(i,shapes[i])for i in groups[z]['objects']+groups[z]['pads']];options=[]
   for ia,sa in aa:
    for iz,sz in zz:
     layers=set(sa['layers'])&set(sz['layers'])
     if not layers:continue
     dist=sa['shape'].distance(sz['shape'])
     if dist>15:continue
     p,q=nearest_points(sa['shape'],sz['shape']);line=LineString([p,q])
     for l in layers:
      # .25mm for the36mA sense branch; .15mm for5mA pullups or shortPHY escape.
      width=.25 if net=='VBUS_IN'else .15
      if line.distance(obs[l])<width/2+.195 or line.distance(holes)<width/2+.25:continue
      if net=='+1V2_PHY'and line.length>5:continue
      options.append(dict(a=ia,z=iz,layer=l,start=list(p.coords[0]),end=list(q.coords[0]),length_mm=line.length,width_mm=width))
   options.sort(key=lambda x:x['length_mm']);joins.append(dict(groups=[a,z],options=options[:8]));print(net,a,z,len(options),json.dumps(options[:3]),flush=True)
 results[net]=joins
Path('/hardware/validation/electrical-integration-18-power/direct-bridge-search.json').write_text(json.dumps(results,indent=2)+'\n')

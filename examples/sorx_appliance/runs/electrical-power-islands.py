import sys,json,collections
from pathlib import Path
sys.path.insert(0,"/hardware/tools")
from high_speed_audit import load_geometry
from shapely.geometry import Point
from shapely.strtree import STRtree
path=sys.argv[1];out=sys.argv[2]
g=load_geometry(path);results={}
for net in ('VBUS_IN','+5V','+3V3','+1V8','+4V5_PD','+1V2_PHY','VBUS_DBG_RAW'):
 nodes=[]
 for t in g['tracks']:
  if t['net']==net:nodes.append(dict(kind='track',id=t['id'],shape=t['line'].buffer(t['width_mm']/2),layers=[t['layer']],length=t['line'].length,width=t['width_mm']))
 for p in g['pads']:
  if p['net']==net:nodes.append(dict(kind='pad',id=p['id'],shape=p['shape'],layers=p['layers']))
 for v in g['vias']:
  if v['net']==net:nodes.append(dict(kind='via',id=v['id'],shape=Point(v['xy']).buffer(v['diameter_mm']/2),layers=v['layers']))
 parent=list(range(len(nodes)))
 def find(i):
  while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
  return i
 for layer in g['layers']:
  ids=[i for i,n in enumerate(nodes) if layer in n['layers']];shapes=[nodes[i]['shape'] for i in ids];tree=STRtree(shapes)
  for a,shape in enumerate(shapes):
   for b in tree.query(shape):
    if b>a and shape.intersects(shapes[b]):parent[find(ids[a])]=find(ids[b])
 clusters=collections.defaultdict(list)
 for i in range(len(nodes)):clusters[find(i)].append(nodes[i])
 result=[]
 for ns in clusters.values():
  result.append(dict(pads=sorted(n['id'] for n in ns if n['kind']=='pad'),objects=[n['id'] for n in ns if n['kind']!='pad'],track_length_mm=sum(n.get('length',0) for n in ns),bounds=[n['shape'].bounds for n in ns]))
 results[net]=result
 print(net,[(len(n['objects']),n['pads']) for n in result],flush=True)
Path(out).write_text(json.dumps(results,indent=2)+'\n')

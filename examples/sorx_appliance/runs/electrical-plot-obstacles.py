import sys
sys.path.insert(0,'/hardware/tools')
import pcbnew
from high_speed_audit import load_geometry
from shapely.geometry import box,Point
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
path='/hardware/runs/route-combined-08-signals.kicad_pcb';g=load_geometry(path)
fig,axes=plt.subplots(1,3,figsize=(18,7))
for ax,(layer,net,bounds,title) in zip(axes,[('B.Cu','+1V8',(34,51,43,59),'U12/C76 supply escape'),('F.Cu','+1V2_PHY',(67,32,76,40),'U13 pin4 supply escape'),('F.Cu','VBUS_DBG_RAW',(112,102,124,114),'J10 debug VBUS')]):
 region=box(*bounds)
 for t in g['tracks']:
  if t['layer']!=layer:continue
  shape=t['line'].buffer(t['width_mm']/2).intersection(region)
  if shape.is_empty:continue
  polys=list(shape.geoms) if hasattr(shape,'geoms') else [shape]
  for p in polys:
   if p.geom_type=='Polygon':ax.fill(*p.exterior.xy,color='#069f43' if t['net']==net else '#ad3838',alpha=.7)
 for p in g['pads']:
  if layer not in p['layers'] or not p['shape'].intersects(region):continue
  polys=list(p['shape'].geoms) if hasattr(p['shape'],'geoms') else [p['shape']]
  for z in polys:
   if z.geom_type=='Polygon':ax.fill(*z.exterior.xy,color='#069f43' if p['net']==net else '#777777',alpha=.6)
  if p['net']==net:ax.text(*p['xy'],p['id'],fontsize=8,color='navy')
 for v in g['vias']:
  if not region.covers(Point(v['xy'])):continue
  ax.add_patch(plt.Circle(v['xy'],v['diameter_mm']/2,color='#069f43' if v['net']==net else '#bd6655',alpha=.8))
  ax.add_patch(plt.Circle(v['xy'],v['drill_mm']/2,color='white'))
 ax.set_xlim(bounds[0],bounds[2]);ax.set_ylim(bounds[3],bounds[1]);ax.set_aspect('equal');ax.set_title(title+' '+layer);ax.grid(alpha=.2)
fig.tight_layout();fig.savefig('/hardware/validation/power-routing-02/blocked-escapes.png',dpi=180)

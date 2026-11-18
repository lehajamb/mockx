import sys
sys.path.insert(0,'/hardware/tools')
from high_speed_audit import load_geometry
from shapely.geometry import Point
import matplotlib;matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon
from matplotlib.collections import PatchCollection
g=load_geometry('/hardware/runs/electrical-final17/route-electrical-17-sma.kicad_pcb')
fig,axes=plt.subplots(1,3,figsize=(18,9))
for ax,layer in zip(axes,['F.Cu','B.Cu','In2.Cu']):
 for p in g['pads']:
  if layer not in p['layers']or not(112<p['xy'][0]<124 and 102<p['xy'][1]<117):continue
  color='limegreen'if p['net']=='VBUS_DBG_RAW'else'lightgrey'if p['net']=='GND'else'orange'
  shapes=[p['shape']]if p['shape'].geom_type=='Polygon'else list(p['shape'].geoms)
  for s in shapes:ax.add_patch(Polygon(list(s.exterior.coords),color=color,alpha=.8))
  ax.text(*p['xy'],p['id'],fontsize=6)
 for t in g['tracks']:
  if t['layer']!=layer:continue
  s=t['line'].buffer(t['width_mm']/2)
  color='green'if t['net']=='VBUS_DBG_RAW'else'grey'if t['net']=='GND'else'red'
  ax.add_patch(Polygon(list(s.exterior.coords),color=color,alpha=.6))
 for v in g['vias']:
  if not(112<v['xy'][0]<124 and 102<v['xy'][1]<117):continue
  s=Point(v['xy']).buffer(v['diameter_mm']/2);ax.add_patch(Polygon(list(s.exterior.coords),color='grey'if v['net']=='GND'else'purple',alpha=.6))
 ax.set_xlim(112,124);ax.set_ylim(117,102);ax.set_aspect('equal');ax.grid();ax.set_title(layer)
fig.tight_layout();fig.savefig('/hardware/validation/electrical-integration-18-power/debug-power-geometry.png',dpi=160)

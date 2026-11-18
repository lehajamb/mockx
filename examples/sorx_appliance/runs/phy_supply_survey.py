import sys,json,math,hashlib
from pathlib import Path
sys.path.insert(0,"/hardware/tools")
import pcbnew
import route_completion as rc
from high_speed_audit import load_geometry,polygons
from shapely.geometry import Point,LineString,box
from shapely.ops import unary_union
source=Path("/hardware/runs/electrical-final17/power-bridge-proof01.kicad_pcb")
b=pcbnew.LoadBoard(str(source)); geo=load_geometry('',b)
net="+1V2_PHY"; region=box(62,27,77,46); shapes=[]; holes=[]
for t in geo['tracks']:
 if t['line'].intersects(region):shapes.append((t['net'],[t['layer']],t['line'].buffer(t['width_mm']/2),t['id']))
for v in geo['vias']:
 sh=Point(v['xy']).buffer(v['diameter_mm']/2)
 if sh.intersects(region):shapes.append((v['net'],v['layers'],sh,v['id']));holes.append(Point(v['xy']).buffer(.15))
for f in b.GetFootprints():
 for p in f.Pads():
  xy=(p.GetPosition().x/1e6,p.GetPosition().y/1e6)
  if not Point(xy).intersects(region.buffer(2)):continue
  ls=[b.GetLayerName(i) for i in [pcbnew.F_Cu,pcbnew.In2_Cu,pcbnew.In3_Cu,pcbnew.B_Cu]if p.IsOnLayer(i)]
  if p.GetAttribute()==pcbnew.PAD_ATTRIB_NPTH:holes.append(Point(xy).buffer(max(p.GetDrillSize().x,p.GetDrillSize().y)/2e6))
  else:
   if p.GetDrillSize().x:holes.append(Point(xy).buffer(max(p.GetDrillSize().x,p.GetDrillSize().y)/2e6))
   for sh in polygons(p.GetEffectivePolygon()):shapes.append((p.GetNetname(),ls,sh,f.GetReference()+'.'+p.GetNumber()))
obs={l:unary_union([s for n,ls,s,i in shapes if n!=net and l in ls])for l in ['F.Cu','In2.Cu','In3.Cu','B.Cu']}
obsall=unary_union(list(obs.values())); holes=unary_union(holes)
pin=(71.85,36.0625);options=[]
for ix in range(2800,2961):
 for iy in range(1120,1461):
  xy=(ix*.025,iy*.025);pt=Point(xy)
  if pt.distance(obsall)<.485 or pt.distance(holes)<.4:continue
  line=LineString([pin,xy])
  if line.distance(obs['F.Cu'])<.31 or line.distance(holes)<.325:continue
  options.append(dict(xy=xy,length_mm=line.length))
options.sort(key=lambda x:x['length_mm'])
print('DIRECT OPTIONS',json.dumps(options[:20]),flush=True)
rc.W_TRK=.25;g=rc.Grid(b);g.load(b)
r=g.route(net,pin,'F',via_goal=True,rip=False)
print('GRID VIA ESCAPE',str(r),flush=True)
result=dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),direct_via_options=options[:30],grid_via_escape=r,conservative_clearance_mm=.185,trace_width_mm=.25,via_diameter_mm=.6,drill_mm=.3,no_board_changes=True)
Path('/hardware/validation/phy-supply-survey.json').write_text(json.dumps(result,indent=2)+'\n')

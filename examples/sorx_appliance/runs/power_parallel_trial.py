import sys,json,math,itertools,shutil
from pathlib import Path
sys.path.insert(0,'/hardware/tools')
import pcbnew,route_completion as rc
from high_speed_audit import load_geometry
from shapely.geometry import Point,LineString
from shapely.ops import unary_union
source=Path('/hardware/runs/power-widen-proof-01.kicad_pcb');b=pcbnew.LoadBoard(str(source));geo=load_geometry(source,b)
rc.W_TRK=1.5;rc.VIA_D=.8;rc.VIA_DR=.4
net='+5V';grid=rc.Grid(b);grid.load(b)
# Deliberately forbid all automatic layer changes; the selected external via pairs are explicit.
grid.T['F'][:]+=100;grid.T['C'][:]+=100
for layer in rc.COPPER:grid.V[layer][:]+=100
foreign=unary_union([p['shape']for p in geo['pads']if p['net']!=net]+[t['line'].buffer(t['width_mm']/2,16)for t in geo['tracks']if t['net']!=net]+[Point(v['xy']).buffer(v['diameter_mm']/2,16)for v in geo['vias']if v['net']!=net])
allpads=unary_union([p['shape']for p in geo['pads']]);holes=[]
for f in b.GetFootprints():
 for p in f.Pads():
  if p.GetDrillSize().x>0:holes.append(Point(p.GetPosition().x/1e6,p.GetPosition().y/1e6).buffer(max(p.GetDrillSize().x,p.GetDrillSize().y)/2e6,32))
for v in geo['vias']:holes.append(Point(v['xy']).buffer(v['drill_mm']/2,32))
holes=unary_union(holes)
def candidates(origin,radius):
 out=[]
 for t in geo['tracks']:
  if t['net']!=net or t['layer']!='F.Cu':continue
  for distance in [i*.20 for i in range(int(t['line'].length/.2)+1)]:
   p=t['line'].interpolate(distance);xy=(p.x,p.y)
   if math.dist(xy,origin)>radius:continue
   if p.distance(foreign)<.62 or p.distance(holes)<.49 or p.distance(allpads)<.52:continue
   if any(math.dist(xy,e[0])<.18 for e in out):continue
   out.append((xy,t['id']))
 return out
sources=candidates((20.655,61.35),6);sinks=candidates((44.7195,51.512),5)
print('candidates',len(sources),len(sinks),flush=True)
# Source/sink array pairs must be distinct, drill-safe and lie on the same native F segment.
def pairs(items):
 return [(a,c)for a,c in itertools.combinations(items,2)if a[1]==c[1]and .8<=math.dist(a[0],c[0])<=1.6]
choices=sorted(itertools.product(pairs(sources),pairs(sinks)),key=lambda c:math.dist(c[0][0][0],c[1][0][0]))
print('array combinations',len(choices),flush=True)
record=None
for sp,dp in choices[:40]:
 a,z=sp[0][0],dp[0][0];trial=grid.route(net,a,'B',z,'B',rip=False)
 if trial is None:continue
 runs,vias=trial
 if vias or any(layer!='B'for layer,_ in runs):continue
 # Secondary barrel joins the primary via on each face using a short .8mm B stub;
 # same F segment already connects both primary/secondary barrels.
 extra=[]
 for pair in (sp,dp):
  line=LineString([pair[0][0],pair[1][0]])
  obstacles=unary_union([p['shape']for p in geo['pads']if 'B.Cu'in p['layers']and p['net']!=net]+[t['line'].buffer(t['width_mm']/2,16)for t in geo['tracks']if t['layer']=='B.Cu'and t['net']!=net]+[Point(v['xy']).buffer(v['diameter_mm']/2,16)for v in geo['vias']if v['net']!=net])
  if line.distance(obstacles)<.62:break
  extra.append(('B',[pair[0][0],pair[1][0]]))
 if len(extra)!=2:continue
 rc.commit(b,grid,net,b.GetNetsByName()[net],runs,[],1.5)
 rc.commit(b,grid,net,b.GetNetsByName()[net],extra,[p[0]for pair in(sp,dp)for p in pair],.8)
 record=dict(source_arrays=sp,sink_arrays=dp,runs=runs,stubs=extra,via_diameter_mm=.8,finished_drill_mm=.4,main_width_mm=1.5,stub_width_mm=.8)
 print('ROUTED',json.dumps(record),flush=True);break
out=Path('/hardware/runs/power-parallel-proof-01.kicad_pcb')
if record:
 pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.SaveBoard(str(out),b)
 for suffix in('.kicad_pro','.kicad_dru'):
  if source.with_suffix(suffix).exists():shutil.copy2(source.with_suffix(suffix),out.with_suffix(suffix))
 Path('/hardware/validation/power-widen-proof-01/parallel-record.json').write_text(json.dumps(record,indent=2)+'\n')
else:print('No approved array/trunk combination found',flush=True)

import sys,json,shutil
from pathlib import Path
sys.path.insert(0,'/hardware/tools')
import pcbnew,route_completion as rc
source=Path('/hardware/runs/power-widen-proof-01.kicad_pcb');b=pcbnew.LoadBoard(str(source));rc.W_TRK=1.5;g=rc.Grid(b);g.load(b)
g.T['B'][:]+=100;g.T['C'][:]+=100
for l in rc.COPPER:g.V[l][:]+=100
r=g.route('+5V',(20.655,61.35),'F',(44.7195,51.512),'F',rip=False)
if r:
 runs,vias=r
 print('FOUND',json.dumps(r),flush=True)
 if vias or any(l!='F'for l,points in runs):raise ValueError('unexpected transition')
 rc.commit(b,g,'+5V',b.GetNetsByName()['+5V'],runs,[],1.5)
 out=Path('/hardware/runs/power-parallel-front-proof-01.kicad_pcb');pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.SaveBoard(str(out),b)
 for s in('.kicad_pro','.kicad_dru'):
  if source.with_suffix(s).exists():shutil.copy2(source.with_suffix(s),out.with_suffix(s))
 Path('/hardware/validation/power-widen-proof-01/parallel-front-record.json').write_text(json.dumps(r,indent=2)+'\n')
else:print('No front-only path at1.5mm',flush=True)

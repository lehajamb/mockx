#!/usr/bin/env python3
"""Add a 1.5 mm front-only parallel5V trunk, with independent native DRC acceptance.

The retained narrow rail is not used to justify new via current sharing. No new
vias or layer transitions are permitted. Physical thermal validation remains open.
"""
import argparse,hashlib,json,math,shutil
from pathlib import Path

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 import pcbnew
 import route_completion as rc
 from verification_core import parse_drc
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('source',type=Path);ap.add_argument('output',type=Path);ap.add_argument('evidence',type=Path);a=ap.parse_args()
 if a.output.exists() or a.evidence.exists():raise ValueError('Fresh output and evidence paths required')
 a.evidence.mkdir(parents=True);b=pcbnew.LoadBoard(str(a.source));net='+5V';width=1.5
 def pad(ref,num):
  f=b.FindFootprintByReference(ref);p=next(p for p in f.Pads()if p.GetNumber()==num)
  if p.GetNetname()!=net or not p.IsOnLayer(pcbnew.F_Cu):raise ValueError('Unexpected endpoint')
  return(p.GetPosition().x/1e6,p.GetPosition().y/1e6)
 start,end=pad('L1','2'),pad('J1','259')
 def report(label):
  pcbnew.ZONE_FILLER(b).Fill(b.Zones());path=a.evidence/(label+'.txt');pcbnew.WriteDRCReport(b,str(path),pcbnew.EDA_UNITS_MILLIMETRES,True);return parse_drc(path.read_text())
 before=report('before-drc');old={x['fingerprint']for x in before['findings']if x['section']!='unconnected pads'}
 rc.W_TRK=width;g=rc.Grid(b);g.load(b)
 # A large positive obstacle count preserves subtraction-mask arithmetic while
 # deliberately disabling other routing layers and every automatic transition.
 g.T['B'][:]+=100;g.T['C'][:]+=100
 for layer in rc.COPPER:g.V[layer][:]+=100
 result=g.route(net,start,'F',end,'F',rip=False)
 if result is None:raise ValueError('No full-width front-only bypass; no fallback to narrow traces')
 runs,vias=result
 if vias or any(layer!='F'for layer,_ in runs):raise ValueError('Unexpected transition')
 rc.commit(b,g,net,b.GetNetsByName()[net],runs,[],width)
 after=report('after-drc');new=[x for x in after['findings']if x['section']!='unconnected pads'and x['fingerprint']not in old]
 accepted=not new and after['counts']['unconnected pads']<=before['counts']['unconnected pads']
 pcbnew.SaveBoard(str(a.output),b)
 for suffix in('.kicad_pro','.kicad_dru'):
  if a.source.with_suffix(suffix).exists():shutil.copy2(a.source.with_suffix(suffix),a.output.with_suffix(suffix))
 length=sum(math.dist(x,y)for _,pts in runs for x,y in zip(pts,pts[1:]));rho=1.724e-5*(1+.00393*65);resistance=rho*length/(width*.025)
 output=dict(status='ACCEPTED FOR FURTHER DEVELOPMENT'if accepted else'REJECTED',fabrication_ready=False,source=str(a.source),source_sha256=sha(a.source),output=str(a.output),output_sha256=sha(a.output),tool_sha256=sha(__file__),router_sha256=sha(rc.__file__),before=before['counts'],after=after['counts'],new_other_findings=new,runs=runs,width_mm=width,length_mm=length,vias=[],conductor_path_bound=dict(copper_um=25,temperature_c=85,rho_ohm_mm=rho,ohms=resistance,load_a=2.59,drop_v=resistance*2.59,power_w=resistance*2.59**2,interpretation='Entire load through new full-width centerline path; existing parallel copper can only reduce its conductor resistance. Does not include pad spreading, return plane, connector, regulation tolerance or contact resistance; not a thermal prediction.'))
 (a.evidence/'result.json').write_text(json.dumps(output,indent=2)+'\n');print(json.dumps({k:output[k]for k in ['status','before','after','length_mm','conductor_path_bound']}),flush=True)
 return 0 if accepted else 1
if __name__=='__main__':raise SystemExit(main())

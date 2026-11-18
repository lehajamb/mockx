import sys,json
from pathlib import Path
sys.path.insert(0,'/hardware/tools')
from high_speed_audit import load_geometry,RouteGraph
out={}
for name,path in [('before','/hardware/runs/route-electrical-13c-trimmed.kicad_pcb'),('after','/hardware/runs/pd-decoupling-proof-02.kicad_pcb')]:
 g=load_geometry(path);out[name]={}
 for net,a,z in [('PD_1V2','U2:21','C14:1'),('PD_2V7','U2:23','C15:1')]:
  r=RouteGraph([t for t in g['tracks']if t['net']==net],[p for p in g['pads']if p['net']==net],[v for v in g['vias']if v['net']==net]).route(a,z);out[name][net]=r
  print(name,net,r['planar_length_mm'],r['via_ids'],flush=True)
Path('/hardware/validation/pd-decoupling-proof-02/endpoint-paths.json').write_text(json.dumps(out,indent=2)+'\n')

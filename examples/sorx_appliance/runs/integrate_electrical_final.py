import subprocess,sys,json,hashlib,datetime
from pathlib import Path
root=Path('/hardware');out=root/'validation/electrical-integration-16';out.mkdir()
source='runs/route-combined-15b-ground.kicad_pcb'
if hashlib.sha256((root/source).read_bytes()).hexdigest()!='0e825f295aac2fe7da0723bf85a7b8d2c6de2a09d00255e1a4cd65d6a5e986c6':raise ValueError('Wrong shared checkpoint')
steps=[('network',['tools/power_network.py',source,'validation/electrical-integration-16/network-before.json']),('widen',['tools/widen_main_power.py',source,'validation/electrical-integration-16/network-before.json','runs/route-electrical-16a-widen.kicad_pcb','validation/electrical-integration-16/widen']),('bypass',['tools/route_main_bypass.py','runs/route-electrical-16a-widen.kicad_pcb','runs/route-electrical-16b-bypass.kicad_pcb','validation/electrical-integration-16/bypass']),('pd-local',['tools/relocate_pd_decoupling.py','runs/route-electrical-16b-bypass.kicad_pcb','runs/route-electrical-16c-pd.kicad_pcb','validation/electrical-integration-16/pd-local']),('headers',['tools/update_service_headers.py','runs/route-electrical-16c-pd.kicad_pcb','runs/route-electrical-16d-headers.kicad_pcb','validation/electrical-integration-16/headers'])]
manifest={'image':'sha256:23dbab2af8391fba7496a546d6729c37a5e8bdfb8e941ab903589252e8bec1e2','environment':{'KICAD7_FOOTPRINT_DIR':'/usr/share/kicad/footprints'},'steps':[]}
for name,args in steps:
 command=[sys.executable]+args;record={'name':name,'command':command,'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'tool_sha256':hashlib.sha256((root/args[0]).read_bytes()).hexdigest()};print('START',name,flush=True)
 with (out/(name+'.log')).open('w')as log:r=subprocess.run(command,cwd=root,stdout=log,stderr=subprocess.STDOUT)
 record['returncode']=r.returncode;manifest['steps'].append(record);(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print('FINISHED',name,r.returncode,flush=True)
 if r.returncode:raise SystemExit(r.returncode)
print('ALL FIVE STAGES PASSED',flush=True)

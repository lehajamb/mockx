import json,subprocess,sys,hashlib
from pathlib import Path
root=Path("/work/hardware");code="import json,pcbnew,sys;from ground_stitch import ground_groups;b=pcbnew.LoadBoard(sys.argv[1]);pcbnew.ZONE_FILLER(b).Fill(b.Zones());print(json.dumps([g['pads'] for g in ground_groups(b)]))"
result={}
for label,path in [("before",root/"runs/route-combined-15b-ground.kicad_pcb"),("after",root/"runs/route-verifier-21-ordinary-proof.kicad_pcb")]:
 p=subprocess.run([sys.executable,"-c",code,str(path)],cwd=root/"tools",capture_output=True,text=True,check=True)
 result[label]={"source_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"groups":json.loads(p.stdout)}
before={(p["reference"],p["pad"]) for g in result["before"]["groups"] for p in g}
result["newly_disconnected_pads"]=[p for g in result["after"]["groups"] for p in g if(p["reference"],p["pad"]) not in before]
Path(__file__).with_suffix(".json").write_text(json.dumps(result,indent=2)+"\n");print(json.dumps(result["newly_disconnected_pads"],indent=2),flush=True)

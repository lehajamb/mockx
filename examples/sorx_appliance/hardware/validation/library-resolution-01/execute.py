import hashlib,json,os,shutil,subprocess,sys,tempfile
from collections import Counter
from pathlib import Path
sys.path.insert(0,"/work/hardware/tools")
from verification_core import parse_drc
root=Path("/work/hardware");source=root/"runs/route-electrical-09-branches.kicad_pcb";report=root/"validation/library-resolution-01"
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
cases=[]
with tempfile.TemporaryDirectory(prefix="sorx-codex-libraries-") as temp:
 for name,local,explicit_env in (("checkpoint-no-local",False,False),("project-local",True,False),("project-local-explicit-env",True,True)):
  project=Path(temp)/name;project.mkdir()
  pcb=project/"sorx.kicad_pcb";shutil.copy2(source,pcb);shutil.copy2(source.with_suffix(".kicad_pro"),pcb.with_suffix(".kicad_pro"))
  if local:
   shutil.copy2(root/"hw/fp-lib-table",project/"fp-lib-table");shutil.copytree(root/"hw/sorx.pretty",project/"sorx.pretty")
  env=dict(os.environ)
  if explicit_env:env["KICAD7_FOOTPRINT_DIR"]="/usr/share/kicad/footprints"
  drc=report/(name+".txt")
  command=[sys.executable,"-c","import pcbnew,sys; b=pcbnew.LoadBoard(sys.argv[1]); pcbnew.WriteDRCReport(b,sys.argv[2],pcbnew.EDA_UNITS_MILLIMETRES,True)",str(pcb),str(drc)]
  p=subprocess.run(command,env=env,capture_output=True,text=True)
  (report/(name+".stdout.log")).write_text(p.stdout);(report/(name+".stderr.log")).write_text(p.stderr)
  if p.returncode:raise RuntimeError(name+": "+p.stderr)
  data=parse_drc(drc.read_text());counts=dict(Counter(f["category"] for f in data["findings"]))
  cases.append({"name":name,"command":command,"exit_code":p.returncode,"source_pcb_sha256":sha(pcb),
                "local_table":local,"explicit_KICAD7_FOOTPRINT_DIR":explicit_env,"counts":data["counts"],"categories":counts,
                "library_samples":[f for f in data["findings"] if f["category"].startswith("lib_")][:8]})
  print(name,json.dumps({"counts":data["counts"],"categories":counts}),flush=True)
result={"input":str(source),"input_sha256":sha(source),"image":os.environ["SORX_IMAGE_ID"],"cases":cases,"fabrication_approval":False}
(report/"result.json").write_text(json.dumps(result,indent=2))

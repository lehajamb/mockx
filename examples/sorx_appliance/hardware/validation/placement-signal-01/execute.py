import hashlib, json, os, subprocess, sys, time
from pathlib import Path
TOOLS = Path("/work/hardware/tools")
sys.path.insert(0, str(TOOLS))
from verification_core import parse_drc
root = Path("/work/hardware")
outdir = root / "validation/placement-signal-01"
source = root / "runs/route-03-placement.kicad_pcb"
output = root / "runs/route-verifier-04-signals.kicad_pcb"
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
inputs = [source, source.with_suffix(".kicad_pro"), TOOLS/"route_completion.py", TOOLS/"proj.py", TOOLS/"verification_worker.py", TOOLS/"design.py", TOOLS/"gen_sch.py", Path(__file__)]
source_hashes = {str(p):sha(p) for p in inputs}
commands = []
def run(label,args):
    begun=time.time()
    p=subprocess.run(args,cwd=TOOLS,capture_output=True,text=True)
    (outdir/(label+".stdout.log")).write_text(p.stdout)
    (outdir/(label+".stderr.log")).write_text(p.stderr)
    commands.append({"label":label,"command":args,"returncode":p.returncode,"seconds":time.time()-begun})
    print(label,"exit",p.returncode,flush=True)
    if p.stdout:print(p.stdout[-12000:],flush=True)
    if p.stderr:print(p.stderr[-3000:],flush=True)
    return p
for stage in ("before","after"):(outdir/stage).mkdir()
before = run("before-board", [sys.executable,str(TOOLS/"verification_worker.py"),"board",str(source),str(outdir/"before/model.json"),"--drc",str(outdir/"before/drc-stored.txt")])
if before.returncode:raise SystemExit(before.returncode)
routing = run("routing",[sys.executable,str(TOOLS/"route_completion.py"),str(source),str(output),"2"])
if not output.is_file():raise SystemExit(2)
after = run("after-board", [sys.executable,str(TOOLS/"verification_worker.py"),"board",str(output),str(outdir/"after/model.json"),"--drc",str(outdir/"after/drc-stored.txt")])
if after.returncode:raise SystemExit(after.returncode)
a=parse_drc((outdir/"before/drc-refilled.txt").read_text())
b=parse_drc((outdir/"after/drc-refilled.txt").read_text())
old={f["fingerprint"] for f in a["findings"] if f["section"]!="unconnected pads"}
new=[f for f in b["findings"] if f["section"]!="unconnected pads" and f["fingerprint"] not in old]
model=json.loads((outdir/"after/model.json").read_text())
unchanged=all(sha(Path(p))==h for p,h in source_hashes.items())
result={"schema_version":1,"input":str(source),"output":str(output),"input_sha256":sha(source),"output_sha256":sha(output),"source_inputs_sha256":source_hashes,"sources_unchanged":unchanged,"environment":{"python":sys.version,"container_image":os.environ["SORX_IMAGE_ID"]},"commands":commands,"before_counts":a["counts"],"after_counts":b["counts"],"new_non_connectivity_findings":new,"board_model":model,"routing_exit_code":routing.returncode,"fabrication_approval":False}
(outdir/"result.json").write_text(json.dumps(result,indent=2))
print(json.dumps({k:result[k] for k in ("before_counts","after_counts","sources_unchanged")},indent=2))
print("new other findings",len(new),"model mismatches",len(model["model_mismatches"]),flush=True)
raise SystemExit(2 if new or not unchanged or "Traceback" in routing.stderr else 1 if b["counts"]["unconnected pads"] or model["model_mismatches"] else 0)

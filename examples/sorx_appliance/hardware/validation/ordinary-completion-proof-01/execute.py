import hashlib,json,os,subprocess,sys,time
from pathlib import Path
root=Path("/work/hardware");tools=root/"tools";sys.path.insert(0,str(tools))
from verification_core import parse_drc
source=root/"runs/route-combined-15b-ground.kicad_pcb";output=root/"runs/route-verifier-21-ordinary-proof.kicad_pcb";out=Path(__file__).resolve().parent
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
inputs=[source,source.with_suffix(".kicad_pro"),Path(__file__)]+[tools/n for n in ("route_completion.py","proj.py","verification_core.py","replay_copper_delta.py")]
bound={str(p):sha(p) for p in inputs};commands=[]
if output.exists():raise ValueError("Use a fresh candidate")
def run(label,args):
 start=time.time();p=subprocess.run(args,cwd=tools,capture_output=True,text=True);(out/(label+".stdout.log")).write_text(p.stdout);(out/(label+".stderr.log")).write_text(p.stderr);commands.append(dict(label=label,command=args,returncode=p.returncode,seconds=time.time()-start));print(label,p.returncode,flush=True)
 if p.stdout:print(p.stdout[-9000:],flush=True)
 if p.stderr:print(p.stderr[-2000:],flush=True)
 return p
code="import pcbnew,json,sys;from pathlib import Path;from replay_copper_delta import inventory,copper_differences;b=pcbnew.LoadBoard(sys.argv[1]);a=inventory(b);pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.WriteDRCReport(b,sys.argv[2],pcbnew.EDA_UNITS_MILLIMETRES,True);Path(sys.argv[3]).write_text(json.dumps({'postfill_changes':copper_differences(a,inventory(b)),'kicad':pcbnew.Version()}))"
before=run("before",[sys.executable,"-c",code,str(source),str(out/"before.txt"),str(out/"before.json")])
if before.returncode:raise SystemExit(2)
routing=run("routing",[sys.executable,str(tools/"route_completion.py"),str(source),str(output),"2"])
if routing.returncode not in (0,1) or not output.is_file():raise SystemExit(2)
after=run("after",[sys.executable,"-c",code,str(output),str(out/"after.txt"),str(out/"after.json")])
if after.returncode:raise SystemExit(2)
a=parse_drc((out/"before.txt").read_text());b=parse_drc((out/"after.txt").read_text());old={f["fingerprint"] for f in a["findings"] if f["section"]!="unconnected pads"};new=[f for f in b["findings"] if f["section"]!="unconnected pads" and f["fingerprint"] not in old]
fill_before=json.loads((out/"before.json").read_text());fill_after=json.loads((out/"after.json").read_text());unchanged=all(sha(Path(p))==h for p,h in bound.items());accepted=unchanged and not new and not fill_before["postfill_changes"] and not fill_after["postfill_changes"] and b["counts"]["unconnected pads"]<a["counts"]["unconnected pads"]
r=dict(source=str(source),source_sha256=sha(source),output=str(output),output_sha256=sha(output),source_inputs_sha256=bound,sources_unchanged=unchanged,commands=commands,before_counts=a["counts"],after_counts=b["counts"],new_non_connectivity_findings=new,fill_before=fill_before,fill_after=fill_after,accepted_for_further_development=accepted,fabrication_approval=False,environment={"container_image":os.environ["SORX_IMAGE_ID"],"KICAD7_FOOTPRINT_DIR":os.environ["KICAD7_FOOTPRINT_DIR"]})
(out/"result.json").write_text(json.dumps(r,indent=2)+"\n");print(json.dumps({k:r[k] for k in ("before_counts","after_counts","accepted_for_further_development")},indent=2),flush=True);raise SystemExit(0 if accepted else 2)

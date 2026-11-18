from pathlib import Path
import subprocess,json,hashlib,tempfile,shutil,datetime
root=Path('/hardware');out=root/'validation/electrical/generation';out.mkdir(parents=True,exist_ok=True)
records=[]
def run(label,cmd,cwd=root,stdout_file=None):
 start=datetime.datetime.now(datetime.timezone.utc).isoformat();r=subprocess.run(cmd,cwd=cwd,capture_output=True,text=True)
 (out/(label+'.stdout.txt')).write_text(r.stdout,encoding='utf-8');(out/(label+'.stderr.txt')).write_text(r.stderr,encoding='utf-8')
 records.append(dict(label=label,command=cmd,cwd=str(cwd),started_utc=start,returncode=r.returncode));print(label,r.returncode,flush=True)
 if stdout_file is not None and r.returncode==0:stdout_file.write_text(r.stdout,encoding='utf-8')
 return r.returncode
before=hashlib.sha256((root/'hw/sorx.kicad_pcb').read_bytes()).hexdigest()
for name,cmd in [('schematic',['python3','tools/gen_sch.py']),('bom',['python3','tools/gen_bom.py'])]:
 if run(name,cmd):raise RuntimeError(name+' failed')
if run('power-budget',['python3','tools/power_budget.py'],stdout_file=root/'docs/power-budget.md'):raise RuntimeError('power budget failed')
if run('netlist',['kicad-cli','sch','export','netlist','--format','kicadsexpr','-o',str(root/'hw/sorx.net'),str(root/'hw/sorx.kicad_sch')]):raise RuntimeError('netlist failed')
with tempfile.TemporaryDirectory(prefix='sorx-codex-electrical-erc-') as d:
 scratch=Path(d);shutil.copytree(root/'hw',scratch/'hw');erc=scratch/'erc';erc.mkdir()
 run('erc',['xvfb-run','-a','eeschema_do','run_erc',str(scratch/'hw/sorx.kicad_sch'),str(erc)])
 if (erc/'sorx.erc').exists():shutil.copy2(erc/'sorx.erc',out/'sorx.erc')
 else:raise RuntimeError('ERC did not produce fresh report')
 run('netlist-parity',['python3','tools/verification_worker.py','netlist',str(root/'hw/sorx.net'),str(out/'netlist-parity.json')])
after=hashlib.sha256((root/'hw/sorx.kicad_pcb').read_bytes()).hexdigest()
files=sorted((root/'hw').glob('*.kicad_sch'))+[root/'hw/sorx.kicad_sym',root/'hw/sorx.net',root/'tools/netmap.json',root/'docs/power-budget.md']+sorted((root/'bom').glob('*.csv'))
manifest=dict(image_identity='sha256:23dbab2af8391fba7496a546d6729c37a5e8bdfb8e941ab903589252e8bec1e2',commands=records,pcb_hash_before=before,pcb_hash_after=after,pcb_unchanged=before==after,artifacts={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
assert before==after,'PCB changed concurrently or unexpectedly'
assert not any(r['returncode'] for r in records), 'At least one validation failed; see raw reports'

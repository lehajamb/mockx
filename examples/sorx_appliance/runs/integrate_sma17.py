import subprocess,json,hashlib,shutil,sys
from pathlib import Path
root=Path('/hardware');dest=root/'runs/electrical-final17';dest.mkdir();source=root/'runs/route-electrical-16d-headers.kicad_pcb'
if hashlib.sha256(source.read_bytes()).hexdigest()!='4a9d59f1d3292ec9f2004aeffd04bc6b1c7f66bcfa97734081f5ddb669ad555f':raise ValueError('Unexpected16d')
newsource=dest/'source16d.kicad_pcb';shutil.copy2(source,newsource)
for s in('.kicad_pro','.kicad_dru'):
 if source.with_suffix(s).exists():shutil.copy2(source.with_suffix(s),newsource.with_suffix(s))
(dest/'fp-lib-table').write_text('(fp_lib_table (lib (name "sorx")(type "KiCad")(uri "/hardware/hw/sorx.pretty")(options "")(descr "SORX custom footprints")))\n')
output=dest/'route-electrical-17-sma.kicad_pcb';evidence=root/'validation/electrical-integration-17-sma'
command=[sys.executable,'tools/apply_sma_land.py',str(newsource),str(output),str(evidence)]
r=subprocess.run(command,cwd=root,capture_output=True,text=True)
if evidence.exists():(evidence/'execution.log').write_text(r.stdout+r.stderr)
print('SMA returncode',r.returncode,flush=True)
if r.returncode:print(r.stdout+r.stderr);raise SystemExit(r.returncode)
d=json.loads((evidence/'result.json').read_text());print(d['before'],d['after'],d['output_sha256'],flush=True)

"""Reproduce the immutable submitted design in scratch; never use stale outputs."""
import hashlib,json,os,subprocess,sys,tempfile,zipfile
from pathlib import Path
AP=Path(__file__).resolve().parents[2]
OUT=AP/'hardware/validation/baseline-codex'
ARCHIVE=AP/'baseline/sorx-hw-2026-11-16-final.zip'
EXPECTED='2f51a36928e87cb51dcb15f3fa5f04b5b789473a67bb6b2b4529f671bc91addd'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    assert sha(ARCHIVE)==EXPECTED
    OUT.mkdir(parents=True,exist_ok=True)
    if (OUT/'run.json').exists(): raise RuntimeError('Baseline run already exists; choose a new explicit run directory')
    record={'archive_sha256':EXPECTED,'tool_image_id':(AP/'hardware/tools/env/image-id.txt').read_text().strip(),'checks':[],'hardware_ready':False}
    def run(name,args,cwd):
        cp=subprocess.run(args,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        (OUT/(name+'.log')).write_text(cp.stdout)
        record['checks'].append({'name':name,'argv':args,'exit_code':cp.returncode,'log_sha256':sha(OUT/(name+'.log'))})
        print(name,'exit',cp.returncode,flush=True)
        if cp.returncode: raise RuntimeError(name+' failed; no output accepted')
    try:
      with tempfile.TemporaryDirectory(prefix='sorx-codex-baseline-') as scratch:
        with zipfile.ZipFile(ARCHIVE) as z:
          for m in z.infolist():
            target=Path(scratch,m.filename).resolve()
            if not target.is_relative_to(Path(scratch)) or m.filename.startswith('/') or '..' in Path(m.filename).parts: raise ValueError('Unsafe ZIP path')
          z.extractall(scratch)
        h=Path(scratch)/'sorx-hw'; t=h/'tools'
        drc_code="import pcbnew,json; from pathlib import Path; b=pcbnew.LoadBoard('sorx.kicad_pcb'); o=Path(sys.argv[1]); pcbnew.WriteDRCReport(b,str(o/'drc-stored.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True); pcbnew.ZONE_FILLER(b).Fill(b.Zones()); pcbnew.WriteDRCReport(b,str(o/'drc-refilled.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True); (o/'inventory.json').write_text(json.dumps({'footprints':len(list(b.GetFootprints())),'pads':sum(len(list(f.Pads())) for f in b.GetFootprints()),'named_nets':sum(bool(str(n)) for n in b.GetNetsByName()),'segments':sum(x.GetClass()!='PCB_VIA' for x in b.GetTracks()),'vias':sum(x.GetClass()=='PCB_VIA' for x in b.GetTracks())},indent=2))"
        run('drc',['python3','-c','import sys; '+drc_code,str(OUT)],h/'hw')
        erc=Path(scratch)/'erc'; erc.mkdir()
        run('erc',['xvfb-run','-a','eeschema_do','run_erc',str(h/'hw/sorx.kicad_sch'),str(erc)],t)
        erc_report=erc/'sorx.erc'
        if not erc_report.is_file(): raise RuntimeError('Missing fresh ERC output')
        (OUT/'erc-report.txt').write_bytes(erc_report.read_bytes())
        run('netlist-parity',['python3','check_netlist.py'],t)
        run('pairs',['python3','pairs.py',str(h/'hw/sorx.kicad_pcb')],t)
        run('mech-export',['python3','mech_export.py',str(h/'hw/sorx.kicad_pcb'),str(h/'hw/build/mech_parts.json')],t)
        fit=h/'mech/fit-check.json'; fit.unlink()
        run('mechanical',['/opt/cq/bin/python','gen_mech.py'],t)
        if not fit.is_file(): raise RuntimeError('Missing fresh fit output')
        (OUT/'fit-check.json').write_bytes(fit.read_bytes())
        run('system-packages',['dpkg-query','-W'],t)
        run('python-packages',['python3','-m','pip','freeze'],t)
        run('cadquery-packages',['/opt/cq/bin/python','-m','pip','freeze'],t)
        record['execution_success']=True
    except Exception as exc:
      record['execution_success']=False; record['error']=str(exc)
    record['outputs']={p.name:sha(p) for p in OUT.iterdir() if p.is_file() and p.name!='run.json'}
    (OUT/'run.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps({'execution_success':record['execution_success'],'hardware_ready':False}))
    return 0 if record['execution_success'] else 1
if __name__=='__main__': sys.exit(main())

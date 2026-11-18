#!/usr/bin/env python3
"""Correct the reproduced H7/U12 collision and TP21 overlap without full sync.

Exact removed copper UUIDs and changed pad coordinates are retained. Routing is
intentionally a separate step; an intermediate board is not a completed design.
"""
import argparse,hashlib,json,re,shutil,subprocess,sys
from pathlib import Path
PLACEMENT={'U12':(30,55.5,0,'B'),'C76':(30.75,58.5,90,'B'),'TP21':(17.5,28,0,'F')}
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 import pcbnew
 from sync_codex_board import pad_shape,trace_shape
 from audit_npth import audit
 from verification_core import parse_drc
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('source',type=Path);ap.add_argument('output',type=Path);ap.add_argument('evidence',type=Path);a=ap.parse_args()
 if a.output.exists() or a.evidence.exists():raise ValueError('Use fresh output/evidence paths')
 a.evidence.mkdir(parents=True);a.output.parent.mkdir(parents=True,exist_ok=True)
 b=pcbnew.LoadBoard(str(a.source));pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.WriteDRCReport(b,str(a.evidence/'before-drc.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True)
 before=parse_drc((a.evidence/'before-drc.txt').read_text());before_npth=audit(a.source)
 (a.evidence/'before-npth.json').write_text(json.dumps(before_npth,indent=2)+'\n')
 if not any(x['hole']['ref']=='H7' and x['component_pad']['ref']=='U12' and x['component_pad']['pad']=='5' and x['physical_overlap_mm2']>0 for x in before_npth['other_component_findings']):raise ValueError('Expected baseline H7/U12.5 collision not reproduced')
 tracks=list(b.GetTracks());kill={};changes=[]
 for ref,(x,y,angle,side) in PLACEMENT.items():
  f=b.FindFootprintByReference(ref)
  if not f:raise ValueError('Missing '+ref)
  old=dict(xy=[f.GetPosition().x/1e6,f.GetPosition().y/1e6],angle=f.GetOrientationDegrees(),side=b.GetLayerName(f.GetLayer()))
  for pad in f.Pads():
   shape=pad_shape(pad)
   for t in tracks:
    if t.GetNetname()==pad.GetNetname() and t.GetNetname() and (t.GetClass()=='PCB_VIA' or pad.IsOnLayer(t.GetLayer())) and trace_shape(t).intersects(shape):
     uid=t.m_Uuid.AsString();kill[uid]=dict(uuid=uid,net=t.GetNetname(),reference=ref,pad=pad.GetNumber(),reason='copper attached to relocated pad')
  if (f.GetLayer()==pcbnew.B_Cu)!=(side=='B'):raise ValueError('Unexpected side; no silent flipping')
  f.SetOrientationDegrees(angle);f.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x),pcbnew.FromMM(y)));f.Reference().SetPosition(f.GetPosition())
  changes.append(dict(reference=ref,before=old,after=dict(xy=[x,y],angle=angle,side=side)))
 pcbnew.SaveBoard(str(a.output),b)
 lines=[];removed=[]
 for line in a.output.read_text().splitlines(keepends=True):
  m=re.search(r'\(tstamp ([0-9a-f-]+)\)',line)
  if re.match(r'^\s*\((segment|via) ',line) and m and m.group(1) in kill:removed.append(m.group(1));continue
  lines.append(line)
 if set(removed)!=set(kill):raise ValueError('Not all exact copper UUIDs were removed')
 a.output.write_text(''.join(lines));
 for suffix in ('.kicad_pro','.kicad_dru'):
  if a.source.with_suffix(suffix).exists():shutil.copy2(a.source.with_suffix(suffix),a.output.with_suffix(suffix))
 # Fresh process avoids retained SWIG objects after mutation/removal.
 subprocess.run([sys.executable,__file__,'--finalize',str(a.output),str(a.evidence)],check=True)
 after=parse_drc((a.evidence/'after-drc.txt').read_text());after_npth=json.loads((a.evidence/'after-npth.json').read_text())
 old_other={f['fingerprint'] for f in before['findings'] if f['section']!='unconnected pads'}
 new_other=[f for f in after['findings'] if f['section']!='unconnected pads' and f['fingerprint'] not in old_other]
 accepted=not after_npth['other_component_findings'] and not new_other
 result=dict(status='PLACEMENT CORRECTED; DETACHED NETS REQUIRE ROUTING' if accepted else 'REJECTED',fabrication_ready=False,source=str(a.source),source_sha256=sha(a.source),output=str(a.output),output_sha256=sha(a.output),tool_sha256=sha(__file__),placements=changes,removed_copper=[kill[u] for u in removed],before=before['counts'],after=after['counts'],new_other_findings=new_other)
 (a.evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:result[k] for k in ('status','before','after')}),flush=True)
 return 0 if accepted else 1
if __name__=='__main__':
 if len(sys.argv)==4 and sys.argv[1]=='--finalize':
  import pcbnew
  from audit_npth import audit
  path=Path(sys.argv[2]);evidence=Path(sys.argv[3]);board=pcbnew.LoadBoard(str(path));pcbnew.ZONE_FILLER(board).Fill(board.Zones());pcbnew.SaveBoard(str(path),board);pcbnew.WriteDRCReport(board,str(evidence/'after-drc.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True)
  (evidence/'after-npth.json').write_text(json.dumps(audit(path),indent=2)+'\n')
 else:raise SystemExit(main())

#!/usr/bin/env python3
"""Install exact service-header lands, preserving copper centers except reviewed JP2 clearance move."""
import argparse,hashlib,json,shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def replace(path,ref,move):
 import pcbnew
 from design import DESIGN
 b=pcbnew.LoadBoard(str(path));old=b.FindFootprintByReference(ref);part=DESIGN.parts[ref]
 oldpads={p.GetNumber():dict(net=p.GetNetname(),xy=[p.GetPosition().x/1e6,p.GetPosition().y/1e6])for p in old.Pads()}
 x,y=old.GetPosition().x/1e6,old.GetPosition().y/1e6;angle=old.GetOrientationDegrees()
 if old.GetLayer()!=pcbnew.F_Cu:raise ValueError('Unexpected header side')
 new=pcbnew.FootprintLoad(str(ROOT/'hw/sorx.pretty'),part.fp.split(':',1)[1]);new.SetReference(ref);new.SetFPID(pcbnew.LIB_ID(*part.fp.split(':',1)));new.SetValue(part.value)
 new.SetOrientationDegrees(angle);new.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(move if ref=='JP2'and move is not None else x),pcbnew.FromMM(y)))
 b.Remove(old);b.Add(new)
 for p in new.Pads():
  previous=oldpads[p.GetNumber()];p.SetNet(b.GetNetsByName()[previous['net']]);xy=[p.GetPosition().x/1e6,p.GetPosition().y/1e6]
  if xy!=previous['xy']:
   if ref!='JP2'or move is None:raise ValueError('Unexpected center movement')
   track=pcbnew.PCB_TRACK(b);track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(previous['xy'][0]),pcbnew.FromMM(previous['xy'][1])));track.SetEnd(p.GetPosition());track.SetWidth(pcbnew.FromMM(.25));track.SetLayer(pcbnew.F_Cu);track.SetNet(p.GetNet());b.Add(track)
 new.Reference().SetLayer(pcbnew.F_Fab);new.Reference().SetPosition(new.GetPosition());new.Value().SetVisible(False)
 pcbnew.SaveBoard(str(path),b)
def report(path,target):
 import pcbnew
 scratch=target.parent/(target.stem+'-project');scratch.mkdir()
 candidate=scratch/path.name;shutil.copy2(path,candidate)
 for suffix in('.kicad_pro','.kicad_dru'):
  if path.with_suffix(suffix).exists():shutil.copy2(path.with_suffix(suffix),candidate.with_suffix(suffix))
 (scratch/'fp-lib-table').write_text('(fp_lib_table (lib (name "sorx")(type "KiCad")(uri "'+str(ROOT/'hw/sorx.pretty')+'")(options "")(descr "SORX custom footprints")))\n')
 b=pcbnew.LoadBoard(str(candidate));pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.WriteDRCReport(b,str(target),pcbnew.EDA_UNITS_MILLIMETRES,True);pcbnew.SaveBoard(str(path),b)
def main():
 from verification_core import parse_drc
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('source',type=Path);ap.add_argument('output',type=Path);ap.add_argument('evidence',type=Path);ap.add_argument('--jp2-x',type=float);a=ap.parse_args()
 if a.output.exists()or a.evidence.exists():raise ValueError('Fresh output/evidence paths required')
 a.evidence.mkdir(parents=True);shutil.copy2(a.source,a.output)
 for suffix in('.kicad_pro','.kicad_dru'):
  if a.source.with_suffix(suffix).exists():shutil.copy2(a.source.with_suffix(suffix),a.output.with_suffix(suffix))
 # Report scratch copy, never rewrite the input candidate.
 subprocess.run([sys.executable,__file__,'--report',str(a.output),str(a.evidence/'before-drc.txt')],check=True)
 for ref in('JP1','JP2','J14'):subprocess.run([sys.executable,__file__,'--replace',str(a.output),ref,str(a.jp2_x)],check=True)
 subprocess.run([sys.executable,__file__,'--report',str(a.output),str(a.evidence/'after-drc.txt')],check=True)
 before=parse_drc((a.evidence/'before-drc.txt').read_text());after=parse_drc((a.evidence/'after-drc.txt').read_text());keys={x['fingerprint']for x in before['findings']if x['section']!='unconnected pads'};new=[x for x in after['findings']if x['section']!='unconnected pads'and x['fingerprint']not in keys];accepted=not new and after['counts']['unconnected pads']<=before['counts']['unconnected pads']
 result=dict(status='ACCEPTED FOR FURTHER DEVELOPMENT'if accepted else'REJECTED',source=str(a.source),source_sha256=sha(a.source),output=str(a.output),output_sha256=sha(a.output),tool_sha256=sha(__file__),jp2_x=a.jp2_x,finished_hole_mm=1.2,required_finished_hole_tolerance_mm=.05,before=before['counts'],after=after['counts'],new_other_findings=new,fabrication_ready=False)
 (a.evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:result[k]for k in('status','before','after')}),flush=True);return 0 if accepted else 1
if __name__=='__main__':
 if len(sys.argv)==5 and sys.argv[1]=='--replace':replace(Path(sys.argv[2]),sys.argv[3],None if sys.argv[4]=='None'else float(sys.argv[4]))
 elif len(sys.argv)==4 and sys.argv[1]=='--report':report(Path(sys.argv[2]),Path(sys.argv[3]))
 else:raise SystemExit(main())

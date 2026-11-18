#!/usr/bin/env python3
"""Apply one measured PCIe RX_N matching detour; native geometry gates remain mandatory."""
import argparse,hashlib,json,math,re,shutil,subprocess,sys
from pathlib import Path
import pcbnew
from shapely.geometry import LineString
import high_speed_audit as hs
import route_completion as router
from correct_high_speed import add_track,run_audit
from verification_core import parse_drc
from pcie_candidate_checks import assess_pcie_rx_candidate
UUID='0c7c585c-0401-463e-9193-db956eb27f3c'

def worker(plan):
 b=pcbnew.LoadBoard(plan['filtered'])
 for a,c in zip(plan['points'],plan['points'][1:]):add_track(b,'PCIE_RX_N',pcbnew.F_Cu,a,c,.155)
 pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.SaveBoard(plan['output'],b)
 if not pcbnew.WriteDRCReport(b,plan['after_drc'],pcbnew.EDA_UNITS_MILLIMETRES,True):raise RuntimeError('Native DRC failed')

def main():
 p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('output',type=Path,nargs='?');p.add_argument('--evidence',type=Path);p.add_argument('--worker',action='store_true');a=p.parse_args()
 if a.worker:worker(json.loads(a.source.read_text()));return 0
 if a.source.resolve()==a.output.resolve() or a.output.exists():raise ValueError('Require fresh isolated output')
 a.evidence.mkdir(parents=True,exist_ok=False);digest=hashlib.sha256(a.source.read_bytes()).hexdigest();before=run_audit(a.source,a.evidence/'before.json');b=pcbnew.LoadBoard(str(a.source))
 matches=[t for t in b.GetTracks() if str(t.m_Uuid.AsString())==UUID]
 if len(matches)!=1:raise ValueError('Exact original RX segment not present')
 t=matches[0];start=(t.GetStart().x/1e6,t.GetStart().y/1e6);end=(t.GetEnd().x/1e6,t.GetEnd().y/1e6)
 if t.GetNetname()!='PCIE_RX_N' or t.GetLayer()!=pcbnew.F_Cu or start!=(48.8517,56.2713) or end!=(45.569,56.2713) or t.GetWidth()!=155000:raise ValueError('Original matching region changed')
 amp=.90/(2*(math.sqrt(2)-1));points=[start,(start[0]-.35,start[1]),(start[0]-.35-amp,start[1]-amp),(end[0]+.35+amp,end[1]-amp),(end[0]+.35,end[1]),end]
 line=LineString(points);grid=router.Grid(b);grid.load(b)
 for net,items in grid.items.items():
  if net=='PCIE_RX_N':continue
  for layers,shape,hole,_ in items:
   if 'F' in layers and line.distance(shape)<.155/2+.18+.01+(router.HOLE_EXTRA if hole else 0):raise ValueError('New matching clearance conflict '+net)
 if not pcbnew.WriteDRCReport(b,str(a.evidence/'before-drc.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True):raise RuntimeError('Native before DRC failed')
 lines=a.source.read_text().splitlines(True);removed=[s for s in lines if s.lstrip().startswith('(segment ') and UUID in s]
 if len(removed)!=1:raise ValueError('Unsupported exact source serialization')
 filtered=a.evidence/'filtered.kicad_pcb';filtered.write_text(''.join(s for s in lines if s not in removed),encoding='utf-8')
 for dest in [filtered,a.output]:shutil.copy2(a.source.with_suffix('.kicad_pro'),dest.with_suffix('.kicad_pro'))
 for parent in [a.evidence,a.output.parent]:
  table=a.source.parent/'fp-lib-table'
  if table.exists() and (parent/'fp-lib-table').resolve()!=table.resolve():shutil.copy2(table,parent/'fp-lib-table')
 plan={'source_sha256':digest,'filtered':str(filtered),'output':str(a.output),'points':points,'removed':removed,'added_planar_mm':line.length-math.dist(start,end),'after_drc':str(a.evidence/'after-drc.txt')};(a.evidence/'plan.json').write_text(hs.report_json(plan),encoding='utf-8')
 r=subprocess.run([sys.executable,__file__,str(a.evidence/'plan.json'),'--worker'],capture_output=True,text=True);(a.evidence/'worker.log').write_text(r.stdout+r.stderr,encoding='utf-8')
 if r.returncode:raise RuntimeError('Fresh geometry worker failed: '+str(r.returncode))
 after=run_audit(a.output,a.evidence/'after.json');pairs=[q for q in after['pairs'] if q['name']=='PCIE_RX'];fail=[]
 if after['errors'] or len(pairs)!=1:fail.append('Invalid geometry extraction')
 for q in pairs:
  if q['errors']:fail+=q['errors']
  for flag in ['planar_length_gate_pass','via_symmetry_pass','via_budget_pass','layer_sequence_symmetry_pass']:
   if q.get(flag) is not True:fail.append(flag)
  for pol in ['p','n']:
   v=q.get(pol,{}).get('off_path_track_length_mm')
   if type(v) not in [int,float] or not math.isfinite(v) or not 0<=v<=.01:fail.append('Incomplete/branched '+pol)
 native=parse_drc((a.evidence/'after-drc.txt').read_text());prior=parse_drc((a.evidence/'before-drc.txt').read_text());keys={f['fingerprint'] for f in prior['findings'] if f['section']!='unconnected pads'}
 new=[f for f in native['findings'] if f['section']!='unconnected pads' and f['fingerprint'] not in keys]
 if new:fail.append('New native non-open findings')
 target=[f for f in native['findings'] if any(n in f['objects'] for n in ['PCIE_RX_','OE_PET_']) and ('Severity: error' in f['objects'] or f['section']=='unconnected pads')]
 if target:fail.append('Native target errors')
 checks=assess_pcie_rx_candidate(after,(a.evidence/'after-drc.txt').read_text(),(a.evidence/'before-drc.txt').read_text());fail.extend(checks['failures'])
 result={'targeted_geometric_checks_pass':not fail,'fabrication_ready':False,'source_sha256':digest,'output_sha256':hashlib.sha256(a.output.read_bytes()).hexdigest(),'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'before_native':prior['counts'],'after_native':native['counts'],'new_native_findings':new,'failures':fail,'before_skew_mm':next(q['planar_skew_mm'] for q in before['pairs'] if q['name']=='PCIE_RX'),'after_skew_mm':pairs[0].get('planar_skew_mm') if len(pairs)==1 else None,'unqualified':['impedance and propagation-delay prescription','remaining coupling and reference discontinuities','whole-board release gates']}
 if hashlib.sha256(a.source.read_bytes()).hexdigest()!=digest:raise ValueError('Source changed')
 (a.evidence/'result.json').write_text(hs.report_json(result),encoding='utf-8');print(hs.report_json(result));return 0 if not fail else 1
if __name__=='__main__':raise SystemExit(main())

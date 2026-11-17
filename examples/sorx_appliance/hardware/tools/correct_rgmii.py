#!/usr/bin/env python3
"""Route the previously absent MG_TXD0 in an isolated candidate; no release claim."""
import argparse,hashlib,json,shutil
import numpy as np
from PIL import Image,ImageDraw
from pathlib import Path
import pcbnew
from shapely.geometry import LineString,box
import route_completion as router
import rgmii_audit
import high_speed_audit as hs
from correct_high_speed import add_track,pad_map,xy
from verification_core import parse_drc

def main():
 p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('output',type=Path);p.add_argument('--evidence',type=Path,required=True);a=p.parse_args()
 if a.source.resolve()==a.output.resolve() or a.output.exists():raise ValueError('Require new isolated output')
 a.evidence.mkdir(parents=True,exist_ok=False);digest=hashlib.sha256(a.source.read_bytes()).hexdigest();b=pcbnew.LoadBoard(str(a.source));pads=pad_map(b);net='MG_TXD0'
 if any(t.GetNetname()==net for t in b.GetTracks()):raise ValueError('Requalify existing MG_TXD0 copper before running missing-route repair')
 if abs(xy(pads['J1:221'])[0]-54.3695)>.001 or abs(xy(pads['U13:19'])[1]-39.75)>.001:raise ValueError('Changed interface anchors')
 pcbnew.ZONE_FILLER(b).Fill(b.Zones())
 if not pcbnew.WriteDRCReport(b,str(a.evidence/'before-drc.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True):raise RuntimeError('Native before DRC failed')
 before=rgmii_audit.audit(a.source);(a.evidence/'before.json').write_text(hs.report_json(before),encoding='utf-8')
 records=[]
 for net,module,phy in [('MG_TXD0','J1:221','U13:19')]:
  router.W_TRK=.15;router.CLR=.15;router.EPS=.01
  grid=router.Grid(b);grid.load(b);anchors=[]
  for endpoint in [module,phy]:
   result=grid.route(net,xy(pads[endpoint]),'F',via_goal=True,via_forbidden=box(-100,50.8,200,200) if endpoint.startswith('J1:') else (box(65.9,-100,200,200).union(box(-100,39.15,200,200)) if net=='MG_TXD0' else None))
   if result is None or len(result[1])!=1 or any(layer!='F' for layer,points in result[0]):raise ValueError('No one-via RGMII escape '+endpoint)
   router.commit(b,grid,net,b.GetNetsByName()[net],result[0],result[1]);anchors.append(result[1][0]);records.append({'net':net,'endpoint':endpoint,'route':result});print(json.dumps(records[-1]),flush=True)
  for v in grid.V.values():v.fill(60000)
  for layer in ['C','F']:grid.T[layer].fill(60000)
  pcbnew.SaveBoard(str(a.evidence/('anchors-'+net+'.kicad_pcb')),b)
  (a.evidence/('anchors-'+net+'.json')).write_text(json.dumps(anchors))
  result=grid.route(net,anchors[0],'B',anchors[1],'B')
  print('C-result',net,result,flush=True)
  if result is None or result[1] or any(layer!='B' for layer,points in result[0]):raise ValueError('No legal internal RGMII data route '+net)
  for layer,points in result[0]:
   for c,d in zip(points,points[1:]):add_track(b,net,pcbnew.B_Cu,c,d,router.W_TRK)
  records.append({'net':net,'trunk':result})
 router.W_TRK=.25;router.CLR=.15;router.EPS=.01
 powergrid=router.Grid(b);powergrid.load(b)
 for v in powergrid.V.values():v.fill(60000)
 for layer in ['B','C']:powergrid.T[layer].fill(60000)
 power=powergrid.route('+1V2_PHY',xy(pads['U13:18']),'F',(64.8381,39.3479),'F')
 if power is None or power[1] or any(layer!='F' for layer,points in power[0]):raise ValueError('No legal width-preserving PHY supply detour')
 for layer,points in power[0]:
  for c,d in zip(points,points[1:]):add_track(b,'+1V2_PHY',pcbnew.F_Cu,c,d,.25)
 records.append({'supply_detour':power,'width_mm':.25,'planning_current_A':.30})
 net='MG_TXD0'
 pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.SaveBoard(str(a.output),b);shutil.copy2(a.source.with_suffix('.kicad_pro'),a.output.with_suffix('.kicad_pro'))
 if not pcbnew.WriteDRCReport(b,str(a.evidence/'after-drc.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True):raise RuntimeError('Native after DRC failed')
 after=rgmii_audit.audit(a.output);(a.evidence/'after.json').write_text(hs.report_json(after),encoding='utf-8')
 path=after['groups']['TX']['paths'].get(net);clock=after['groups']['TX']['paths']['MG_TXC']['planar_length_mm'];drc=parse_drc((a.evidence/'after-drc.txt').read_text());target=[f for f in drc['findings'] if net in f['objects']]
 summary={'fabrication_ready':False,'source_sha256':digest,'output_sha256':hashlib.sha256(a.output.read_bytes()).hexdigest(),'target_path':path,'clock_planar_difference_mm':abs(path['planar_length_mm']-clock) if path else None,'target_native_findings':target,'routes':records}
 (a.evidence/'result.json').write_text(hs.report_json(summary),encoding='utf-8');print(json.dumps({k:v for k,v in summary.items() if k not in ['target_path','routes']},indent=2))
 if hashlib.sha256(a.source.read_bytes()).hexdigest()!=digest:raise ValueError('Source mutated')
 return 0 if path and not target and path['off_path_track_length_mm']<=.01 and summary['clock_planar_difference_mm']<=1.27 else 1
if __name__=='__main__':raise SystemExit(main())

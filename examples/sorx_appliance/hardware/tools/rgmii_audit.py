#!/usr/bin/env python3
"""Audit actual MG RGMII paths; planar screening is not full timing qualification."""
import argparse,hashlib,json,math
from pathlib import Path
import high_speed_audit as hs
SOURCE='https://ww1.microchip.com/downloads/aemDocuments/documents/UNG/ProductDocuments/SupportingCollateral/KSZ9131RNXReferenceDesign.pdf'
GROUPS={'TX':['MG_TXC','MG_TXD0','MG_TXD1','MG_TXD2','MG_TXD3','MG_TX_CTL'],'RX':['MG_RXC','MG_RXD0','MG_RXD1','MG_RXD2','MG_RXD3','MG_RX_CTL']}

def audit(path):
 g=hs.load_geometry(path);out={'source_pcb_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),'fabrication_ready':False,'errors':g['errors'],'groups':{},'source':SOURCE,'reference_design_screen_mm':1.27,'qualification':'Reference design page4 specifies50mil group matching. This carrier-only planar screen excludes module-internal lengths, delay settings, package/via delay, impedance and SI; qualify the complete Verdin timing budget.'}
 for name,nets in GROUPS.items():
  group={'paths':{},'errors':[]}
  for net in nets:
   pads=[p for p in g['pads'] if p['net']==net];ends=[[p for p in pads if p['ref']==ref] for ref in ['J1','U13']]
   try:
    if any(len(e)!=1 for e in ends):raise ValueError('Expected one module and one PHY pin '+net)
    graph=hs.RouteGraph([t for t in g['tracks'] if t['net']==net],pads,[v for v in g['vias'] if v['net']==net]);group['errors']+=graph.errors
    route=graph.route(ends[0][0]['id'],ends[1][0]['id']);route['reference']=hs.reference_audit(route,g);group['paths'][net]=route
   except (ValueError,KeyError) as error:group['errors'].append(net+': '+str(error))
  lengths=[p['planar_length_mm'] for p in group['paths'].values()];group['carrier_planar_spread_mm']=max(lengths)-min(lengths) if len(lengths)==len(nets) else None
  group['carrier_planar_group_screen_pass']=not group['errors'] and group['carrier_planar_spread_mm'] is not None and group['carrier_planar_spread_mm']<=1.27
  out['groups'][name]=group
 return out

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('pcb',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args();r=audit(a.pcb);a.output.write_text(hs.report_json(r),encoding='utf-8');print(json.dumps({k:{'errors':v['errors'],'spread':v['carrier_planar_spread_mm'],'lengths':{n:round(p['planar_length_mm'],6) for n,p in v['paths'].items()}} for k,v in r['groups'].items()},indent=2));raise SystemExit(1)

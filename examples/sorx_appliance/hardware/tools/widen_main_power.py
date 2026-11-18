#!/usr/bin/env python3
"""Widen measured current-carrying power segments using actual foreign copper clearance.

No topology changes, moved parts, waived rules or new layer transitions. Native
DRC and a fresh nodal solution remain mandatory after this local improvement.
"""
import argparse,hashlib,json,math,shutil
from pathlib import Path

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 import pcbnew
 from shapely.geometry import Point,LineString
 from shapely.ops import unary_union
 from high_speed_audit import load_geometry,polygons
 from verification_core import parse_drc
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('source',type=Path);ap.add_argument('network',type=Path);ap.add_argument('output',type=Path);ap.add_argument('evidence',type=Path);a=ap.parse_args()
 if a.output.exists() or a.evidence.exists():raise ValueError('Fresh output/evidence required')
 data=json.loads(a.network.read_text())
 if data['board_sha256']!=sha(a.source):raise ValueError('Stale network evidence')
 a.evidence.mkdir(parents=True);b=pcbnew.LoadBoard(str(a.source));g=load_geometry(a.source,b)
 def report(label):
  pcbnew.ZONE_FILLER(b).Fill(b.Zones());p=a.evidence/(label+'.txt');pcbnew.WriteDRCReport(b,str(p),pcbnew.EDA_UNITS_MILLIMETRES,True);return parse_drc(p.read_text())
 before=report('before-drc');wanted={}
 for c in data['cases'][:3]:
  for e in c['all_current_carrying_sections']:
   if e['kind']=='track' and e['current_a']>=.25:wanted[e['object']]=max(wanted.get(e['object'],0),e['current_a'])
 raw={t.m_Uuid.AsString():t for t in b.GetTracks()};changes=[];refused=[]
 holes=[]
 for f in b.GetFootprints():
  for p in f.Pads():
   if p.GetAttribute()==pcbnew.PAD_ATTRIB_NPTH:
    # Effective copper polygon for NPTH equals its full round/slot clearance shape.
    holes.extend(polygons(p.GetEffectivePolygon()))
 for t in g['tracks']:
  if t['id'] not in wanted:continue
  target=1.5 if t['net']=='+5V' else 2.0
  others=[x['line'].buffer(x['width_mm']/2,32) for x in g['tracks'] if x['layer']==t['layer'] and x['net']!=t['net']]
  others += [p['shape'] for p in g['pads'] if t['layer'] in p['layers'] and p['net']!=t['net']]
  others += [Point(v['xy']).buffer(v['diameter_mm']/2,32) for v in g['vias'] if t['layer'] in v['layers'] and v['net']!=t['net']]
  # .20mm copper clearance conservatively exceeds ordinary .15 and DP .18.
  limit=2*(t['line'].distance(unary_union(others))-.20) if others else target
  # NPTH copper-to-hole spacing is independently .25mm.
  if holes:limit=min(limit,2*(t['line'].distance(unary_union(holes))-.25))
  bb=b.GetBoardEdgesBoundingBox();x0,y0,x1,y1=bb.GetX()/1e6,bb.GetY()/1e6,bb.GetRight()/1e6,bb.GetBottom()/1e6
  boundary=LineString([(x0,y0),(x1,y0),(x1,y1),(x0,y1),(x0,y0)])
  limit=min(limit,2*(t['line'].distance(boundary)-.25),target)
  width=math.floor((limit+1e-8)/.025)*.025
  entry=dict(uuid=t['id'],net=t['net'],layer=t['layer'],from_mm=t['width_mm'],maximum_safe_mm=limit,target_mm=target,current_a=wanted[t['id']],start=list(t['line'].coords[0]),end=list(t['line'].coords[-1]))
  if width>t['width_mm']+.001:
   raw[t['id']].SetWidth(pcbnew.FromMM(width));entry['to_mm']=width;changes.append(entry)
  else:refused.append(entry)
 after=report('after-drc');old={x['fingerprint'] for x in before['findings'] if x['section']!='unconnected pads'}
 new=[x for x in after['findings'] if x['section']!='unconnected pads' and x['fingerprint'] not in old]
 accepted=not new and after['counts']['unconnected pads']<=before['counts']['unconnected pads']
 pcbnew.SaveBoard(str(a.output),b)
 for s in ('.kicad_pro','.kicad_dru'):
  if a.source.with_suffix(s).exists():shutil.copy2(a.source.with_suffix(s),a.output.with_suffix(s))
 result=dict(status='ACCEPTED FOR FURTHER DEVELOPMENT' if accepted else 'REJECTED',source=str(a.source),source_sha256=sha(a.source),output=str(a.output),output_sha256=sha(a.output),network_sha256=sha(a.network),tool_sha256=sha(__file__),before=before['counts'],after=after['counts'],new_other_findings=new,changes=changes,limited_segments=refused,fabrication_ready=False)
 (a.evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:result[k] for k in ['status','before','after']}),len(changes),'widened',flush=True)
 return 0 if accepted else 1
if __name__=='__main__':raise SystemExit(main())

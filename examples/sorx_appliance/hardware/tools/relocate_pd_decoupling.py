#!/usr/bin/env python3
"""Replace remote PD regulator-capacitor loops with compact backside decoupling.

Retains exact existing F.Cu pin escapes, removes obsolete net copper by UUID,
and performs fresh-process native validation. Thermal vias require filled/capped
assembly qualification; this helper never declares fabrication readiness.
"""
import argparse,hashlib,json,re,shutil,subprocess,sys
from pathlib import Path
PLACEMENT={'C14':(17.75,9.25,90),'C15':(20.75,11.5,0)}
KEEP={'2bb2031f-2216-47e8-a1e3-593d210dfa00','340263e1-dc72-40f7-8ac4-230c877704b0','38fb4ff5-d3e1-4ccd-ad72-1aae0815daf7','41ad8bd3-3096-4dea-a986-f75f162c05f9','5501fa80-6b47-4f59-b1fc-5c94b0f70e8d','df51cd63-6894-46df-8fb9-70985690e164','5326039b-7b28-4be1-8f5c-b3c3630df7b7','8c787eda-6988-4b8f-b3ae-00a5ede6b0f4','c5ae1794-e871-4a78-9efc-833c158b0550','efd269c1-bb08-41a6-8f97-ad685c8f2a60'}
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def finalize(board_path,evidence):
 import pcbnew
 from verification_core import parse_drc
 b=pcbnew.LoadBoard(str(board_path));added=[]
 def track(net,layer,a,z,width=.25):
  t=pcbnew.PCB_TRACK(b);t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]),pcbnew.FromMM(a[1])));t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(z[0]),pcbnew.FromMM(z[1])));t.SetWidth(pcbnew.FromMM(width));t.SetLayer(layer);t.SetNet(b.GetNetsByName()[net]);b.Add(t);added.append(dict(uuid=t.m_Uuid.AsString(),kind='track',net=net,layer=b.GetLayerName(layer),start=a,end=z,width_mm=width))
 def via(net,xy):
  if any(t.GetClass()=='PCB_VIA'and t.GetNetname()==net and abs(t.GetPosition().x/1e6-xy[0])<.001 and abs(t.GetPosition().y/1e6-xy[1])<.001 for t in b.GetTracks()):return
  v=pcbnew.PCB_VIA(b);v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(xy[0]),pcbnew.FromMM(xy[1])));v.SetWidth(pcbnew.FromMM(.6));v.SetDrill(pcbnew.FromMM(.3));v.SetViaType(pcbnew.VIATYPE_THROUGH);v.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu);v.SetNet(b.GetNetsByName()[net]);b.Add(v);added.append(dict(uuid=v.m_Uuid.AsString(),kind='via',net=net,xy=xy,diameter_mm=.6,drill_mm=.3))
 track('PD_1V2',pcbnew.B_Cu,(17.7119,11.0326),(17.75,10.025))
 via('PD_2V7',(18.9945,11.5596));track('PD_2V7',pcbnew.B_Cu,(18.9945,11.5596),(19.975,11.5))
 for xy in[(16,12.5),(15,13.5),(16,13.5)]:via('GND',xy)
 via('GND',(18.75,8.475));track('GND',pcbnew.B_Cu,(17.75,8.475),(18.75,8.475))
 via('GND',(22.5,11.5));track('GND',pcbnew.B_Cu,(21.525,11.5),(22.5,11.5))
 pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.SaveBoard(str(board_path),b);pcbnew.WriteDRCReport(b,str(evidence/'after-drc.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True)
 (evidence/'added-copper.json').write_text(json.dumps(added,indent=2)+'\n')
def main():
 import pcbnew
 from verification_core import parse_drc
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('source',type=Path);ap.add_argument('output',type=Path);ap.add_argument('evidence',type=Path);a=ap.parse_args()
 if a.output.exists()or a.evidence.exists():raise ValueError('Fresh output/evidence required')
 a.evidence.mkdir(parents=True);b=pcbnew.LoadBoard(str(a.source));pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.WriteDRCReport(b,str(a.evidence/'before-drc.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True)
 before=parse_drc((a.evidence/'before-drc.txt').read_text());inventory={t.m_Uuid.AsString():dict(net=t.GetNetname(),kind=t.GetClass())for t in b.GetTracks()if t.GetNetname()in('PD_1V2','PD_2V7')}
 if not KEEP<=set(inventory):raise ValueError('Expected retained pin escape has changed; re-audit first')
 kill=set(inventory)-KEEP;placements=[]
 for ref,(x,y,angle)in PLACEMENT.items():
  f=b.FindFootprintByReference(ref);old=dict(xy=[f.GetPosition().x/1e6,f.GetPosition().y/1e6],angle=f.GetOrientationDegrees(),layer=b.GetLayerName(f.GetLayer()))
  if f.GetLayer()!=pcbnew.B_Cu:f.Flip(f.GetPosition(),False)
  f.SetOrientationDegrees(angle);f.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x),pcbnew.FromMM(y)));f.Reference().SetPosition(f.GetPosition());placements.append(dict(ref=ref,before=old,after=dict(xy=[x,y],angle=angle,layer='B.Cu')))
 pcbnew.SaveBoard(str(a.output),b);lines=[];removed=[]
 for line in a.output.read_text().splitlines(keepends=True):
  m=re.search(r'\(tstamp ([0-9a-f-]+)\)',line)
  if re.match(r'^\s*\((segment|via) ',line)and m and m.group(1)in kill:removed.append(m.group(1));continue
  lines.append(line)
 if set(removed)!=kill:raise ValueError('Exact copper deletion failed')
 a.output.write_text(''.join(lines))
 for s in('.kicad_pro','.kicad_dru'):
  if a.source.with_suffix(s).exists():shutil.copy2(a.source.with_suffix(s),a.output.with_suffix(s))
 subprocess.run([sys.executable,__file__,'--finalize',str(a.output),str(a.evidence)],check=True)
 after=parse_drc((a.evidence/'after-drc.txt').read_text());old={x['fingerprint']for x in before['findings']if x['section']!='unconnected pads'};new=[x for x in after['findings']if x['section']!='unconnected pads'and x['fingerprint']not in old];accepted=not new and after['counts']['unconnected pads']<=before['counts']['unconnected pads']
 result=dict(status='ACCEPTED FOR FURTHER DEVELOPMENT'if accepted else'REJECTED',source=str(a.source),source_sha256=sha(a.source),output=str(a.output),output_sha256=sha(a.output),tool_sha256=sha(__file__),placements=placements,removed_copper=[dict(uuid=u,**inventory[u])for u in removed],retained_copper=sorted(KEEP),before=before['counts'],after=after['counts'],new_other_findings=new,fabrication_ready=False,thermal_via_process='Filled/capped qualification required; no blanket via-in-pad waiver')
 (a.evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:result[k]for k in('status','before','after')}),flush=True);return 0 if accepted else 1
if __name__=='__main__':
 if len(sys.argv)==4 and sys.argv[1]=='--finalize':finalize(Path(sys.argv[2]),Path(sys.argv[3]))
 else:raise SystemExit(main())

#!/usr/bin/env python3
"""Close the audited100mA debug USB input using explicit filled/capped power-pad vias."""
import argparse,hashlib,json,shutil
from pathlib import Path

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 import pcbnew
 from verification_core import parse_drc
 from power_acceptance import accept_power_change
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('source',type=Path);ap.add_argument('output',type=Path);ap.add_argument('evidence',type=Path);a=ap.parse_args()
 if a.output.exists()or a.evidence.exists():raise ValueError('Fresh paths required')
 a.evidence.mkdir(parents=True);b=pcbnew.LoadBoard(str(a.source));net='VBUS_DBG_RAW';records=[]
 def report(name):
  pcbnew.ZONE_FILLER(b).Fill(b.Zones());p=a.evidence/(name+'.txt');pcbnew.WriteDRCReport(b,str(p),pcbnew.EDA_UNITS_MILLIMETRES,True);return parse_drc(p.read_text())
 before=report('before');old={f['fingerprint']for f in before['findings']if f['section']!='unconnected pads'}
 def add(layer,start,end):
  t=pcbnew.PCB_TRACK(b);t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start[0]),pcbnew.FromMM(start[1])));t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]),pcbnew.FromMM(end[1])));t.SetWidth(pcbnew.FromMM(.25));t.SetLayer(layer);t.SetNet(b.GetNetsByName()[net]);b.Add(t);records.append(dict(uuid=t.m_Uuid.AsString(),kind='track',net=net,layer=b.GetLayerName(layer),start=start,end=end,width_mm=.25))
 for ref,pin,xy in [('J10','A4',(115.6,110.64)),('J10','A9',(120.4,110.64)),('FB2','1',(115.6525,106.42))]:
  p=next(p for p in b.FindFootprintByReference(ref).Pads()if p.GetNumber()==pin)
  if p.GetNetname()!=net or abs(p.GetPosition().x/1e6-xy[0])>.001 or abs(p.GetPosition().y/1e6-xy[1])>.001:raise ValueError('Audited endpoint changed')
 for xy in[(115.6525,106.42),(115.6,110.64),(120.4,110.64)]:
  v=pcbnew.PCB_VIA(b);v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(xy[0]),pcbnew.FromMM(xy[1])));v.SetWidth(pcbnew.FromMM(.6));v.SetDrill(pcbnew.FromMM(.3));v.SetViaType(pcbnew.VIATYPE_THROUGH);v.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu);v.SetNet(b.GetNetsByName()[net]);b.Add(v);records.append(dict(uuid=v.m_Uuid.AsString(),kind='via',net=net,xy=xy,diameter_mm=.6,drill_mm=.3))
 for layer,points in[(pcbnew.B_Cu,[(115.6525,106.42),(115.6,106.4725),(115.6,110.64)]),(pcbnew.B_Cu,[(115.6,107.8),(120.4,107.8),(120.4,110.64)])]:
  for start,end in zip(points,points[1:]):add(layer,start,end)
 after=report('after');new=[f for f in after['findings']if f['section']!='unconnected pads'and f['fingerprint']not in old];accepted,net_acceptance=accept_power_change(before,after,new,['VBUS_DBG_RAW']);pcbnew.SaveBoard(str(a.output),b)
 for s in('.kicad_pro','.kicad_dru'):
  if a.source.with_suffix(s).exists():shutil.copy2(a.source.with_suffix(s),a.output.with_suffix(s))
 d=dict(status='ACCEPTED FOR FURTHER DEVELOPMENT'if accepted else'REJECTED',source_sha256=sha(a.source),output_sha256=sha(a.output),tool_sha256=sha(__file__),current_allocation_a=.10,via_in_power_pad_qualification_required=['FB2.1','J10.A4/B9','J10.A9/B4'],assembly_requirement='Filled and copper-capped planar vias; no open or merely underside-tented holes in solder lands. Supplier approval required.',before=before['counts'],after=after['counts'],new_other_findings=new,net_acceptance=net_acceptance,added=records,fabrication_ready=False);(a.evidence/'result.json').write_text(json.dumps(d,indent=2)+'\n');print(json.dumps({k:d[k]for k in('status','before','after')}),flush=True);return 0 if accepted else 1
if __name__=='__main__':raise SystemExit(main())

#!/usr/bin/env python3
"""Close audited small supply islands using explicit copper endpoints and existing vias.

The two1.8V islands contain reset logic and pullups only (5mA allocation). The
VBUS island contains Q2 source reference and R12 sense/discharge only (36mA).
This tool never routes on the In1/In4 reference planes or changes existing copper.
"""
import argparse,hashlib,json,shutil,math
from pathlib import Path

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 import pcbnew
 import route_completion as rc
 from verification_core import parse_drc
 from power_acceptance import accept_power_change
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('source',type=Path);ap.add_argument('output',type=Path);ap.add_argument('evidence',type=Path);a=ap.parse_args()
 if a.output.exists()or a.evidence.exists():raise ValueError('Fresh paths required')
 a.evidence.mkdir(parents=True);b=pcbnew.LoadBoard(str(a.source));records=[]
 def report(name):
  pcbnew.ZONE_FILLER(b).Fill(b.Zones());p=a.evidence/(name+'.txt');pcbnew.WriteDRCReport(b,str(p),pcbnew.EDA_UNITS_MILLIMETRES,True);return parse_drc(p.read_text())
 before=report('before');old={x['fingerprint']for x in before['findings']if x['section']!='unconnected pads'}
 current={t.m_Uuid.AsString():t for t in b.GetTracks()}
 for uid in ['43d258bf-7058-4775-aa16-77a2ab5033f2','bab33cc3-b45f-4c9d-89db-4980d2542155','4010b7e9-20ff-461c-b2f0-c09bea22568a']:
  if uid not in current or current[uid].GetNetname()!='+1V8':raise ValueError('Audited1V8 endpoint changed')
 def add(net,layer,start,end,width,current_a):
  t=pcbnew.PCB_TRACK(b);t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start[0]),pcbnew.FromMM(start[1])));t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]),pcbnew.FromMM(end[1])));t.SetWidth(pcbnew.FromMM(width));t.SetLayer(layer);t.SetNet(b.GetNetsByName()[net]);b.Add(t);records.append(dict(uuid=t.m_Uuid.AsString(),net=net,layer=b.GetLayerName(layer),start=start,end=end,width_mm=width,current_allocation_a=current_a))
 add('+1V8',pcbnew.In2_Cu,(40.026964916066945,59.65642883068638),(31.537082100719662,56.73708540317634),.15,.005)
 add('+1V8',pcbnew.B_Cu,(30.6387,56.15),(30.6387,55.6606),.15,.005)
 # Existing through-via copper is a valid anchor; adding a duplicate drill is not.
 ends=[]
 for uid in ['36d9d5ca-7bbd-4657-ae05-a5382f70ddb1','2e9e7f9b-79fe-455f-8ecc-5411a3b95815']:
  v=current.get(uid)
  if v is None or v.GetClass()!='PCB_VIA'or v.GetNetname()!='VBUS_IN':raise ValueError('Audited VBUS anchor changed')
  ends.append((v.GetPosition().x/1e6,v.GetPosition().y/1e6))
 rc.W_TRK=.25;g=rc.Grid(b);g.load(b)
 # Constrain to In3. Existing vias are reused; every new transition is forbidden.
 g.T['F'][:]+=100;g.T['B'][:]+=100
 for layer in rc.COPPER:g.V[layer][:]+=100
 route=g.route('VBUS_IN',ends[0],'C',ends[1],'C',rip=False)
 vbus_record={'net':'VBUS_IN','current_allocation_a':.036,'existing_via_anchors':ends,'routed':False}
 if route:
  runs,vias=route
  if vias or any(layer!='C'for layer,_ in runs):raise ValueError('Unexpected supply transition')
  rc.commit(b,g,'VBUS_IN',b.GetNetsByName()['VBUS_IN'],runs,[],.25);vbus_record.update(routed=True,runs=runs,width_mm=.25)
 after=report('after');new=[x for x in after['findings']if x['section']!='unconnected pads'and x['fingerprint']not in old];accepted,net_acceptance=accept_power_change(before,after,new,['+1V8', 'VBUS_IN']);pcbnew.SaveBoard(str(a.output),b)
 for s in('.kicad_pro','.kicad_dru'):
  if a.source.with_suffix(s).exists():shutil.copy2(a.source.with_suffix(s),a.output.with_suffix(s))
 result=dict(status='ACCEPTED FOR FURTHER DEVELOPMENT'if accepted else'REJECTED',source_sha256=sha(a.source),output_sha256=sha(a.output),tool_sha256=sha(__file__),router_sha256=sha(rc.__file__),before=before['counts'],after=after['counts'],new_other_findings=new,net_acceptance=net_acceptance,added_1v8=records,vbus=vbus_record,fabrication_ready=False)
 (a.evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:result[k]for k in('status','before','after')}),vbus_record['routed'],flush=True);return 0 if accepted else 1
if __name__=='__main__':raise SystemExit(main())

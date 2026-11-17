"""Update the preserved routed board from the corrected model without regenerating placement.

Changed-pad copper is detached explicitly and left for the completion router. Every
removal is recorded; final DRC and model parity remain mandatory release gates.
"""
import argparse,json,math,shutil,tempfile,subprocess,sys,re
from pathlib import Path
import pcbnew
from shapely.geometry import Point,LineString,Polygon
from design import DESIGN
from gen_sch import resolve
from gen_pcb import fp_path,api_rect,overlap
mm=pcbnew.FromMM;MM=pcbnew.ToMM
# Fixed manual engineering placements, including the manufacturer's module mounts.
PLACEMENT={
 'C14':(17.75,9.25,90,'B'),'C15':(20.75,11.5,0,'B'),
 'U1':(134,46,0,'F'),'R12':(18,16,90,'B'),
 'U38':(8,15,0,'B'),'C136':(3,18.5,0,'B'),'C137':(8,19,0,'B'),'C138':(7,22,0,'B'),
 'U39':(13,22,0,'B'),'R112':(13,26,0,'B'),'R113':(17,26,0,'B'),'Q5':(18,22,0,'B'),'TP21':(17.5,28,0,'F'),
 'H5':(45.3945,87.074,0,'F'),'H6':(110.5945,87.074,0,'F'),'H7':(37.9945,56.074,0,'F'),'H8':(117.9945,56.074,0,'F'),
 'R115':(13,18,0,'B'),'R116':(21,22,0,'B'),'R117':(12.5,14,0,'B'),'R118':(12.5,16,0,'B'),'U40':(124,104,0,'B'),'C139':(124,107,0,'B'),'R114':(120,106,0,'B'),
 'JP1':(146,77,270,'F'),'JP2':(154,77,0,'F'),'J14':(160.5,77.5,0,'F'),
 'U12':(30,55.5,0,'B'),'C76':(30.75,58.5,90,'B'),'U19':(66,96,90,'F'),
 'R5':(33,54.5,90,'F'),'C3':(33,51.5,90,'F'),'R4':(33,61,90,'F'),'R107':(31.5,58,90,'F'),'C20':(123,57,90,'F'),'R11':(140,46,90,'F')}
RENAME={'FRAM_HOLD#':'FRAM_DNU'}

def pad_shape(p):
 ps=p.GetEffectivePolygon(); result=[]
 for i in range(ps.OutlineCount()):
  c=ps.Outline(i);pts=[(MM(c.CPoint(k).x),MM(c.CPoint(k).y)) for k in range(c.PointCount())]
  if len(pts)>=3:result.append(Polygon(pts))
 from shapely.ops import unary_union
 return unary_union(result)

def trace_shape(t):
 if t.GetClass()=='PCB_VIA':return Point(MM(t.GetPosition().x),MM(t.GetPosition().y)).buffer(MM(t.GetWidth())/2)
 if t.GetClass()!='PCB_TRACK':raise ValueError('Unsupported routed shape '+t.GetClass())
 a,c=t.GetStart(),t.GetEnd();return LineString([(MM(a.x),MM(a.y)),(MM(c.x),MM(c.y))]).buffer(MM(t.GetWidth())/2)

def main(src,dst):
 b=pcbnew.LoadBoard(str(src)); log={'renamed_nets':RENAME,'replaced':[],'added':[],'changed_pads':[],'removed_copper':[],'placement_overlaps':[]}
 nets={str(k):v for k,v in b.GetNetsByName().items()}
 def net(name):
  if name not in nets:
   n=pcbnew.NETINFO_ITEM(b,name);b.Add(n);nets[name]=n
  return nets[name]
 for t in b.GetTracks():
  n=str(t.GetNetname())
  if n in RENAME:t.SetNet(net(RENAME[n]))
 fps={f.GetReference():f for f in b.GetFootprints()};kill={};new_refs=set();copper_shapes={t.m_Uuid.AsString():trace_shape(t) for t in b.GetTracks()}
 def detach(ref,shape,layer,oldnet,reason):
  for t in b.GetTracks():
   if str(t.GetNetname())!=oldnet:continue
   if t.GetClass()!='PCB_VIA' and t.GetLayer()!=layer:continue
   if copper_shapes[t.m_Uuid.AsString()].intersects(shape):
    kill[t.m_Uuid.AsString()]={'uuid':t.m_Uuid.AsString(),'net':oldnet,'reason':reason,'ref':ref}
 for part in DESIGN.parts.values():
  if not part.fp:continue
  expected={n:(v or '') for n,v in resolve(part).items()};old=b.FindFootprintByReference(part.ref)
  oldpads=[]
  if old:
   oldpads=[{'number':x.GetNumber(),'net':RENAME.get(str(x.GetNetname()),str(x.GetNetname())),'shape':pad_shape(x),'layer':x.GetLayer(),'xy':(MM(x.GetPosition().x),MM(x.GetPosition().y))} for x in old.Pads()]
  replace=old is None or (str(old.GetFPID().GetLibNickname())+':'+str(old.GetFPID().GetLibItemName()))!=part.fp or part.ref=='J1'
  moved=part.ref in PLACEMENT and old is not None
  if replace:
   directory,name=fp_path(part.fp);f=pcbnew.FootprintLoad(directory,name)
   if f is None:raise ValueError('Missing footprint '+part.fp)
   f.SetReference(part.ref);f.SetFPID(pcbnew.LIB_ID(*part.fp.split(':',1)))
   if old:
    xy=(MM(old.GetPosition().x),MM(old.GetPosition().y));side='B' if old.GetLayer()==pcbnew.B_Cu else 'F';angle=old.GetOrientationDegrees()
    b.Remove(old);log['replaced'].append(part.ref)
   else:
    if part.ref not in PLACEMENT:raise ValueError('Missing explicit placement for '+part.ref)
    xy=(0,0);side='F';angle=0;log['added'].append(part.ref)
   b.Add(f);f.SetPosition(pcbnew.VECTOR2I(0,0))
   if side=='B':f.Flip(pcbnew.VECTOR2I(0,0),False)
   f.SetOrientationDegrees(angle);f.SetPosition(pcbnew.VECTOR2I(mm(xy[0]),mm(xy[1])))
  else:f=old
  if part.ref in PLACEMENT:
   x,y,angle,side=PLACEMENT[part.ref]
   if (f.GetLayer()==pcbnew.B_Cu)!=(side=='B'):f.Flip(f.GetPosition(),False)
   f.SetOrientationDegrees(angle);f.SetPosition(pcbnew.VECTOR2I(mm(x),mm(y)))
  f.SetValue(part.value)
  bynum={}
  for x in f.Pads():
   name=expected.get(x.GetNumber(),'');x.SetNet(net(name));bynum.setdefault(x.GetNumber(),[]).append(x)
  for prev in oldpads:
   name=expected.get(prev['number'],'')
   choices=bynum.get(prev['number'],[])
   relocation=not choices or min(math.hypot(MM(x.GetPosition().x)-prev['xy'][0],MM(x.GetPosition().y)-prev['xy'][1]) for x in choices)>.01
   if name!=prev['net'] or relocation:
    log['changed_pads'].append({'ref':part.ref,'pad':prev['number'],'old_net':prev['net'],'new_net':name,'relocated':relocation})
    detach(part.ref,prev['shape'],prev['layer'],prev['net'],'pad net/geometry changed')
  if replace or moved:new_refs.add(part.ref)
  if part.dnp:f.SetAttributes(f.GetAttributes()|pcbnew.FP_EXCLUDE_FROM_POS_FILES|pcbnew.FP_EXCLUDE_FROM_BOM)
  if not part.inbom:f.SetAttributes(f.GetAttributes()|pcbnew.FP_EXCLUDE_FROM_BOM)
  f.Reference().SetLayer(pcbnew.B_Fab if f.GetLayer()==pcbnew.B_Cu else pcbnew.F_Fab)
  f.Reference().SetPosition(f.GetPosition());f.Value().SetVisible(False)
  fps[part.ref]=f
  if replace or moved:
   # KiCad7 invalidates Python containers after footprint replacement: roundtrip before another access.
   tmp=Path('/tmp/sorx-codex-sync-intermediate.kicad_pcb');pcbnew.SaveBoard(str(tmp),b);b=pcbnew.LoadBoard(str(tmp));nets={str(k):v for k,v in b.GetNetsByName().items()}
 fps={f.GetReference():f for f in b.GetFootprints()}
 # Clear existing copper that new pads/holes obstruct, then let routing restore required connectivity.
 for ref in new_refs:
  for p in fps[ref].Pads():
   shape=pad_shape(p);pn=str(p.GetNetname());drill=p.GetDrillSize()
   hole=Point(MM(p.GetPosition().x),MM(p.GetPosition().y)).buffer(max(MM(drill.x),MM(drill.y))/2) if drill.x or drill.y else None
   for t in b.GetTracks():
    uid=t.m_Uuid.AsString()
    if uid in kill:continue
    geom=copper_shapes[uid];isvia=t.GetClass()=='PCB_VIA'
    obstruct=hole is not None and geom.distance(hole)<.25
    if (isvia or p.IsOnLayer(t.GetLayer())) and (not pn or str(t.GetNetname())!=pn) and geom.distance(shape)<.15-1e-5:obstruct=True
    if obstruct:kill[uid]={'uuid':uid,'net':str(t.GetNetname()),'reason':'new pad/hole clearance','ref':ref}
 for ref in sorted(new_refs):
  f=fps[ref];r=api_rect(f)
  for other,q in fps.items():
   if ref==other or q.GetLayer()!=f.GetLayer() or other.startswith(('TP','FID')):continue
   if overlap(r,api_rect(q),.1):log['placement_overlaps'].append([ref,other])
 dst.parent.mkdir(parents=True,exist_ok=True)
 shutil.copyfile(src.with_suffix('.kicad_pro'),dst.with_suffix('.kicad_pro'))
 pcbnew.SaveBoard(str(dst),b)
 # Remove serialized track records by exact UUID. KiCad7 SWIG type metadata can
 # become unusable after many Remove() calls; finish/refill in a fresh process.
 removed=[];lines=[]
 for line in dst.read_text().splitlines(keepends=True):
  uid=re.search(r'\(tstamp ([0-9a-f-]+)\)',line)
  if re.match(r'^\s*\((segment|via) ',line) and uid and uid.group(1) in kill:
   removed.append(uid.group(1));continue
  lines.append(line)
 if set(removed)!=set(kill):raise RuntimeError('Could not identify all detached copper records')
 dst.write_text(''.join(lines));log['removed_copper']=[kill[x] for x in removed]
 dst.with_suffix('.sync.json').write_text(json.dumps(log,indent=2)+'\n')
 subprocess.run([sys.executable,__file__,'--finalize',str(dst)],check=True)
 print(json.dumps({'replaced':log['replaced'],'added':log['added'],'copper_detached':len(kill),'placement_overlaps':log['placement_overlaps']}))
if __name__=='__main__':
 if len(sys.argv)==3 and sys.argv[1]=='--finalize':
  b=pcbnew.LoadBoard(sys.argv[2]);pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.SaveBoard(sys.argv[2],b)
 else:
  p=argparse.ArgumentParser();p.add_argument('input',type=Path);p.add_argument('output',type=Path);a=p.parse_args();main(a.input,a.output)

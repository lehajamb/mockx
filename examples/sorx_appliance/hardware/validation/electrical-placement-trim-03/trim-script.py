import sys,json,shutil,re,hashlib
from pathlib import Path
sys.path.insert(0,'/hardware/tools')
import pcbnew
from shapely.geometry import Point
from sync_codex_board import trace_shape,pad_shape
from audit_orphan_copper import audit
from verification_core import parse_drc
src=Path('/hardware/runs/route-electrical-12-power.kicad_pcb');dst=Path('/hardware/runs/route-electrical-13c-trimmed.kicad_pcb');ev=Path('/hardware/validation/electrical-placement-trim-03');ev.mkdir()
b=pcbnew.LoadBoard(str(src));orphans,groups=audit(b);kill={o['uuid'] for island in orphans for o in island['objects']};net='PCIE_RST#_3V3';items=[]
for t in b.GetTracks():
 if t.GetNetname()==net:
  via=t.GetClass()=='PCB_VIA';a=(t.GetStart().x/1e6,t.GetStart().y/1e6);c=(t.GetEnd().x/1e6,t.GetEnd().y/1e6)
  items.append(dict(id=t.m_Uuid.AsString(),shape=trace_shape(t),layers=[l for l in b.GetEnabledLayers().Seq() if l<=31 and t.IsOnLayer(l)],ends=[a,c],via=via))
pads=[dict(shape=pad_shape(p),layers=[l for l in b.GetEnabledLayers().Seq() if l<=31 and p.IsOnLayer(l)]) for f in b.GetFootprints() for p in f.Pads() if p.GetNetname()==net]
for repeat in range(100):
 remove=[]
 for x in items:
  if x['id'] in kill:continue
  other=[y for y in items if y['id'] not in kill and y['id']!=x['id']]+pads
  if x['via']:
   touches={layer for y in other for layer in set(x['layers'])&set(y['layers']) if y['shape'].intersects(x['shape'])}
   if len(touches)<2:remove.append(x['id'])
  else:
   for xy in x['ends']:
    if not any(set(x['layers'])&set(y['layers']) and y['shape'].buffer(1e-6).covers(Point(xy)) for y in other):remove.append(x['id']);break
 if not remove:break
 kill.update(remove)
rows=[];lines=[]
for line in src.read_text().splitlines(keepends=True):
 m=re.search(r'\(tstamp ([0-9a-f-]+)\)',line)
 if re.match(r'^\s*\((segment|via) ',line) and m and m.group(1) in kill:rows.append(dict(uuid=m.group(1),record=line.strip()));continue
 lines.append(line)
if len(rows)!=len(kill):raise ValueError('Missing UUID')
dst.write_text(''.join(lines));shutil.copy2(src.with_suffix('.kicad_pro'),dst.with_suffix('.kicad_pro'));new=pcbnew.LoadBoard(str(dst));_,groupsafter=audit(new)
if {k:v for k,v in groups.items() if v}!={k:v for k,v in groupsafter.items() if v}:raise ValueError('Actual pad connectivity changed')
pcbnew.ZONE_FILLER(new).Fill(new.Zones());pcbnew.SaveBoard(str(dst),new);pcbnew.WriteDRCReport(new,str(ev/'after-drc.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True)
report=parse_drc((ev/'after-drc.txt').read_text());baseline=parse_drc(Path('/hardware/validation/electrical-placement-02/before-drc.txt').read_text());prev={f['fingerprint'] for f in baseline['findings'] if f['section']!='unconnected pads'};added=[f for f in report['findings'] if f['section']!='unconnected pads' and f['fingerprint'] not in prev]
r=dict(source_sha256=hashlib.sha256(src.read_bytes()).hexdigest(),output_sha256=hashlib.sha256(dst.read_bytes()).hexdigest(),removed=rows,pad_connectivity_unchanged=True,before=baseline['counts'],after=report['counts'],new_other_findings=added,accepted=not added,fabrication_ready=False)
(ev/'result.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps({k:r[k] for k in ('accepted','before','after','new_other_findings')}),flush=True);raise SystemExit(0 if not added else 1)

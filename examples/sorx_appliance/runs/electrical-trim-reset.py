import sys,json,shutil,re,hashlib
from pathlib import Path
sys.path.insert(0,'/hardware/tools')
import pcbnew
from audit_orphan_copper import audit
from verification_core import parse_drc
source=Path('/hardware/runs/route-electrical-12-power.kicad_pcb');original=Path('/hardware/runs/route-electrical-11b-placement.kicad_pcb');target=Path('/hardware/runs/route-electrical-13b-trimmed.kicad_pcb');ev=Path('/hardware/validation/electrical-placement-trim-02');ev.mkdir()
b=pcbnew.LoadBoard(str(source));old=pcbnew.LoadBoard(str(original));oldids={t.m_Uuid.AsString() for t in old.GetTracks()};orphans,groups=audit(b)
kill={t.m_Uuid.AsString() for t in b.GetTracks() if t.GetNetname()=='PCIE_RST#_3V3' and t.m_Uuid.AsString() not in oldids}
kill.add('082062f8-e9a7-4282-993c-24916dcea85c')
kill.update(o['uuid'] for island in orphans for o in island['objects'])
rows=[];lines=[]
for line in source.read_text().splitlines(keepends=True):
 m=re.search(r'\(tstamp ([0-9a-f-]+)\)',line)
 if re.match(r'^\s*\((segment|via) ',line) and m and m.group(1) in kill:rows.append(dict(uuid=m.group(1),record=line.strip()));continue
 lines.append(line)
if len(rows)!=len(kill):raise ValueError('Missing exact UUID')
target.write_text(''.join(lines));shutil.copy2(source.with_suffix('.kicad_pro'),target.with_suffix('.kicad_pro'))
new=pcbnew.LoadBoard(str(target));orphansafter,groupsafter=audit(new)
groups={k:v for k,v in groups.items() if v};groupsafter={k:v for k,v in groupsafter.items() if v}
if groups!=groupsafter:
 print({k:[groups.get(k),groupsafter.get(k)] for k in groups.keys()|groupsafter.keys() if groups.get(k)!=groupsafter.get(k)},flush=True);raise ValueError('Trimming changes actual pad connectivity')
pcbnew.ZONE_FILLER(new).Fill(new.Zones());pcbnew.SaveBoard(str(target),new);pcbnew.WriteDRCReport(new,str(ev/'after-drc.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True)
report=parse_drc((ev/'after-drc.txt').read_text());baseline=parse_drc(Path('/hardware/validation/electrical-placement-02/before-drc.txt').read_text());prev={f['fingerprint'] for f in baseline['findings'] if f['section']!='unconnected pads'};added=[f for f in report['findings'] if f['section']!='unconnected pads' and f['fingerprint'] not in prev]
result=dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),output_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),removed=rows,pad_connectivity_unchanged=True,before=baseline['counts'],after=report['counts'],new_other_findings=added,accepted=not added,fabrication_ready=False)
(ev/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:result[k] for k in ('accepted','before','after','new_other_findings')}),flush=True)
raise SystemExit(0 if not added else 1)

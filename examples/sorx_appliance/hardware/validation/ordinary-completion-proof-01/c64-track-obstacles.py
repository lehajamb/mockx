import pcbnew,sys,json,re
from pathlib import Path
sys.path.insert(0,"/work/hardware/tools")
from shapely.geometry import Point,LineString
b=pcbnew.LoadBoard("/work/hardware/runs/ordinary-ground-repair/ordinary-input.kicad_pcb");center=Point(33.279999,49.11);base=Path("/work/hardware/runs/route-combined-15b-ground.kicad_pcb").read_text();ids=set(re.findall(r"\(tstamp ([^)]+)\)",base));records=[]
for t in b.GetTracks():
 a,z=t.GetStart(),t.GetEnd();via=isinstance(t,pcbnew.PCB_VIA);shape=Point(a.x/1e6,a.y/1e6) if via else LineString([(a.x/1e6,a.y/1e6),(z.x/1e6,z.y/1e6)]);clear=shape.distance(center)-t.GetWidth()/2e6-.3
 if clear<.25:records.append({"uuid":t.m_Uuid.AsString(),"net":t.GetNetname(),"layer":b.GetLayerName(t.GetLayer()),"kind":"via" if via else "track","start_mm":[a.x/1e6,a.y/1e6],"end_mm":[z.x/1e6,z.y/1e6],"copper_gap_to_proposed_via_mm":clear,"added_by_ordinary_proof":t.m_Uuid.AsString() not in ids})
print(json.dumps(records,indent=2));Path(__file__).with_suffix(".json").write_text(json.dumps(records,indent=2)+"\n")

import sys,json,hashlib,collections
from pathlib import Path
sys.path.insert(0,"/hardware/tools")
import pcbnew
from high_speed_audit import load_geometry,RouteGraph
base=pcbnew.LoadBoard("/hardware/runs/route-06-final-model.kicad_pcb")
path="/hardware/runs/route-electrical-07-power.kicad_pcb"
board=pcbnew.LoadBoard(path)
old={t.m_Uuid.AsString() for t in base.GetTracks()}
new=[t for t in board.GetTracks() if t.m_Uuid.AsString() not in old]
result={"added_by_net":dict(collections.Counter(t.GetNetname() for t in new)),
        "added_uuids":[t.m_Uuid.AsString() for t in new],
        "removed_uuids":sorted(old-{t.m_Uuid.AsString() for t in board.GetTracks()}),
        "source_sha256":hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "main_current_paths":[],"thermal_qualification":False}
g=load_geometry(path,board)
for net,pairs in [("VBUS_IN",[("J3:A4","Q1:1"),("J3:A9","Q1:1"),("J3:B4","Q1:1"),("J3:B9","Q1:1")]),("+5V",[("L1:2","J1:251"),("L1:2","J1:259")])]:
 graph=RouteGraph([t for t in g["tracks"] if t["net"]==net],[p for p in g["pads"] if p["net"]==net],[v for v in g["vias"] if v["net"]==net])
 for a,b in pairs:
  entry={"net":net,"source":a,"destination":b}
  try:
   route=graph.route(a,b);entry.update(connected=True,route=route,
    added_track_edges=[e for e in route['edges'] if e['kind']=='track' and e['object'] not in old])
  except ValueError as e:entry.update(connected=False,error=str(e))
  result['main_current_paths'].append(entry)
Path('/hardware/validation/power-routing-01/additions-and-main-paths.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({"added_by_net":result['added_by_net'],"removed":result['removed_uuids'],"paths":[{k:v for k,v in r.items() if k not in ('route','added_track_edges')} for r in result['main_current_paths']]},indent=2))

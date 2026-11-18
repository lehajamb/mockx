import sys,json,hashlib
from pathlib import Path
import pcbnew
source=Path('/hardware/runs/route-combined-15b-ground.kicad_pcb');target=Path(sys.argv[1]);out=Path(sys.argv[2]);a=pcbnew.LoadBoard(str(source));b=pcbnew.LoadBoard(str(target))
def copper(board):return{t.m_Uuid.AsString():t.GetNetname()for t in board.GetTracks()}
def pads(board):
 d={}
 for f in board.GetFootprints():
  for p in f.Pads():d.setdefault(f.GetReference()+':'+p.GetNumber(),[]).append(p.GetNetname())
 return{k:sorted(v)for k,v in d.items()}
a_c,b_c=copper(a),copper(b);a_p,b_p=pads(a),pads(b)
changes=[dict(uuid=u,before=a_c[u],after=b_c[u])for u in a_c.keys()&b_c.keys()if a_c[u]!=b_c[u]]
padchanges=[dict(pad=p,before=a_p.get(p),after=b_p.get(p))for p in a_p.keys()|b_p.keys()if a_p.get(p)!=b_p.get(p)]
removed={u:a_c[u]for u in a_c.keys()-b_c.keys()}
# Only the explicit obsolete regulator-capacitor copper may disappear in this integration stage.
unexpected={u:n for u,n in removed.items()if n not in ('PD_1V2','PD_2V7')}
result=dict(source=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),target=str(target),target_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),changed_existing_copper_nets=changes,changed_pad_nets=padchanges,removed_copper=removed,unexpected_removed_copper=unexpected,added_copper_count=len(b_c.keys()-a_c.keys()))
out.write_text(json.dumps(result,indent=2)+'\n');print('Changed copper nets',len(changes),'changed pad nets',len(padchanges),'unexpected removed',len(unexpected),'added',result['added_copper_count']);assert not changes and not padchanges and not unexpected

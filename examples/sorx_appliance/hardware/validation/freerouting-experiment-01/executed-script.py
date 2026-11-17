"""Disposable six-layer completion experiment using the pinned Freerouting tool.
All original differential and power copper stays fixed. Only ordinary signal
and GND results may be imported; native DRC remains the acceptance authority.
"""
import argparse,json,re,shutil,subprocess,sys,hashlib,time
from pathlib import Path
import pcbnew
import proj
from sx import parse,find,first,dump,Sym

def digest(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def eligible(net): return net=='GND' or proj.classify(net)=='Default' and not str(net).startswith(('+','VBUS','VIN','VSYS','MG_','SD_'))

def prepare(src,dsn):
 b=pcbnew.LoadBoard(str(src))
 for t in b.GetTracks():t.SetLocked(not eligible(str(t.GetNetname())))
 assert pcbnew.ExportSpecctraDSN(b,str(dsn))
 text=dsn.read_text().replace('(string_quote ")','(string_quote "SORX_QUOTE_MARKER")')
 r=parse(text);st=first(r,'structure')
 st[:]=[x for x in st if not (isinstance(x,list) and x and x[0]=='plane' and first(x,'polygon')[1] not in ('In1.Cu','In4.Cu'))]
 for x in find(st,'layer'):
  if x[1] in ('In1.Cu','In4.Cu'):first(x,'type')[1]=Sym('power')
 for cl in find(first(r,'network'),'class'):
  circuit=first(cl,'circuit')
  if circuit is None:circuit=[Sym('circuit')];cl.append(circuit)
  layers=('F.Cu','In2.Cu','In3.Cu','B.Cu') if cl[1] in ('PWR','PWR_HI') else ('F.Cu','In3.Cu','B.Cu')
  circuit.append([Sym('use_layer')]+[Sym(x) for x in layers])
 dsn.write_text(dump(r).replace('"SORX_QUOTE_MARKER"','"')+'\n')

def install(src,ses,dst):
 # Filter exact original wire UUIDs textually; KiCad7's Remove loop corrupts SWIG.
 b=pcbnew.LoadBoard(str(src));remove={t.m_Uuid.AsString() for t in b.GetTracks() if eligible(str(t.GetNetname()))}
 lines=[]
 for line in src.read_text().splitlines(keepends=True):
  uid=re.search(r'\(tstamp ([0-9a-f-]+)\)',line)
  if re.match(r'^\s*\((segment|via) ',line) and uid and uid.group(1) in remove:continue
  lines.append(line)
 dst.write_text(''.join(lines));shutil.copyfile(src.with_suffix('.kicad_pro'),dst.with_suffix('.kicad_pro'))
 subprocess.run([sys.executable,__file__,'import',str(dst),str(ses),str(dst)],check=True)

def import_session(dst,ses):
 b=pcbnew.LoadBoard(str(dst));root=parse(ses.read_text());routes=first(root,'routes');res=first(routes,'resolution');scale=1/(float(res[2])*1000);nets=b.GetNetsByName();n=0
 layers={b.GetLayerName(l):l for l in (0,1,2,3,4,31)}
 for net in find(first(routes,'network_out'),'net'):
  name=str(net[1])
  if not eligible(name):continue
  ni=nets[name]
  for wire in find(net,'wire'):
   path=first(wire,'path');layer=layers[str(path[1])]
   if layer in (1,4):raise ValueError('Unexpected signal track on reference plane')
   width=float(path[2])*scale;coords=list(map(float,path[3:]));pts=[(coords[i]*scale,-coords[i+1]*scale) for i in range(0,len(coords),2)]
   for a,c in zip(pts,pts[1:]):
    if a==c:continue
    t=pcbnew.PCB_TRACK(b);t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]),pcbnew.FromMM(a[1])));t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(c[0]),pcbnew.FromMM(c[1])));t.SetWidth(pcbnew.FromMM(width));t.SetLayer(layer);t.SetNet(ni);b.Add(t);n+=1
  for via in find(net,'via'):
   match=re.fullmatch(r'Via\[0-5\]_(\d+):(\d+)_um',str(via[1]))
   if not match:raise ValueError('Unrecognized via '+str(via[1]))
   v=pcbnew.PCB_VIA(b);v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(float(via[2])*scale),pcbnew.FromMM(-float(via[3])*scale)));v.SetWidth(pcbnew.FromMM(int(match[1])/1000));v.SetDrill(pcbnew.FromMM(int(match[2])/1000));v.SetLayerPair(0,31);v.SetNet(ni);b.Add(v)
 pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.SaveBoard(str(dst),b);print('Imported ordinary/GND segments',n)

def main():
 p=argparse.ArgumentParser();p.add_argument('operation',choices=['prepare','run','install','import']);p.add_argument('input',type=Path);p.add_argument('session',type=Path);p.add_argument('output',type=Path);p.add_argument('--passes',type=int,default=15);a=p.parse_args()
 if a.operation=='prepare':prepare(a.input,a.output)
 elif a.operation=='import':import_session(a.input,a.session)
 elif a.operation=='install':install(a.input,a.session,a.output)
 else:
  if a.output.exists():raise FileExistsError(a.output)
  start=time.time();log=a.output.with_suffix('.log');cmd=['xvfb-run','-a','java','-Xmx3g','-jar','/opt/freerouting/freerouting-1.9.0.jar','-de',str(a.input),'-do',str(a.output),'-mp',str(a.passes),'-mt','1','-oit','1'];record={'command':cmd,'input_sha256':digest(a.input),'jar_sha256':digest('/opt/freerouting/freerouting-1.9.0.jar')}
  try:
   with log.open('w') as f:r=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,timeout=1200)
   record['exit_status']=r.returncode
  except subprocess.TimeoutExpired:record['exit_status']=124
  record.update(elapsed_seconds=time.time()-start,log_sha256=digest(log),session_sha256=digest(a.output) if a.output.exists() else None)
  a.output.with_suffix('.run.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))
  if record['exit_status'] or not a.output.is_file():raise SystemExit(record['exit_status'] or 1)
if __name__=='__main__':main()

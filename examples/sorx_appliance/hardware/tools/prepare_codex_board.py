"""Prepare an explicit six-layer correction checkpoint; preserve all signal nets."""
import argparse,json,re,shutil
from pathlib import Path
import pcbnew

def prepare(src,dst):
 b=pcbnew.LoadBoard(str(src)); oldvias=list(t for t in b.GetTracks() if t.GetClass()=='PCB_VIA')
 seen={}; removed=[]
 for v in oldvias:
  k=(v.GetPosition().x,v.GetPosition().y,v.GetDrill(),v.GetWidth(),v.GetNetCode(),v.TopLayer(),v.BottomLayer())
  if k in seen:
   removed.append({'uuid':v.m_Uuid.AsString(),'retained_uuid':seen[k].m_Uuid.AsString(),'x_mm':pcbnew.ToMM(v.GetPosition().x),'y_mm':pcbnew.ToMM(v.GetPosition().y),'net':str(v.GetNetname())});b.Remove(v)
  else:seen[k]=v
 b.SetCopperLayerCount(6)
 layers=b.GetEnabledLayers();layers.AddLayer(pcbnew.In3_Cu);layers.AddLayer(pcbnew.In4_Cu);b.SetEnabledLayers(layers)
 template=next(z for z in b.Zones() if z.GetLayer()==pcbnew.In1_Cu and str(z.GetNetname())=='GND' and not z.GetIsRuleArea())
 for layer in (pcbnew.In3_Cu,pcbnew.In4_Cu):
  z=template.Duplicate();z.SetLayer(layer);b.Add(z)
 b.GetTitleBlock().SetRevision('B-CODEX-WIP')
 b.GetTitleBlock().SetComment(0,'NOT READY FOR FABRICATION - independent correction checkpoint')
 pcbnew.ZONE_FILLER(b).Fill(b.Zones())
 dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src.with_suffix('.kicad_pro'),dst.with_suffix('.kicad_pro'));pcbnew.SaveBoard(str(dst),b)
 s=dst.read_text();start=s.index('(stackup');depth=0;end=None
 for i in range(start,len(s)):
  if s[i]=='(':depth+=1
  elif s[i]==')':
   depth-=1
   if depth==0:end=i+1;break
 parts=['(stackup']
 for i,(name,diel) in enumerate([('F.Cu',.1),('In1.Cu',.25),('In2.Cu',.69),('In3.Cu',.25),('In4.Cu',.1),('B.Cu',None)]):
  parts.append('(layer "%s" (type "copper") (thickness 0.035))'%name)
  if diel is not None:parts.append('(layer "dielectric %d" (type "%s") (thickness %.3f) (material "FR4") (epsilon_r 4.1) (loss_tangent 0.02))'%(i+1,'prepreg' if i in(0,2,4) else 'core',diel))
 parts.append('(copper_finish "ENIG") (dielectric_constraints yes))')
 s=s[:start]+'\n'.join(parts)+s[end:]
 s=re.sub(r'(\(general\s+\(thickness )[^)]+',r'\g<1>1.6',s,count=1);dst.write_text(s)
 check=pcbnew.LoadBoard(str(dst));assert check.GetCopperLayerCount()==6
 result={'input':str(src),'output':str(dst),'copper_layers':6,'removed_duplicate_vias':removed,'fabricator_impedance_approval':'BLOCKED - proposed stackup requires manufacturer confirmation'}
 dst.with_suffix('.changes.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('input',type=Path);p.add_argument('output',type=Path);a=p.parse_args();prepare(a.input,a.output)

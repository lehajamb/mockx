import sys,json,math
sys.path.insert(0,'/hardware/tools')
import pcbnew
b=pcbnew.LoadBoard('/hardware/runs/route-electrical-12-power.kicad_pcb')
for t in b.GetTracks():
 if t.GetNetname()=='PCIE_RST#_3V3':
  a=(t.GetStart().x/1e6,t.GetStart().y/1e6);c=(t.GetEnd().x/1e6,t.GetEnd().y/1e6)
  if math.dist(a,(37.7285,53.7147))<1.5 or math.dist(c,(37.7285,53.7147))<1.5:print(t.m_Uuid.AsString(),t.GetClass(),a,c,b.GetLayerName(t.GetLayer()),t.GetWidth()/1e6)

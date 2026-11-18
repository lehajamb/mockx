import pcbnew,json
b=pcbnew.LoadBoard('runs/route-electrical-14-pcie.kicad_pcb')
for f in b.GetFootprints():
 for p in f.Pads():
  if p.GetNetname().startswith('MG_TX'):print('pad',f.GetReference(),p.GetNumber(),p.GetNetname(),pcbnew.ToMM(p.GetPosition().x),pcbnew.ToMM(p.GetPosition().y))
for t in b.GetTracks():
 if t.GetNetname()=='MG_TXD0':print('copper',t.GetClass(),str(t.m_Uuid.AsString()),b.GetLayerName(t.GetLayer()),[pcbnew.ToMM(t.GetStart().x),pcbnew.ToMM(t.GetStart().y)],[pcbnew.ToMM(t.GetEnd().x),pcbnew.ToMM(t.GetEnd().y)],pcbnew.ToMM(t.GetWidth()))

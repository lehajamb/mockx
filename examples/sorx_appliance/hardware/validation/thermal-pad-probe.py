import pcbnew,json
b=pcbnew.LoadBoard("/work/hardware/runs/route-verifier-15-ground-fanout-proof.kicad_pcb")
for fp in b.GetFootprints():
 if fp.GetReference() in ("U2","U13"):
  print(fp.GetReference(),fp.GetValue(),fp.GetOrientationDegrees(),[(p.GetNumber(),p.GetNetname(),(p.GetPosition().x/1e6,p.GetPosition().y/1e6),(p.GetSize().x/1e6,p.GetSize().y/1e6)) for p in fp.Pads() if p.GetNetname()=="GND"],flush=True)

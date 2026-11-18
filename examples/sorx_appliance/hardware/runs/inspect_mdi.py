import pcbnew,json
b=pcbnew.LoadBoard('runs/route-electrical-14-pcie.kicad_pcb')
rows=[]
for f in b.GetFootprints():
 x,y=pcbnew.ToMM(f.GetPosition().x),pcbnew.ToMM(f.GetPosition().y)
 if f.GetReference() in ['U10','J6','R57','C69'] or 22<x<38 and 25<y<39:
  rows.append({'ref':f.GetReference(),'value':f.GetValue(),'xy':[x,y],'side':b.GetLayerName(f.GetLayer()),'angle':f.GetOrientationDegrees(),'pads':[{'number':p.GetNumber(),'net':p.GetNetname(),'xy':[pcbnew.ToMM(p.GetPosition().x),pcbnew.ToMM(p.GetPosition().y)]} for p in f.Pads() if f.GetReference()!='U10' or p.GetNetname().startswith('OE_MDI')]})
print(json.dumps(rows,indent=2))

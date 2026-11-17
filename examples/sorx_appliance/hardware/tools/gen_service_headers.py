#!/usr/bin/env python3
"""Wurth service header lands, preserving generic 2.54 mm pin centers.

Drawings61300311121/61300211121 rev003.000 recommend hole1.10 +/-0.15.
Selected finished hole1.20 +/-0.05 is inside that range and exceeds the maximum
0.79mm square-pin diagonal. Positional/assembly qualification remains separate.
"""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def footprint(mpn,count):
 name='Wurth_'+mpn;end=2.54*(count-1)
 lines=[f'(footprint "{name}" (version 20221018) (generator sorx_gen_service_headers) (layer "F.Cu")',
  '  (descr "WR-PHD; finished hole1.20 +/-0.05mm, manufacturer allowed0.95..1.25mm; pin0.64 +/-0.15mm square; body envelope max above PCB8.94mm; populated shunt reserve9.5mm. Qualify positional fit with assembler.")',
  '  (attr through_hole)',
  '  (fp_text reference "REF**" (at 0 -2.3) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))',
  f'  (fp_text value "{name}" (at 0 {end+2.3:.3f}) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))']
 for layer,x0,y0,x1,y1,w in [('F.Fab',-1.27,-1.27,1.27,end+1.27,.10),('F.CrtYd',-1.845,-1.945,1.845,end+1.945,.05)]:
  for a,b in [((x0,y0),(x1,y0)),((x1,y0),(x1,y1)),((x1,y1),(x0,y1)),((x0,y1),(x0,y0))]:
   lines.append(f'  (fp_line (start {a[0]:.3f} {a[1]:.3f}) (end {b[0]:.3f} {b[1]:.3f}) (stroke (width {w}) (type solid)) (layer "{layer}"))')
 # Body side marks stay outside copper; pin1 uses the square copper land.
 for x in (-1.45,1.45):lines.append(f'  (fp_line (start {x} -1.4) (end {x} {end+1.4:.3f}) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))')
 for i in range(count):
  shape='rect' if i==0 else 'oval';lines.append(f'  (pad "{i+1}" thru_hole {shape} (at 0 {i*2.54:.3f}) (size 1.7 1.7) (drill 1.2) (layers "*.Cu" "*.Mask"))')
 return '\n'.join(lines+[')'])+'\n'
if __name__=='__main__':
 for mpn,count in [('61300311121',3),('61300211121',2)]:
  target=ROOT/'hw/sorx.pretty'/('Wurth_'+mpn+'.kicad_mod');target.write_text(footprint(mpn,count),encoding='utf-8');print(target)

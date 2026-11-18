#!/usr/bin/env python3
"""Exact TE 2309409-2 solder land from Toradex guide V1.2 Figure 114.

Coordinate convention deliberately retains the baseline origin and orientation:
module toward +Y, pin-1 end on +X, peg midpoint (-0.0055, -7.926) mm.
The signal contacts are canonicalized without moving their intended positions.
The old related-part hold-downs and oversized nominal peg drills are replaced.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MID_X, PEG_Y = -0.0055, -7.926


def footprint_text():
    lines = [
        '(footprint "SODIMM-260_DDR4_TE_2309409-2"',
        '  (version 20221018) (generator sorx_gen_socket) (layer "F.Cu")',
        '  (descr "TE 2309409-2 land pattern: Toradex Verdin carrier guide V1.2 Fig114, TE customer drawing 2309409. Pin1 at +X, module toward +Y. Body envelope is approximate; use final mechanical model.")',
        '  (tags "SODIMM DDR4 Verdin TE2309409-2") (attr smd)',
        '  (fp_text reference "REF**" (at 0 -15.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))',
        '  (fp_text value "SODIMM-260_DDR4_TE_2309409-2" (at 0 30) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))',
    ]
    for layer, width, x0, y0, x1, y1 in [('F.Fab',0.1,-39.2,-14.2,39.2,6.2),('F.Fab',0.1,-34.8,-8.5,34.8,26.5),('F.CrtYd',0.05,-39.9,-14.7,39.9,27.0)]:
        for a,b in [((x0,y0),(x1,y0)),((x1,y0),(x1,y1)),((x1,y1),(x0,y1)),((x0,y1),(x0,y0))]:
            lines.append(f'  (fp_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (stroke (width {width}) (type solid)) (layer "{layer}"))')
    lines.append('  (fp_text user "1" (at 34.0 -14.3) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))')
    for x,d in [(MID_X-34.4,1.1),(MID_X+34.4,1.6)]:
        lines.append(f'  (pad "" np_thru_hole circle (at {x:.4f} {PEG_Y:.4f}) (size {d} {d}) (drill {d}) (layers "*.Cu" "*.Mask"))')
    for n in range(1,261):
        index = (n-1)//2
        pitch_offset = index*0.5 + (2.0 if index >= 72 else 0.0)
        offset = 1.025 if n%2 else 1.275
        x = MID_X + 34.4 - offset - pitch_offset
        y = PEG_Y + (-4.1 if n%2 else 4.1)
        lines.append(f'  (pad "{n}" smd rect (at {x:.4f} {y:.4f}) (size 0.3 1.75) (layers "F.Cu" "F.Paste" "F.Mask"))')
    for n,x in [(261,MID_X-33.8),(262,MID_X+33.8)]:
        lines.append(f'  (pad "{n}" smd rect (at {x:.4f} {PEG_Y+12:.4f}) (size 3.5 4.6) (layers "F.Cu" "F.Paste" "F.Mask"))')
    return "\n".join(lines+[""])[0:-1] + "\n)\n"


if __name__ == "__main__":
    target = ROOT / "hw/sorx.pretty/SODIMM-260_DDR4_TE_2309409-2.kicad_mod"
    target.write_text(footprint_text(),encoding="utf-8")
    print(target)

#!/usr/bin/env python3
"""Render a board to PNG for visual review: render.py board.kicad_pcb out.png [layers] [x1 y1 x2 y2]"""
import sys, subprocess, os, re
import cairosvg
pcb, out = sys.argv[1], sys.argv[2]
layers = sys.argv[3] if len(sys.argv) > 3 else 'F.Cu,B.Cu,F.Fab,F.CrtYd,Edge.Cuts'
svg = '/tmp/render.svg'
subprocess.run(['kicad-cli', 'pcb', 'export', 'svg', '--layers', layers, '--exclude-drawing-sheet', '--page-size-mode', '2', '-o', svg, pcb], capture_output=True)
s = open(svg).read()
if len(sys.argv) > 7:
    x1, y1, x2, y2 = map(float, sys.argv[4:8])
    m = re.search(r'viewBox="([-\d. ]+)"', s); vb = [float(v) for v in m.group(1).split()]
    import pcbnew
    bb = pcbnew.LoadBoard(pcb).GetBoundingBox(); x1 -= bb.GetX() / 1e6; x2 -= bb.GetX() / 1e6; y1 -= bb.GetY() / 1e6; y2 -= bb.GetY() / 1e6
    s = re.sub(r'viewBox="[-\d. ]+"', 'viewBox="%f %f %f %f"' % (vb[0] + x1, vb[1] + y1, x2 - x1, y2 - y1), s, count=1)
    s = re.sub(r'width="[\d.]+mm"', 'width="%fmm"' % (x2 - x1), s, count=1); s = re.sub(r'height="[\d.]+mm"', 'height="%fmm"' % (y2 - y1), s, count=1)
cairosvg.svg2png(bytestring=s.encode(), write_to=out, output_width=int(sys.argv[8]) if len(sys.argv) > 8 else 1500, background_color='white')

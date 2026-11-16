#!/usr/bin/env python3
"""Dump footprint positions and body rectangles from the board for the mechanical model."""
import sys, json, pcbnew
b = pcbnew.LoadBoard(sys.argv[1]); out = []
for f in b.GetFootprints():
    side = 'B' if f.GetLayer() == pcbnew.B_Cu else 'F'
    xs, ys = [], []
    for g in f.GraphicalItems():
        if g.GetLayer() in (pcbnew.F_Fab, pcbnew.B_Fab) and isinstance(g, pcbnew.FP_SHAPE):
            q = g.GetBoundingBox(); xs += [q.GetLeft(), q.GetRight()]; ys += [q.GetTop(), q.GetBottom()]
    if not xs:
        for p in f.Pads():
            q = p.GetBoundingBox(); xs += [q.GetLeft(), q.GetRight()]; ys += [q.GetTop(), q.GetBottom()]
    if not xs: continue
    p = f.GetPosition()
    out.append(dict(ref=f.GetReference(), value=f.GetValue(), fp=str(f.GetFPID().GetLibItemName()), side=side, x=pcbnew.ToMM(p.x), y=pcbnew.ToMM(p.y), rot=f.GetOrientationDegrees(),
                    box=[pcbnew.ToMM(v) for v in (min(xs), min(ys), max(xs), max(ys))]))
json.dump(out, open(sys.argv[2], 'w'), indent=0)
print(len(out), 'footprints')

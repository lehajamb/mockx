#!/usr/bin/env python3
"""Place every footprint of design.py on the SORX carrier and write hw/build/sorx_placed.kicad_pcb.

Fixed positions for connectors and main ICs are listed in FIX; passives and small parts are
placed automatically next to the pin they serve.  Routing is done by route.py.
"""
import os, sys, math, json
import pcbnew
from design import DESIGN
from gen_sch import resolve

HERE = os.path.dirname(os.path.abspath(__file__))
HW = os.path.join(HERE, '..', 'hw')
BUILD = os.path.join(HW, 'build')
KFP = '/usr/share/kicad/footprints/'
W, H = 172.0, 118.0          # board outline, mm.  Rear edge is y = 0, front edge is y = H.
XS, YS = 78.0, 64.0          # SoM socket centre
JY = 25.2                    # magjack pin-1 row; jack front protrudes 1.0 mm past the rear edge
mm = pcbnew.FromMM

FIX = {  # ref: (x, y, rotation)
    'J1': (XS, YS, 0),
    'J6': (25.5, JY, 90), 'J4': (45.5, JY, 90), 'J7': (65.5, JY, 90),          # OE, MD-IN, MGMT
    'J3': (14.0, 3.68, 180), 'J5': (100.0, None, 90),                           # USB-C PWR, PPS SMA
    'J8': (68.0, 110.62, 0), 'J9': (86.0, 110.62, 0),                           # USB-A 1 / 2
    'J11': (99.0, 104.97, 0), 'J10': (118.0, 114.32, 0),                      # console RJ45, USB-C debug
    'SW1': (132.0, 113.6, 180), 'SW2': (142.0, 113.6, 180), 'J12': (156.0, 109.6, 0),
    'D4': (14.0, 116.7, 180), 'D5': (20.0, 116.7, 180), 'D6': (26.0, 116.7, 180), 'D7': (32.0, 116.7, 180),
    'D8': (38.0, 116.7, 180), 'D9': (44.0, 116.7, 180), 'D10': (50.0, 116.7, 180),
    'D2': (6.0, 11.5, 90), 'U2': (15.5, 13.0, 0), 'Q1': (6.5, 23.0, 0), 'Q2': (14.5, 21.0, 0), 'U3': (10.5, 34.0, 0), 'C23': (7.0, 47.5, 0),
    'U4': (8.0, 59.0, 0), 'L1': (19.0, 59.0, 0), 'U5': (8.0, 77.0, 0), 'L2': (19.0, 77.0, 0), 'U6': (8.0, 95.0, 0), 'L3': (19.0, 95.0, 0),
    'U10': (30.6, 41.5, 'mdi_up', 'B'),
    'U13': (70.6, 39.5, 270), 'Y2': (80.5, 36.0, 0), 'U7': (90.0, 40.0, 0), 'FB1': (78.5, 42.5, 0),
    'U8': (95.0, 13.5, 0), 'U9': (105.0, 13.5, 0), 'D3': (100.0, 9.5, 0),
    'J13': (136.0, 8.0, 0), 'BT1': (152.0, 30.0, 0), 'J2': (116.0, 24.0, 90),
    'U1': (124.0, 46.0, 0), 'U30': (126.0, 62.0, 0), 'U29': (140.0, 62.0, 0), 'U32': (126.0, 76.0, 0),
    'U27': (104.0, 95.5, 0), 'U24': (118.0, 100.5, 0), 'U17': (57.0, 96.0, 0), 'C106': (54.5, 107.0, 90), 'U21': (77.0, 96.0, 0), 'C110': (93.0, 95.6, 0),
    'H1': (4.5, 4.5, 0), 'H2': (167.5, 4.5, 0), 'H3': (4.5, 113.5, 0), 'H4': (167.5, 113.5, 0),
    'FID1': (7.0, 107.5, 0), 'FID2': (166.0, 46.0, 0), 'FID3': (120.0, 4.0, 0),
}
KEEPOUT = [  # (x1, y1, x2, y2, reason) -- no components
    (XS - 35.6, YS - 2.2, XS + 35.6, YS + 27.3, 'SoM module (no parts under module with 5.2 mm socket)'),
    (45.3, 31.5, 56.2, YS - 13.2, 'MD-IN MDI pair corridor'),
    (64.0, 31.5, 77.5, 35.2, 'MGMT MDI pair corridor'),
    (XS - 34.0, YS - 16.2, XS + 34.0, YS - 12.9, 'socket odd-row via fan-out strip'),
]
POWER = {'GND', 'VSYS', 'VBUS_IN', 'VIN_PROT'}


GAP = 0.6    # courtyard-to-courtyard spacing of auto-placed parts: leaves room for a via between any two parts
ESC = 1.3    # extra clearance around fine-pitch ICs


def is_power(n): return n in POWER or n.startswith('+')


def fp_path(fpid):
    lib, name = fpid.split(':')
    return (os.path.join(HW, 'sorx.pretty') if lib == 'sorx' else KFP + lib + '.pretty'), name


def api_rect(f):
    """Courtyard rectangle of a footprint as currently positioned (falls back to pads)."""
    xs, ys = [], []
    for g in f.GraphicalItems():
        if g.GetLayer() in (pcbnew.F_CrtYd, pcbnew.B_CrtYd) and isinstance(g, pcbnew.FP_SHAPE):
            q = g.GetBoundingBox(); xs += [q.GetLeft(), q.GetRight()]; ys += [q.GetTop(), q.GetBottom()]
    if not xs:
        for p in f.Pads():
            q = p.GetBoundingBox(); xs += [q.GetLeft() - mm(0.25), q.GetRight() + mm(0.25)]; ys += [q.GetTop() - mm(0.25), q.GetBottom() + mm(0.25)]
    if not xs: return (-3.2, -3.2, 3.2, 3.2)
    return tuple(pcbnew.ToMM(v) for v in (min(xs), min(ys), max(xs), max(ys)))


def rect_at(f, rot):
    f.SetPosition(pcbnew.VECTOR2I(0, 0)); f.SetOrientationDegrees(rot)
    return api_rect(f)


def shift(r, x, y): return (r[0] + x, r[1] + y, r[2] + x, r[3] + y)


def overlap(a, b, m=0.0):
    return a[0] < b[2] + m and b[0] < a[2] + m and a[1] < b[3] + m and b[1] < a[3] + m


def main():
    os.makedirs(BUILD, exist_ok=True)
    board = pcbnew.BOARD(); board.SetCopperLayerCount(4)
    nets = {}

    def net(n):
        if n not in nets:
            ni = pcbnew.NETINFO_ITEM(board, n); board.Add(ni); nets[n] = ni
        return nets[n]
    P = lambda x, y: pcbnew.VECTOR2I(mm(x), mm(y))
    corners = [(0, 0), (W, 0), (W, H), (0, H)]
    for i in range(4):
        s = pcbnew.PCB_SHAPE(board); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetStart(P(*corners[i])); s.SetEnd(P(*corners[(i + 1) % 4]))
        s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(mm(0.1)); board.Add(s)

    placed = {}     # ref -> (footprint, rect, side)
    rects = {'F': [], 'B': []}
    netpads = {}    # net -> list of (ref, x, y)
    decap_count = {}
    report = []

    def load(part, side):
        path, name = fp_path(part.fp)
        f = pcbnew.FootprintLoad(path, name)
        assert f is not None, part.fp
        f.SetReference(part.ref); f.SetValue(part.value)
        f.SetFPID(pcbnew.LIB_ID(part.fp.split(':')[0], part.fp.split(':')[1]))
        f.SetPosition(pcbnew.VECTOR2I(0, 0)); board.Add(f)
        if side == 'B': f.Flip(pcbnew.VECTOR2I(0, 0), False)
        return f

    def commit(part, f, x, y, rot, side):
        f.SetOrientationDegrees(rot); f.SetPosition(P(x, y))
        r = api_rect(f); placed[part.ref] = (f, r, side)
        e = esc(part, f) if part.ref not in FIX else 0.0   # fixed parts get their escape ring after the fixed-overlap check
        rects[side].append((part.ref, (r[0] - e, r[1] - e, r[2] + e, r[3] + e)))
        pn = resolve(part); padnums = {p.GetNumber() for p in f.Pads()}
        for num, n in pn.items():
            if n: assert num in padnums, 'footprint %s of %s has no pad %s' % (part.fp, part.ref, num)
        for pad in f.Pads():
            n = pn.get(pad.GetNumber())
            pos = pad.GetPosition(); xy = (pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y))
            if pad.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):   # through-hole pins block the other side too
                q = pad.GetBoundingBox(); rr = tuple(pcbnew.ToMM(v) for v in (q.GetLeft(), q.GetTop(), q.GetRight(), q.GetBottom()))
                rects['B' if side == 'F' else 'F'].append((part.ref + ':' + pad.GetNumber(), (rr[0] - 0.3, rr[1] - 0.3, rr[2] + 0.3, rr[3] + 0.3)))
            if n:
                pad.SetNet(net(n)); netpads.setdefault(n, []).append((part.ref, xy[0], xy[1]))
        if part.dnp: f.SetAttributes(f.GetAttributes() | pcbnew.FP_EXCLUDE_FROM_POS_FILES | pcbnew.FP_EXCLUDE_FROM_BOM)
        if not part.inbom: f.SetAttributes(f.GetAttributes() | pcbnew.FP_EXCLUDE_FROM_BOM)

    def esc(part, f):
        """Escape ring: fine-pitch ICs keep neighbours far enough away for two rows of fan-out vias."""
        return ESC if part.ref.startswith('U') and len(list(f.Pads())) >= 14 else 0.0

    def free(r, side, margin):
        if r[0] < 0.9 or r[1] < 0.9 or r[2] > W - 0.9 or r[3] > H - 0.9: return False
        if side == 'F':
            for k in KEEPOUT:
                if overlap(r, k): return False
        for _, q in rects[side]:
            if overlap(r, q, margin): return False
        return True

    def spiral(f, tx, ty, side, margin):
        r0 = {rot: rect_at(f, rot) for rot in (0, 90)}
        for i in range(0, 110):
            rad = i * 0.45
            nstep = max(1, int(2 * math.pi * rad / 0.45))
            for k in range(nstep):
                a = 2 * math.pi * k / nstep
                x = round(tx + rad * math.cos(a), 2); y = round(ty + rad * math.sin(a), 2)
                for rot in (0, 90):
                    if free(shift(r0[rot], x, y), side, margin): return x, y, rot
        raise RuntimeError('no room')

    parts = [p for p in DESIGN.parts.values() if p.fp]
    side_of = {}
    # 1) fixed parts
    for p in parts:
        if p.ref in FIX:
            fx = FIX[p.ref]; x, y, rot = fx[:3]; side = fx[3] if len(fx) > 3 else 'F'
            f = load(p, side)
            if y is None:   # SMA edge launch: pad ends flush with the rear edge
                f.SetOrientationDegrees(0); y = max(pcbnew.ToMM(q.GetBoundingBox().GetRight()) for q in f.Pads())
            if rot == 'mdi_up':   # I210 on the bottom side: MDI pins face the jack, MDI3 on the left (planar to the jack pin order)
                for cand in (0, 90, 180, 270):
                    f.SetOrientationDegrees(cand); f.SetPosition(pcbnew.VECTOR2I(0, 0))
                    pp = {q.GetNumber(): q.GetPosition() for q in f.Pads()}
                    if pp['53'].y < -mm(3) and pp['50'].x < pp['58'].x: rot = cand
                assert rot != 'mdi_up', 'no planar I210 orientation'
            commit(p, f, x, y, rot, side); side_of[p.ref] = side
    for sd in 'FB':
        rr = [q for q in rects[sd] if ':' not in q[0]]
        for a in range(len(rr)):
            for b in range(a + 1, len(rr)):
                if overlap(rr[a][1], rr[b][1]): report.append('OVERLAP fixed %s %s' % (rr[a][0], rr[b][0]))
    for sd in 'FB':
        for i, (ref, r) in enumerate(rects[sd]):
            if ref in placed and ref in FIX:
                e = esc(DESIGN.parts[ref], placed[ref][0])
                if e: rects[sd][i] = (ref, (r[0] - e, r[1] - e, r[2] + e, r[3] + e))
    # 2) everything else, next to what it serves, on the side of its anchor
    anchor = {}
    last = None
    for p in DESIGN.parts.values():
        if p.ref in FIX: last = p.ref
        elif p.fp: anchor[p.ref] = last
    for p in parts:
        if p.ref in FIX: continue
        a = anchor.get(p.ref); side = side_of.get(a, 'F')
        if p.sym == 'SORX:TP': side = 'F'
        f = load(p, side); pn = resolve(p)
        sig = [n for n in pn.values() if n and not is_power(n)]
        pwr = [n for n in pn.values() if n and is_power(n) and n != 'GND']
        tgt = None
        cands = []
        for n in sig:   # nearest already-placed pad on a shared signal net, preferring the anchor
            for (r, x, y) in netpads.get(n, []):
                cands.append((0 if r == a else 1, r, x, y))
        if cands:
            cands.sort(key=lambda c: c[0]); c = cands[0]
            same = [(x, y) for (pri, r, x, y) in cands if r == c[1] and pri == c[0]]
            tgt = (sum(q[0] for q in same) / len(same), sum(q[1] for q in same) / len(same))
        elif pwr and a:
            opts = [(decap_count.get((a, round(x, 1), round(y, 1)), 0), x, y) for (r, x, y) in netpads.get(pwr[0], []) if r == a]
            if opts:
                opts.sort(); _, x, y = opts[0]; tgt = (x, y); decap_count[(a, round(x, 1), round(y, 1))] = opts[0][0] + 1
        if tgt is None and pwr:
            q = netpads.get(pwr[0], []); tgt = (q[-1][1], q[-1][2]) if q else None
        if tgt is None:
            if a: r = placed[a][1]; tgt = ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
            else: tgt = (W / 2, 10)
        x, y, rot = spiral(f, tgt[0], tgt[1], side, GAP + esc(p, f))
        commit(p, f, x, y, rot, side); side_of[p.ref] = side
    # zones: both inner layers are solid GND planes
    for layer in (pcbnew.In1_Cu, pcbnew.In2_Cu):
        z = pcbnew.ZONE(board); z.SetLayer(layer); z.SetNet(net('GND')); z.SetIsFilled(False)
        o = z.Outline(); o.NewOutline()
        for (x, y) in ((0.4, 0.4), (W - 0.4, 0.4), (W - 0.4, H - 0.4), (0.4, H - 0.4)): o.Append(mm(x), mm(y))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL); z.SetLocalClearance(mm(0.2)); z.SetMinThickness(mm(0.2)); board.Add(z)
    out = os.path.join(BUILD, 'sorx_placed.kicad_pcb')
    pcbnew.SaveBoard(out, board)
    json.dump({'W': W, 'H': H, 'XS': XS, 'YS': YS, 'rects': rects['F'], 'rects_b': rects['B'], 'keepout': KEEPOUT, 'bottom': sorted(r for r, sd in side_of.items() if sd == 'B')}, open(os.path.join(BUILD, 'placement.json'), 'w'))
    print('placed', len(placed), 'footprints;', len(nets), 'nets;', 'issues:', report or 'none')


if __name__ == '__main__':
    main()

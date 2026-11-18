#!/usr/bin/env python3
"""Post-route finishing for the SORX carrier.

  widen   - widen power tracks to the largest width that keeps 0.18 mm clearance (the autorouter needs narrow tracks to leave fine-pitch pads)
  ground  - GND pours on both outer layers, stitching vias to the two inner GND planes, explicit vias for GND pads the pour cannot reach
  final   - silkscreen, stackup, save hw/sorx.kicad_pcb
Each stage runs in its own process (pcbnew is not re-entrant): finish.py widen|ground|fixgnd|final
"""
import os, sys, re, math, json, subprocess
import pcbnew
import proj
from shapely.geometry import LineString, Point, box as sbox
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__)); HW = os.path.join(HERE, '..', 'hw'); B = os.path.join(HW, 'build') + '/'
mm = pcbnew.FromMM; MM = pcbnew.ToMM
W, H = 172.0, 118.0
CLR = 0.18
OUTER = (pcbnew.F_Cu, pcbnew.B_Cu)
CU = (pcbnew.F_Cu, pcbnew.B_Cu, pcbnew.In2_Cu)   # layers that carry tracks: In2 holds the supply rails


def load(name):
    proj.write(B + name + '.kicad_pro', name); return pcbnew.LoadBoard(B + name + '.kicad_pcb')


def save(board, name):
    proj.write(B + name + '.kicad_pro', name); pcbnew.SaveBoard(B + name + '.kicad_pcb', board)


class Geo:
    """Copper obstacles on the two outer layers as shapely shapes."""
    def __init__(self, board):
        self.items = {l: [] for l in CU}   # (geom, net, kind, obj)
        for f in board.GetFootprints():
            for p in f.Pads():
                q = p.GetBoundingBox(); g = sbox(MM(q.GetLeft()), MM(q.GetTop()), MM(q.GetRight()), MM(q.GetBottom()))
                for l in CU:
                    if p.IsOnLayer(l) or p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH): self.items[l].append((g, p.GetNetname(), 'pad', p))
        for t in board.GetTracks():
            if t.GetClass() == 'PCB_VIA':
                c = t.GetPosition(); g = Point(MM(c.x), MM(c.y)).buffer(MM(t.GetWidth()) / 2, 16)
                for l in CU: self.items[l].append((g, t.GetNetname(), 'via', t))
            elif t.GetLayer() in CU:
                self.items[t.GetLayer()].append((self.track_geom(t, MM(t.GetWidth())), t.GetNetname(), 'track', t))
        self.index()

    @staticmethod
    def track_geom(t, w):
        a, b = t.GetStart(), t.GetEnd()
        return LineString([(MM(a.x), MM(a.y)), (MM(b.x), MM(b.y))]).buffer(w / 2, 8)

    def index(self):
        self.tree = {l: STRtree([i[0] for i in self.items[l]]) for l in CU}

    def add(self, layer, geom, net, kind, obj=None):
        self.items[layer].append((geom, net, kind, obj))

    def clear(self, layer, geom, net, clearance, ignore_pads=False, extra=()):
        """True if geom keeps `clearance` to every other-net item on the layer (and to any pad if not ignore_pads)."""
        probe = geom.buffer(clearance, 8)
        for i in self.tree[layer].query(probe):
            g, n, kind, _ = self.items[layer][int(i)]
            if n == net and not (kind == 'pad' and not ignore_pads and False): continue
            if g.intersects(probe): return False
        for (g, n) in extra:
            if n != net and g.intersects(probe): return False
        return True


def widen():
    b = load('sorx_rp'); geo = Geo(b)
    targets = {'PWR_HI': (0.8, 0.65, 0.5), 'PWR': (0.5, 0.4, 0.3)}
    stats = {}; changed = []
    tracks = [(l, i) for l in CU for i, it in enumerate(geo.items[l]) if it[2] == 'track' and proj.classify(it[1]) in targets]
    for l, i in tracks:
        g, n, kind, t = geo.items[l][i]; cur = MM(t.GetWidth()); cls = proj.classify(n)
        for w in targets[cls]:
            if w <= cur: break
            ng = Geo.track_geom(t, w)
            mnx, mny, mxx, mxy = ng.bounds
            if mnx < 0.35 or mny < 0.35 or mxx > W - 0.35 or mxy > H - 0.35: continue
            if geo.clear(l, ng, n, CLR, extra=[(c[0], c[1]) for c in changed if c[2] == l]):
                t.SetWidth(mm(w)); changed.append((ng, n, l)); break
        length = MM(t.GetLength()); s = stats.setdefault(n, {}); wk = '%.2f' % MM(t.GetWidth()); s[wk] = s.get(wk, 0.0) + length
    save(b, 'sorx_w')
    json.dump(stats, open(B + 'power_widths.json', 'w'), indent=1)
    tot = {}
    for n, s in stats.items():
        for wk, L in s.items(): tot[wk] = tot.get(wk, 0) + L
    print('power track length by width (mm):', {k: round(v, 1) for k, v in sorted(tot.items())})


def add_via(board, x, y, net, dia=0.6, drill=0.3):
    v = pcbnew.PCB_VIA(board); v.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y))); v.SetWidth(mm(dia)); v.SetDrill(mm(drill)); v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(net); board.Add(v); return v


def add_track(board, a, b_, layer, net, w):
    t = pcbnew.PCB_TRACK(board); t.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1]))); t.SetEnd(pcbnew.VECTOR2I(mm(b_[0]), mm(b_[1]))); t.SetWidth(mm(w)); t.SetLayer(layer); t.SetNet(net); board.Add(t); return t


def ground():
    b = load('sorx_w'); gnd = b.GetNetsByName()['GND']
    for layer in OUTER:
        z = pcbnew.ZONE(b); z.SetLayer(layer); z.SetNet(gnd)
        o = z.Outline(); o.NewOutline()
        for (x, y) in ((0.4, 0.4), (W - 0.4, 0.4), (W - 0.4, H - 0.4), (0.4, H - 0.4)): o.Append(mm(x), mm(y))
        z.SetLocalClearance(mm(0.2)); z.SetMinThickness(mm(0.2)); b.Add(z)
    for z in b.Zones():
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_THT_THERMAL); z.SetThermalReliefGap(mm(0.25)); z.SetThermalReliefSpokeWidth(mm(0.4)); z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    from shapely.geometry import Polygon
    ko = []   # footprint keep-out areas (e.g. under the microSD socket): no stitching vias there
    for f in b.GetFootprints():
        for z in f.Zones():
            if not z.GetIsRuleArea(): continue
            o = z.Outline()
            for k in range(o.OutlineCount()):
                ch = o.Outline(k); pp = [(MM(ch.CPoint(i).x), MM(ch.CPoint(i).y)) for i in range(ch.PointCount())]
                if len(pp) >= 3: ko.append(Polygon(pp).buffer(0.4))
    geo = Geo(b); n = 0; new = []
    step = 3.5
    y = 2.0
    while y < H - 1.9:
        x = 2.0 + (step / 2 if int(y / step) % 2 else 0)
        while x < W - 1.9:
            g = Point(x, y).buffer(0.3, 16)
            ok = all(geo.clear(l, g, 'GND', 0.22) for l in CU) and not any(k.intersects(g) for k in ko)
            if ok:   # also stay off every pad, including GND pads (no via-in-pad)
                probe = g.buffer(0.25)
                for l in OUTER:
                    for i in geo.tree[l].query(probe):
                        it = geo.items[l][int(i)]
                        if it[2] == 'pad' and it[0].intersects(probe): ok = False; break
                    if not ok: break
            if ok and all(Point(x, y).distance(Point(*q)) > 1.2 for q in new[-60:]):
                add_via(b, x, y, gnd); new.append((x, y)); n += 1
            x += step
        y += step
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    save(b, 'sorx_g')
    print('stitching vias added:', n)


def drc(name, out):
    b = pcbnew.LoadBoard(B + name + '.kicad_pcb')
    pcbnew.WriteDRCReport(b, out, pcbnew.EDA_UNITS_MILLIMETRES, True)


def parse_drc(path):
    txt = open(path).read()
    secs = re.split(r'\n\*\* ', txt)
    res = {'violations': [], 'unconnected': []}
    for s in secs:
        key = 'unconnected' if s.startswith('Found') and 'unconnected' in s.split('\n')[0] else 'violations' if s.startswith('Found') else None
        if not key: continue
        for m in re.finditer(r'\[(\w+)\]: ([^\n]*)\n((?:    [^\n]*\n?)+)', s):
            res[key].append((m.group(1), m.group(2), m.group(3)))
    return res


def fixgnd(src='sorx_g', dst='sorx_g2'):
    """Give every GND pad the pours could not reach its own via to the inner planes."""
    rpt = B + 'drc_tmp.rpt'
    subprocess.run([sys.executable, os.path.abspath(__file__), 'drc', src, rpt], check=True, capture_output=True)
    un = parse_drc(rpt)['unconnected']
    want = set()
    for typ, msg, body in un:
        for m in re.finditer(r'Pad (\S+) \[GND\] of (\S+)', body): want.add((m.group(2), m.group(1)))
    b = load(src); gnd = b.GetNetsByName()['GND']; geo = Geo(b); added = 0; failed = []; extra = []
    for f in b.GetFootprints():
        for p in f.Pads():
            if (f.GetReference(), p.GetNumber()) not in want or p.GetNetname() != 'GND': continue
            if p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH: continue
            layer = pcbnew.F_Cu if p.IsOnLayer(pcbnew.F_Cu) else pcbnew.B_Cu
            c = p.GetPosition(); px, py = MM(c.x), MM(c.y); q = p.GetBoundingBox(); pw, ph = MM(q.GetWidth()), MM(q.GetHeight())
            tw = max(0.15, min(0.3, min(pw, ph) - 0.02))
            dirs = [(0, 1), (0, -1), (1, 0), (-1, 0)] if ph >= pw else [(1, 0), (-1, 0), (0, 1), (0, -1)]
            dirs += [(0.707, 0.707), (-0.707, 0.707), (0.707, -0.707), (-0.707, -0.707)]
            done = False
            for dist_extra in (0.55, 0.8, 1.1, 1.5, 2.0, 2.6, 3.3):
                for dx, dy in dirs:
                    half = (ph if abs(dy) > abs(dx) else pw) / 2
                    vx, vy = px + dx * (half + dist_extra), py + dy * (half + dist_extra)
                    if not (1.0 < vx < W - 1.0 and 1.0 < vy < H - 1.0): continue
                    vg = Point(vx, vy).buffer(0.3, 16); tg = LineString([(px, py), (vx, vy)]).buffer(tw / 2, 8)
                    if not all(geo.clear(l, vg, 'GND', 0.2, extra=extra) for l in OUTER): continue
                    if not geo.clear(layer, tg, 'GND', 0.16, extra=extra): continue
                    hit = False     # do not land the via on somebody else's pad or on another GND pad
                    for l in OUTER:
                        for i in geo.tree[l].query(vg.buffer(0.2)):
                            it = geo.items[l][int(i)]
                            if it[2] == 'pad' and it[3] is not p and it[0].intersects(vg.buffer(0.2)): hit = True
                    if hit: continue
                    add_via(b, vx, vy, gnd); add_track(b, (px, py), (vx, vy), layer, gnd, tw); extra.append((vg, 'GND')); added += 1; done = True; break
                if done: break
            if not done: failed.append('%s.%s' % (f.GetReference(), p.GetNumber()))
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    save(b, dst)
    print('GND pads needing a via: %d, added: %d, no room: %s' % (len(want), added, failed[:20]))


STACKUP = """(stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Paste" (type "Top Solder Paste"))
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "prepreg") (thickness 0.1) (material "FR4") (epsilon_r 4.1) (loss_tangent 0.02))
      (layer "In1.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 2" (type "core") (thickness 1.265) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "In2.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 3" (type "prepreg") (thickness 0.1) (material "FR4") (epsilon_r 4.1) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
      (layer "B.Paste" (type "Bottom Solder Paste"))
      (layer "B.SilkS" (type "Bottom Silk Screen"))
      (copper_finish "ENIG")
      (dielectric_constraints yes)
    )
    """
SILK = [  # text, x, y, size, rotation
    ('PWR 15-20V PD', 14.0, 9.6, 0.9, 0), ('OE', 30.6, 33.6, 1.2, 0), ('MD-IN', 50.6, 33.6, 1.2, 0), ('MGMT', 70.6, 33.6, 1.2, 0), ('PPS IN', 100.0, 6.3, 1.0, 0),
    ('FAN', 139.8, 13.6, 1.0, 0), ('CR2032 +', 152.0, 42.0, 1.0, 0), ('USB 1', 68.0, 99.9, 1.0, 0), ('USB 2 / REC', 86.0, 99.9, 1.0, 0), ('CONSOLE', 103.4, 99.6, 1.0, 0),
    ('DEBUG', 118.0, 108.2, 0.9, 0), ('RST', 129.7, 108.0, 0.9, 0), ('REC', 139.7, 108.0, 0.9, 0), ('SD', 156.0, 100.2, 1.0, 0),
    ('PWR', 14.0, 113.6, 0.8, 90), ('SYS', 20.0, 113.6, 0.8, 90), ('FEED A', 26.0, 112.6, 0.8, 90), ('FEED B', 32.0, 112.6, 0.8, 90), ('FIX A', 38.0, 113.0, 0.8, 90),
    ('FIX B', 44.0, 113.0, 0.8, 90), ('ALARM', 50.0, 112.8, 0.8, 90),
    ('SORX CARRIER  REV A  2026-11', 140.0, 88.0, 1.5, 0), ('Verdin iMX8M Plus  J1', 78.0, 94.2, 1.0, 0),
]


def final():
    b = load('sorx_g2')
    for f in b.GetFootprints():
        bottom = f.GetLayer() == pcbnew.B_Cu
        r = f.Reference(); r.SetLayer(pcbnew.B_Fab if bottom else pcbnew.F_Fab); r.SetTextSize(pcbnew.VECTOR2I(mm(0.6), mm(0.6))); r.SetTextThickness(mm(0.1)); r.SetVisible(True)
        r.SetPosition(f.GetPosition())
        f.Value().SetVisible(False)
        for g in list(f.GraphicalItems()):
            if isinstance(g, pcbnew.FP_TEXT): g.SetVisible(False)
    for t, x, y, sz, rot in SILK:
        g = pcbnew.PCB_TEXT(b); g.SetText(t); g.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y))); g.SetLayer(pcbnew.F_SilkS)
        g.SetTextSize(pcbnew.VECTOR2I(mm(sz), mm(sz))); g.SetTextThickness(mm(max(0.12, sz * 0.15))); g.SetTextAngleDegrees(rot); b.Add(g)
    tb = b.GetTitleBlock(); tb.SetTitle('SORX carrier board'); tb.SetRevision('A'); tb.SetDate('2026-11-16'); tb.SetCompany('SORX')
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    out = os.path.join(HW, 'sorx.kicad_pcb'); pcbnew.SaveBoard(out, b)
    s = open(out).read()
    assert '(stackup' not in s
    i = s.index('(setup') + len('(setup'); j = s.index('(', i)
    s = s[:j] + STACKUP + s[j:]
    s = s.replace('(general\n    (thickness 1.6)', '(general\n    (thickness 1.605)')
    open(out, 'w').write(s)
    proj.write(os.path.join(HW, 'sorx.kicad_pro'), 'sorx')
    b2 = pcbnew.LoadBoard(out); pcbnew.SaveBoard(out, b2)     # round-trip to prove the stackup parses
    assert '(stackup' in open(out).read()
    print('final board written:', out)


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'final': final(); sys.exit(0)
    if cmd == 'widen': widen()
    elif cmd == 'ground': ground()
    elif cmd == 'fixgnd': fixgnd(*sys.argv[2:])
    elif cmd == 'drc': drc(sys.argv[2], sys.argv[3])
    elif cmd == 'summary':
        r = parse_drc(sys.argv[2]); import collections
        print('violations:', dict(collections.Counter(v[0] for v in r['violations'])))
        print('unconnected:', len(r['unconnected']))
        nets = collections.Counter()
        for typ, msg, body in r['unconnected']:
            for m in re.finditer(r'\[([^\]]+)\]', body): nets[m.group(1)] += 1
        print('unconnected by net:', dict(nets.most_common(25)))

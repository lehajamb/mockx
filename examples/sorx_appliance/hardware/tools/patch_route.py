#!/usr/bin/env python3
"""Completion router for the connections the autorouter leaves open.

    patch_route.py <board-in> <board-out> [rounds]         open signal/power connections, with local rip-up and retry
    patch_route.py gnd <board-in> <board-out>              GND copper on the outer layers without a path to the planes

Runs the KiCad DRC, reads its unconnected-items list, then routes each open connection on F.Cu/B.Cu with an
A* search on a 0.05 mm grid.  Obstacles are all copper and holes of other nets, grown by clearance + half the
track width (or via radius), so the result is correct by construction; the KiCad DRC run afterwards is the check.
GND is skipped: it is connected by the pours and stitching vias added in finish.py.
"""
import sys, os, re, math, heapq, subprocess
import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import Polygon, LineString, Point
import pcbnew

RES = 0.05; W_TRK = 0.15; VIA_D = 0.6; VIA_DR = 0.3; CLR = 0.15; EPS = 0.045; EDGE = 0.5; HOLE_EXTRA = 0.12
WIN = 14.0; VIA_COST = 40.0; RIP_COST = 3.0
LAY = {'F': pcbnew.F_Cu, 'B': pcbnew.B_Cu}
mm = pcbnew.FromMM


def pts(chain): return [(pcbnew.ToMM(chain.CPoint(i).x), pcbnew.ToMM(chain.CPoint(i).y)) for i in range(chain.PointCount())]


class Grid:
    def __init__(self, board):
        bb = board.GetBoardEdgesBoundingBox()
        self.x0, self.y0 = pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()); self.w, self.h = pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())
        self.nx, self.ny = int(round(self.w / RES)) + 1, int(round(self.h / RES)) + 1
        self.T = {l: np.zeros((self.ny, self.nx), np.uint8) for l in 'FB'}    # blocked for a track centre
        self.V = {l: np.zeros((self.ny, self.nx), np.uint8) for l in 'FBI'}   # blocked for a via centre (I = In2, the supply layer)
        self.S = {l: np.zeros((self.ny, self.nx), np.uint8) for l in 'FB'}    # part of T that comes from rippable tracks/vias
        self.VS = {l: np.zeros((self.ny, self.nx), np.uint8) for l in 'FBI'}   # part of V that comes from rippable tracks/vias
        self.items = {}                                                        # net -> [[layers, shape, hole, track-or-None]]
        self.protected = set()                                                 # nets whose tracks may not be ripped up
        e = int(round(EDGE / RES))
        for m in list(self.T.values()) + list(self.V.values()):
            m[:e, :] = 1; m[-e:, :] = 1; m[:, :e] = 1; m[:, -e:] = 1

    def cell(self, x, y): return int(round((x - self.x0) / RES)), int(round((y - self.y0) / RES))
    def xy(self, ix, iy): return self.x0 + ix * RES, self.y0 + iy * RES

    def stamp(self, arr, shape, d, sign=1, win=None):
        g = shape.buffer(d, 8) if d else shape
        if g.is_empty: return
        bx0, by0, bx1, by1 = g.bounds
        i0, j0 = self.cell(bx0, by0); i1, j1 = self.cell(bx1, by1); i0 -= 1; j0 -= 1; i1 += 1; j1 += 1
        ox, oy = (0, 0) if win is None else win
        ny, nx = arr.shape
        a0, b0, a1, b1 = max(i0 - ox, 0), max(j0 - oy, 0), min(i1 - ox, nx - 1), min(j1 - oy, ny - 1)
        if a1 < a0 or b1 < b0: return
        im = Image.new('1', (a1 - a0 + 1, b1 - b0 + 1), 0); dr = ImageDraw.Draw(im)
        for poly in (g.geoms if hasattr(g, 'geoms') else [g]):
            dr.polygon([((x - self.x0) / RES - ox - a0, (y - self.y0) / RES - oy - b0) for x, y in poly.exterior.coords], fill=1, outline=1)
        m = np.array(im, dtype=np.uint8)
        if sign > 0: arr[b0:b1 + 1, a0:a1 + 1] += m
        else: arr[b0:b1 + 1, a0:a1 + 1] -= m

    def add(self, net, layers, shape, hole=False, trk=None, sign=1):
        if sign > 0: self.items.setdefault(net, []).append([layers, shape, hole, trk])
        x = HOLE_EXTRA if hole else 0.0
        for l in layers:
            if l != 'I': self.stamp(self.T[l], shape, CLR + W_TRK / 2 + EPS + x, sign)
            self.stamp(self.V[l], shape, CLR + VIA_D / 2 + EPS + x, sign)
            if trk is not None:
                if l != 'I': self.stamp(self.S[l], shape, CLR + W_TRK / 2 + EPS + x, sign)
                self.stamp(self.VS[l], shape, CLR + VIA_D / 2 + EPS + x, sign)

    def rippable(self, t):
        import proj
        n = t.GetNetname()
        return not (t.IsLocked() or n == 'GND' or n in self.protected or proj.classify(n) in ('DP100', 'DP90'))

    def keepouts(self, board):
        zs = list(board.Zones())
        for f in board.GetFootprints(): zs += list(f.Zones())
        for z in zs:
            if not z.GetIsRuleArea(): continue
            o = z.Outline()
            for k in range(o.OutlineCount()):
                pp = pts(o.Outline(k))
                if len(pp) < 3: continue
                g = Polygon(pp).buffer(0)
                for l in 'FB':
                    if not z.IsOnLayer(LAY[l]): continue
                    if z.GetDoNotAllowTracks(): self.stamp(self.T[l], g, W_TRK / 2 + EPS)
                    if z.GetDoNotAllowVias(): self.stamp(self.V[l], g, VIA_D / 2 + EPS)
                self.rule_areas.append((g, z.GetDoNotAllowVias()))

    def load(self, board):
        self.rule_areas = []; self.keepouts(board); self.padshapes = {}
        for f in board.GetFootprints():
            for p in f.Pads():
                net = p.GetNetname(); pos = p.GetPosition(); c = (pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y))
                layers = [l for l in 'FB' if p.IsOnLayer(LAY[l])]
                if layers:
                    ps = p.GetEffectivePolygon()
                    for k in range(ps.OutlineCount()):
                        pp = pts(ps.Outline(k))
                        if len(pp) >= 3:
                            g = Polygon(pp).buffer(0); self.add(net, 'FBI' if len(layers) == 2 else layers, g); self.padshapes.setdefault(net, []).append((layers, g))
                d = p.GetDrillSize()
                if d.x > 0:
                    r = max(pcbnew.ToMM(d.x), pcbnew.ToMM(d.y)) / 2
                    self.add(net if layers else '<npth:%s>' % f.GetReference(), 'FBI', Point(c).buffer(r, 12), hole=not layers)
        for t in board.GetTracks():
            net = t.GetNetname()
            soft = t if self.rippable(t) else None
            if t.GetClass() == 'PCB_VIA':
                pos = t.GetPosition(); self.add(net, 'FBI', Point(pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)).buffer(pcbnew.ToMM(t.GetWidth()) / 2, 12), trk=soft)
            else:
                a, b = t.GetStart(), t.GetEnd(); l = 'F' if t.GetLayer() == pcbnew.F_Cu else 'B' if t.GetLayer() == pcbnew.B_Cu else 'I' if t.GetLayer() == pcbnew.In2_Cu else None
                if l: self.add(net, l, LineString([(pcbnew.ToMM(a.x), pcbnew.ToMM(a.y)), (pcbnew.ToMM(b.x), pcbnew.ToMM(b.y))]).buffer(pcbnew.ToMM(t.GetWidth()) / 2, 8), trk=soft if l != 'I' else None)

    def route(self, net, a, la, b=None, lb='', via_goal=False, rip=False):
        """a, b: (x, y) in mm; la, lb: layer strings ('F', 'B' or 'FB').  Returns [(layer, [(x, y), ...])] and via list, or None."""
        if b is None: b = a
        ax, ay = self.cell(*a); bx, by = self.cell(*b); m = int((8.0 if via_goal else WIN) / RES)
        ox, oy = max(min(ax, bx) - m, 0), max(min(ay, by) - m, 0); ex, ey = min(max(ax, bx) + m, self.nx - 1), min(max(ay, by) + m, self.ny - 1)
        nx, ny = ex - ox + 1, ey - oy + 1
        ownT = {l: np.zeros((ny, nx), np.uint8) for l in 'FB'}; ownV = {l: np.zeros((ny, nx), np.uint8) for l in 'FBI'}
        ownS = {l: np.zeros((ny, nx), np.uint8) for l in 'FB'}; ownVS = {l: np.zeros((ny, nx), np.uint8) for l in 'FBI'}
        for layers, shape, hole, trk in self.items.get(net, []):
            x = HOLE_EXTRA if hole else 0.0
            for l in layers:
                if l != 'I': self.stamp(ownT[l], shape, CLR + W_TRK / 2 + EPS + x, win=(ox, oy))
                self.stamp(ownV[l], shape, CLR + VIA_D / 2 + EPS + x, win=(ox, oy))
                if trk is not None and rip:
                    if l != 'I': self.stamp(ownS[l], shape, CLR + W_TRK / 2 + EPS + x, win=(ox, oy))
                    self.stamp(ownVS[l], shape, CLR + VIA_D / 2 + EPS + x, win=(ox, oy))
        sl = (slice(oy, ey + 1), slice(ox, ex + 1))
        clean = {l: (self.T[l][sl] - ownT[l]) == 0 for l in 'FB'}                 # no other net's copper in the way
        vclean = clean['F'] & clean['B']
        for l in 'FBI': vclean = vclean & ((self.V[l][sl] - ownV[l]) == 0)
        if rip:   # rippable tracks of other nets may be crossed at a price; everything else stays an obstacle
            free = {l: ((self.T[l][sl] - self.S[l][sl]) - (ownT[l] - ownS[l])) == 0 for l in 'FB'}
            vfree = free['F'] & free['B']
            for l in 'FBI': vfree = vfree & (((self.V[l][sl] - self.VS[l][sl]) - (ownV[l] - ownVS[l])) == 0)
        else: free, vfree = clean, vclean
        sx, sy, gx, gy = ax - ox, ay - oy, bx - ox, by - oy
        for l in la: free[l][sy, sx] = True
        for l in lb: free[l][gy, gx] = True
        if via_goal:   # the via must stay off every pad of its own net too (no via-in-pad)
            padblk = np.zeros((ny, nx), np.uint8)
            for layers, shape in self.padshapes.get(net, []): self.stamp(padblk, shape, VIA_D / 2 + 0.12, win=(ox, oy))
            vfree = vfree & (padblk == 0)
            ground = np.zeros((ny, nx), np.uint8)   # existing vias / through-hole pins of this net: reaching one is as good as a new via
            for layers, shape, hole, trk in self.items.get(net, []):
                if len(layers) == 3 and not hole: self.stamp(ground, shape, -0.1, win=(ox, oy))
        # A*
        h = (lambda x, y: 0.0) if via_goal else (lambda x, y: 1.6 * math.hypot(x - gx, y - gy))   # weighted A*: long connections stay tractable
        start = [(l, sx, sy) for l in la]; best = {s: 0.0 for s in start}; prev = {}
        pq = [(h(sx, sy), 0.0, s) for s in start]; heapq.heapify(pq); goal = None; n = 0
        nb = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142))
        while pq:
            f_, g_, s = heapq.heappop(pq)
            if g_ > best.get(s, 1e18): continue
            l, x, y = s; n += 1
            if via_goal:
                if vfree[y, x] or ground[y, x]: goal = s; break
            elif x == gx and y == gy and l in lb: goal = s; break
            if n > int(os.environ.get('PATCH_NODES', 6000000)): break
            fl = free[l]
            for dx, dy, c in nb:
                u, v = x + dx, y + dy
                if u < 0 or v < 0 or u >= nx or v >= ny or not fl[v, u]: continue
                if dx and dy and not (fl[y, u] and fl[v, x]): continue
                t = (l, u, v); ng = g_ + c + (0.0 if clean[l][v, u] else RIP_COST)
                if ng < best.get(t, 1e18): best[t] = ng; prev[t] = s; heapq.heappush(pq, (ng + h(u, v), ng, t))
            if vfree[y, x] and not via_goal:
                t = ('B' if l == 'F' else 'F', x, y); ng = g_ + VIA_COST + (0.0 if vclean[y, x] else 12 * RIP_COST)
                if ng < best.get(t, 1e18): best[t] = ng; prev[t] = s; heapq.heappush(pq, (ng + h(x, y), ng, t))
        if goal is None: return None
        path = [goal]
        while path[-1] in prev: path.append(prev[path[-1]])
        path.reverse()
        runs = []; vias = []; cur = [path[0]]
        for p_, q_ in zip(path, path[1:]):
            if p_[0] != q_[0]: runs.append(cur); vias.append(self.xy(p_[1] + ox, p_[2] + oy)); cur = [q_]
            else: cur.append(q_)
        runs.append(cur); out = []
        if via_goal and not ground[goal[2], goal[1]]: vias.append(self.xy(goal[1] + ox, goal[2] + oy))
        for i, r in enumerate(runs):
            pl = [self.xy(x + ox, y + oy) for _, x, y in r]
            if i == 0: pl = [a] + pl
            if i == len(runs) - 1 and not via_goal: pl = pl + [b]
            if len(pl) < 2: continue
            simp = [pl[0]]
            for k in range(1, len(pl) - 1):
                (x0, y0), (x1, y1), (x2, y2) = simp[-1], pl[k], pl[k + 1]
                if abs((x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1)) > 1e-9: simp.append(pl[k])
            simp.append(pl[-1]); out.append((r[0][0], simp))
        return out, vias


def conflicts(g, net, runs, vias):
    """Rippable tracks/vias of other nets that are closer than the clearance to a new path."""
    geom = {'F': [], 'B': []}
    for l, pl in runs:
        if len(pl) >= 2: geom[l].append(LineString(pl).buffer(W_TRK / 2, 8))
    for (x, y) in vias:
        for l in 'FB': geom[l].append(Point(x, y).buffer(VIA_D / 2, 12))
    from shapely.ops import unary_union
    u = {l: unary_union(geom[l]) if geom[l] else None for l in 'FB'}; out = []
    for n, lst in g.items.items():
        if n == net: continue
        for it in lst:
            layers, shape, hole, trk = it
            if trk is None: continue
            if any(u.get(l) is not None and u[l].distance(shape) < CLR + EPS + 0.03 for l in layers): out.append((n, it))
    return out


def parse_unconnected(rpt):
    txt = open(rpt).read(); res = []
    for sec in re.split(r'\n\*\* ', txt):
        if not (sec.startswith('Found') and 'unconnected' in sec.split('\n')[0]): continue
        for m in re.finditer(r'\[unconnected_items\]: [^\n]*\n((?:    [^\n]*\n?)+)', sec):
            ends = []
            for line in m.group(1).split('\n'):
                q = re.search(r'@\(([-\d.]+) mm, ([-\d.]+) mm\): (.*)', line)
                if not q: continue
                d = q.group(3); net = re.search(r'\[([^\]]+)\]', d)
                lay = 'FB' if ('PTH' in d or d.startswith('Via')) else 'F' if 'on F.Cu' in d else 'B'
                ends.append(((float(q.group(1)), float(q.group(2))), lay, net.group(1) if net else None, d))
            if len(ends) == 2 and ends[0][2] and ends[0][2] != 'GND': res.append(ends)
    return res


def commit(board, g, net, ni, runs, vias, width=W_TRK):
    for l, pl in runs:
        for p_, q_ in zip(pl, pl[1:]):
            if math.hypot(p_[0] - q_[0], p_[1] - q_[1]) < 1e-6: continue
            t = pcbnew.PCB_TRACK(board); t.SetStart(pcbnew.VECTOR2I(mm(p_[0]), mm(p_[1]))); t.SetEnd(pcbnew.VECTOR2I(mm(q_[0]), mm(q_[1])))
            t.SetWidth(mm(width)); t.SetLayer(LAY[l]); t.SetNet(ni); board.Add(t)
            g.add(net, l, LineString([p_, q_]).buffer(width / 2, 8))
    for (x, y) in vias:
        v = pcbnew.PCB_VIA(board); v.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y))); v.SetWidth(mm(VIA_D)); v.SetDrill(mm(VIA_DR)); v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(ni); board.Add(v)
        g.add(net, 'FBI', Point(x, y).buffer(VIA_D / 2, 12))


def gnd_components(board, net='GND'):
    """Groups of GND copper on the outer layers that have no via or through-hole pin to the inner planes."""
    from shapely.strtree import STRtree
    nodes = []   # (geom, layers, grounded, start point or None, layer of start)
    for f in board.GetFootprints():
        for p in f.Pads():
            if p.GetNetname() != net: continue
            layers = [l for l in 'FB' if p.IsOnLayer(LAY[l])]
            if not layers: continue
            ps = p.GetEffectivePolygon(); pos = p.GetPosition()
            g = Polygon(pts(ps.Outline(0))).buffer(0); tht = p.GetDrillSize().x > 0
            nodes.append((g, layers, tht, (pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)), layers[0]))
    for t in board.GetTracks():
        if t.GetNetname() != net: continue
        if t.GetClass() == 'PCB_VIA':
            pos = t.GetPosition(); nodes.append((Point(pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)).buffer(pcbnew.ToMM(t.GetWidth()) / 2, 8), ['F', 'B'], True, None, None))
        else:
            l = 'F' if t.GetLayer() == pcbnew.F_Cu else 'B' if t.GetLayer() == pcbnew.B_Cu else None
            a, b = t.GetStart(), t.GetEnd()
            if l: nodes.append((LineString([(pcbnew.ToMM(a.x), pcbnew.ToMM(a.y)), (pcbnew.ToMM(b.x), pcbnew.ToMM(b.y))]).buffer(pcbnew.ToMM(t.GetWidth()) / 2, 4), [l], False, None, None))
    for z in board.Zones():
        if z.GetNetname() != net or z.GetIsRuleArea(): continue
        for l in 'FB':
            if not z.IsOnLayer(LAY[l]): continue
            fp = z.GetFilledPolysList(LAY[l])
            for k in range(fp.OutlineCount()):
                holes = [pts(fp.Hole(k, j)) for j in range(fp.HoleCount(k))]
                g = Polygon(pts(fp.Outline(k)), [h for h in holes if len(h) >= 3]).buffer(0)
                if not g.is_empty: nodes.append((g, [l], False, None, None))
    parent = list(range(len(nodes)))
    def find(i):
        while parent[i] != i: parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for l in 'FB':
        idx = [i for i, nd in enumerate(nodes) if l in nd[1]]; geoms = [nodes[i][0] for i in idx]; tree = STRtree(geoms)
        for k, g in enumerate(geoms):
            for j in tree.query(g):
                j = int(j)
                if j > k and geoms[j].intersects(g): parent[find(idx[k])] = find(idx[j])
    comp = {}
    for i in range(len(nodes)): comp.setdefault(find(i), []).append(i)
    out = []
    for members in comp.values():
        if any(nodes[i][2] for i in members): continue
        starts = [(nodes[i][3], nodes[i][4]) for i in members if nodes[i][3]]
        if starts: out.append(starts)
    return out


def gnd(src, dst):
    """Give every GND island on the outer layers a via to the inner planes, refill the pours, repeat until stable."""
    board = pcbnew.LoadBoard(src); ni = board.GetNetsByName()['GND']; total = 0
    for it in range(6):
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        comps = gnd_components(board)
        print('GND pass %d: %d group(s) without a path to the planes' % (it + 1, len(comps)))
        if not comps: break
        g = Grid(board); g.load(board); fail = 0
        for starts in comps:
            done = False
            for (xy, l) in starts[:6]:
                r = g.route('GND', xy, l, via_goal=True)
                if r: commit(board, g, 'GND', ni, r[0], r[1], 0.2 if False else W_TRK); total += 1; done = True; break
            if not done: fail += 1; print('  no via site for GND group at', starts[0])
        if fail and it >= 2: break
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(dst, board); print('GND vias added by the completion router:', total, '| groups left:', len(gnd_components(board)))


def one_round(src, dst, state):
    """One pass: route what is open.  A connection with no free path is routed across rippable tracks of other nets;
    those tracks are removed and their nets are routed again in the next pass.  A net routed that way is protected from then on."""
    import json
    st = json.load(open(state)) if os.path.exists(state) else {'protected': []}
    board = pcbnew.LoadBoard(src); nets = board.GetNetsByName(); rpt = '/tmp/patch_drc_%d.rpt' % os.getpid()
    pcbnew.WriteDRCReport(board, rpt, pcbnew.EDA_UNITS_MILLIMETRES, True)
    todo = parse_unconnected(rpt)
    if not todo:
        if src != dst: pcbnew.SaveBoard(dst, board)
        print('nothing open'); return
    prot = set(st['protected'])
    todo.sort(key=lambda e: (0 if e[0][2] in prot else 1, math.hypot(e[0][0][0] - e[1][0][0], e[0][0][1] - e[1][0][1])))
    g = Grid(board); g.protected = prot; g.load(board); ok = ripped_n = 0; fail = []; kill = {}; log = []
    for (a, la, net, da), (b, lb, _, db) in todo:
        r = g.route(net, a, la, b, lb) or g.route(net, b, lb, a, la)
        if r is None:
            r = None if os.environ.get('PATCH_NORIP') else g.route(net, a, la, b, lb, rip=True)   # TODO: rip-up does not converge yet; off for the release pass
            if r is None: fail.append(net); continue
            hit = conflicts(g, net, r[0], r[1])
            for n, it in hit:
                if id(it[3]) in kill: continue
                kill[id(it[3])] = it[3]; g.items[n].remove(it); g.add(n, it[0], it[1], it[2], trk=it[3], sign=-1)
            ripped_n += 1; st['protected'].append(net); g.protected.add(net)
            for it in g.items.get(net, []):
                if it[3] is not None and id(it[3]) not in kill:
                    for l in it[0]:
                        x_ = HOLE_EXTRA if it[2] else 0.0
                        if l != 'I': g.stamp(g.S[l], it[1], CLR + W_TRK / 2 + EPS + x_, -1)
                        g.stamp(g.VS[l], it[1], CLR + VIA_D / 2 + EPS + x_, -1)
                    it[3] = None
            pass; log.append('%s (over %s)' % (net, ', '.join(sorted({n for n, _ in hit})) or '-'))
        else: ok += 1
        commit(board, g, net, nets[net], r[0], r[1])
    print('%d open: %d routed, %d routed with rip-up, %d failed' % (len(todo), ok, ripped_n, len(fail)))
    for l in log: print('  rip-up:', l)
    for n in fail: print('  FAILED:', n)
    for t in kill.values(): board.Remove(t)
    pcbnew.SaveBoard(dst, board); json.dump(st, open(state, 'w'))


def main(src, dst, rounds=10):
    """Each pass runs in its own process: the KiCad 7 Python API is not reliable after items have been removed from a board."""
    state = dst + '.state.json'
    if os.path.exists(state): os.remove(state)
    cur = src
    for rnd in range(int(rounds)):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), 'round', cur, dst, state], capture_output=True, text=True)
        out = [l for l in r.stdout.splitlines() if l.strip()]
        print('pass %d:' % (rnd + 1), '\n'.join(out) if out else 'FAILED: ' + r.stderr[-300:]); sys.stdout.flush(); cur = dst
        if out and out[0].startswith('nothing open'): break
    subprocess.run([sys.executable, os.path.abspath(__file__), 'left', dst])


def left(src):
    board = pcbnew.LoadBoard(src); rpt = '/tmp/patch_drc_%d.rpt' % os.getpid()
    pcbnew.WriteDRCReport(board, rpt, pcbnew.EDA_UNITS_MILLIMETRES, True); todo = parse_unconnected(rpt)
    print('open signal/power connections left:', len(todo))
    for e in todo: print('  OPEN', e[0][2], '|', e[0][3][:50], '|', e[1][3][:50])


if __name__ == '__main__':
    if sys.argv[1] == 'gnd': gnd(*sys.argv[2:])
    elif sys.argv[1] == 'round': one_round(*sys.argv[2:])
    elif sys.argv[1] == 'left': left(sys.argv[2])
    else: main(*sys.argv[1:])

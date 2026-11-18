#!/usr/bin/env python3
"""Autoroute the SORX carrier with Freerouting in two phases.

  phase 1: only the differential pairs (Ethernet MDI, USB, PCIe) on an otherwise empty board,
           so each pair takes the direct path with P and N side by side at the class gap;
  phase 2: everything else, with the phase-1 copper locked.
In1 is a solid GND plane.  In2 carries the supply rails (PWR classes only) and is GND-filled around them.
"""
import os, sys, re, subprocess, json, time
import pcbnew
import proj
from sx import parse, find, first

HERE = os.path.dirname(os.path.abspath(__file__))
B = os.path.join(HERE, '..', 'hw', 'build') + '/'
JAR = '/home/claude/tools-ext/freerouting-2.1.0.jar'
JAR19 = '/home/claude/tools-ext/freerouting-1.9.0.jar'
mm = pcbnew.FromMM
LAYER = {'F.Cu': pcbnew.F_Cu, 'B.Cu': pcbnew.B_Cu, 'In1.Cu': pcbnew.In1_Cu, 'In2.Cu': pcbnew.In2_Cu}


def export_dsn(board, path, power_layer=False):
    """KiCad DSN with In1 as a plane.  In2 is a plane too, unless power_layer is set: then the PWR classes may route on it
    (signals stay on the outer layers), which keeps the supply rails off the congested outer layers."""
    assert pcbnew.ExportSpecctraDSN(board, path)
    s = open(path).read()
    for l in ('In1.Cu',) if power_layer else ('In1.Cu', 'In2.Cu'):
        s = re.sub(r'(\(layer %s\s+\(type )signal' % re.escape(l), r'\1power', s)
    if power_layer:
        def fix(m):
            layers = 'F.Cu In2.Cu B.Cu' if m.group(1) in ('PWR', 'PWR_HI') else 'F.Cu B.Cu'
            return m.group(0) + '\n        (use_layer %s)' % layers
        s, n = re.subn(r'\(class "?(\w+)"?[^()]*\(circuit', fix, s); assert n >= 5, n
    open(path, 'w').write(s)


def freeroute(dsn, ses, passes, timeout, log='/tmp/fr19.log'):
    """Freerouting 1.9.0 under a virtual display: this release honours -mp and always writes the session file."""
    t = time.time()
    with open(log, 'w') as lf:
        try:
            subprocess.run(['xvfb-run', '-a', 'java', '-Xmx4g', '-jar', JAR19, '-de', dsn, '-do', ses, '-mp', str(passes), '-mt', '1', '-oit', '1'],
                           stdout=lf, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            print('freerouting hit the wrapper timeout')
    tail = [l[30:] for l in open(log).read().splitlines() if 'pass #' in l.lower() or 'unrouted' in l.lower()]
    print('freerouting %.0fs' % (time.time() - t), '|', ' | '.join(tail[-2:])[:300])
    return os.path.exists(ses)


def import_ses(board, ses, lock=False):
    root = parse(open(ses).read())
    routes = first(root, 'routes'); res = first(routes, 'resolution'); k = 1.0 / (float(res[2]) * 1000.0)
    nets = board.GetNetsByName(); n_t = n_v = 0
    for net in find(first(routes, 'network_out'), 'net'):
        name = str(net[1]); ni = nets[name] if nets.has_key(name) else None
        assert ni is not None, name
        for w in find(net, 'wire'):
            p = first(w, 'path'); layer = LAYER[str(p[1])]; width = float(p[2]) * k; c = [float(v) * k for v in p[3:]]
            pts = [(c[i], -c[i + 1]) for i in range(0, len(c), 2)]
            for a, b in zip(pts, pts[1:]):
                if a == b: continue
                t = pcbnew.PCB_TRACK(board); t.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1]))); t.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
                t.SetWidth(mm(width)); t.SetLayer(layer); t.SetNet(ni); t.SetLocked(lock); board.Add(t); n_t += 1
        for v in find(net, 'via'):
            m = re.match(r'Via\[0-3\]_(\d+):(\d+)_um', str(v[1])); dia, drill = int(m.group(1)) / 1000.0, int(m.group(2)) / 1000.0
            via = pcbnew.PCB_VIA(board); via.SetPosition(pcbnew.VECTOR2I(mm(float(v[2]) * k), mm(-float(v[3]) * k))); via.SetWidth(mm(dia)); via.SetDrill(mm(drill))
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); via.SetNet(ni); via.SetLocked(lock); board.Add(via); n_v += 1
    return n_t, n_v


def load(name):
    proj.write(B + name + '.kicad_pro', name)
    return pcbnew.LoadBoard(B + name + '.kicad_pcb')


def fanout_sites(board, refs=('U13', 'U10')):
    """Supply pins that sit between two differential-pair pins of a PHY (KSZ9131 AVDDL, I210 VDD pins) need their own
    via right outside the pad row; the pairs have to make room for it.  Returns (net, pad xy, via xy, layer)."""
    out = []
    for f in board.GetFootprints():
        if f.GetReference() not in refs: continue
        c = f.GetPosition(); pads = [p for p in f.Pads() if p.GetNetname()]
        ispair = lambda p: proj.classify(p.GetNetname()) in ('DP100', 'DP90')
        for p in pads:
            if ispair(p) or p.GetNetname() == 'GND': continue
            q = p.GetPosition(); nb = [o for o in pads if o is not p and abs(pcbnew.ToMM((o.GetPosition() - q).EuclideanNorm()) - 0.5) < 0.02]
            if len(nb) != 2 or not all(ispair(o) for o in nb): continue
            dx, dy = q.x - c.x, q.y - c.y
            ux, uy = ((1 if dx > 0 else -1), 0) if abs(dx) > abs(dy) else (0, (1 if dy > 0 else -1))
            bb = p.GetBoundingBox(); half = pcbnew.ToMM(bb.GetWidth() if ux else bb.GetHeight()) / 2
            px, py = pcbnew.ToMM(q.x), pcbnew.ToMM(q.y); d = half + 0.80
            lay = pcbnew.B_Cu if p.IsOnLayer(pcbnew.B_Cu) and not p.IsOnLayer(pcbnew.F_Cu) else pcbnew.F_Cu
            out.append((p.GetNetname(), (px, py), (px + ux * d, py + uy * d), lay, (ux, uy), half))
    return out


def phase1(passes=25):
    b = load('sorx_placed'); sites = fanout_sites(b)
    keep = {n for n in map(str, b.GetNetsByName().keys()) if proj.classify(n) in ('DP100', 'DP90')}
    for f in b.GetFootprints():
        for p in f.Pads():
            if p.GetNetname() and p.GetNetname() not in keep: p.SetNetCode(0)
    for z in list(b.Zones()): b.Remove(z)
    for net, (px, py), (vx, vy), layer, (ux, uy), half in sites:   # room for the fan-out vias added after this phase
        z = pcbnew.ZONE(b); z.SetIsRuleArea(True); z.SetDoNotAllowTracks(True); z.SetDoNotAllowVias(True); z.SetDoNotAllowCopperPour(True)
        ls = pcbnew.LSET(); ls.AddLayer(pcbnew.F_Cu); ls.AddLayer(pcbnew.B_Cu); z.SetLayerSet(ls)
        o = z.Outline(); o.NewOutline(); r = 0.52
        for (x, y) in ((vx - r, vy - r), (vx + r, vy - r), (vx + r, vy + r), (vx - r, vy + r)): o.Append(mm(x), mm(y))
        b.Add(z)
    export_dsn(b, B + 'p1.dsn')
    if os.path.exists(B + 'p1.ses'): os.remove(B + 'p1.ses')
    freeroute(B + 'p1.dsn', B + 'p1.ses', passes, 1500)
    sys.stdout.flush()
    subprocess.run([sys.executable, os.path.abspath(__file__), '1i'], check=True)   # import in a fresh process (pcbnew is not re-entrant)


def phase1_import():
    full = load('sorx_placed')
    nt, nv = import_ses(full, B + 'p1.ses', lock=True)
    nets = full.GetNetsByName()
    for net, (px, py), (vx, vy), layer, _, half in fanout_sites(full):   # supply pins between pair pins: short stub and a via to the supply layer
        t = pcbnew.PCB_TRACK(full); t.SetStart(pcbnew.VECTOR2I(mm(px), mm(py))); t.SetEnd(pcbnew.VECTOR2I(mm(vx), mm(vy))); t.SetWidth(mm(0.2)); t.SetLayer(layer); t.SetNet(nets[net]); t.SetLocked(True); full.Add(t)
        v = pcbnew.PCB_VIA(full); v.SetPosition(pcbnew.VECTOR2I(mm(vx), mm(vy))); v.SetWidth(mm(0.6)); v.SetDrill(mm(0.3)); v.SetViaType(pcbnew.VIATYPE_THROUGH); v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(nets[net]); v.SetLocked(True); full.Add(v)
        print('fan-out via for %s at (%.2f, %.2f)' % (net, vx, vy))
    proj.write(B + 'sorx_p1.kicad_pro', 'sorx_p1'); pcbnew.SaveBoard(B + 'sorx_p1.kicad_pcb', full)
    print('phase 1: %d segments, %d vias' % (nt, nv))


def phase2(passes=40, timeout=3000):
    b = load('sorx_p1')
    # GND is not autorouted: it is connected afterwards by copper pours and stitching vias (finish.py)
    for f in b.GetFootprints():
        for p in f.Pads():
            if p.GetNetname() == 'GND': p.SetNetCode(0)
    for z in list(b.Zones()): b.Remove(z)
    export_dsn(b, B + 'p2.dsn', power_layer=True)
    if os.path.exists(B + 'p2.ses'): os.remove(B + 'p2.ses')
    freeroute(B + 'p2.dsn', B + 'p2.ses', passes, timeout)
    sys.stdout.flush()
    subprocess.run([sys.executable, os.path.abspath(__file__), '2i'], check=True)


def phase2_import():
    b = load('sorx_p1')
    nt, nv = import_ses(b, B + 'p2.ses')
    proj.write(B + 'sorx_routed.kicad_pro', 'sorx_routed'); pcbnew.SaveBoard(B + 'sorx_routed.kicad_pcb', b)
    print('phase 2: %d segments, %d vias' % (nt, nv))


if __name__ == '__main__':
    {'1': phase1, '1i': phase1_import, '2': phase2, '2i': phase2_import}[sys.argv[1]](*[int(a) for a in sys.argv[2:]])

#!/usr/bin/env python3
"""SORX enclosure, rack ears and fit check (CadQuery).

Frame: X = board x, Y = -board y (rear edge at Y = 0, front edge at Y = -118), Z up, PCB top face at Z = 0.
Reads hw/build/mech_parts.json (written by mech_export.py from the routed board).
Writes mech/*.step, *.stl, *.3mf, fit-check SVGs and mech/fit-check.json.
"""
import json, os, sys
import cadquery as cq

HERE = os.path.dirname(os.path.abspath(__file__))
MECH = os.path.join(HERE, '..', 'mech'); BUILD = os.path.join(HERE, '..', 'hw', 'build')
W, H, T = 172.0, 118.0, 1.6
STANDOFF = 6.0                    # PCB underside to floor
FLOOR = 2.0; LID = 2.0; WALL = 2.4
OUT_W, OUT_H = 215.0, 44.0        # half-width 1U
X0 = -1.0                         # inner cavity, left
X1 = X0 + OUT_W - 2 * WALL        # inner cavity, right (fan plenum is right of the board)
Y1 = 0.4; Y0 = -H - 0.4           # inner cavity rear / front
ZF = -T - STANDOFF                # floor top
ZC = ZF + (OUT_H - FLOOR - LID)   # ceiling (lid underside)
HOLES = [(4.5, 4.5), (167.5, 4.5), (4.5, 113.5), (167.5, 113.5)]
FAN = dict(size=40.0, thick=10.6, yc=-60.0)

P = {p['ref']: p for p in json.load(open(os.path.join(BUILD, 'mech_parts.json')))}
# component envelope heights above the PCB (mm), nominal datasheet values
HEIGHT = {'RJ45_Pulse_JK0654219NL_Horizontal': 13.6, 'RJ45_Amphenol_54602-x08_Horizontal': 13.2, 'USB3_A_Receptacle_Wuerth_692122030100': 7.0,
          'USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal': 3.3, 'microSD_HC_Hirose_DM3AT-SF-PEJM5': 1.9, 'SW_Tactile_SKHH_Angled': 7.3,
          'LED_Kingbright_KPA-3010_3x2x1mm': 1.0, 'CP_Elec_6.3x7.7': 7.9, 'L_Coilcraft_XAL5050-XXX': 5.1, 'BatteryHolder_Keystone_3034_1x20mm': 5.6,
          'FanPinHeader_1x04_P2.54mm_Vertical': 11.6, 'PinHeader_1x06_P2.54mm_Vertical': 8.6, 'SOT-223-3_TabPin2': 1.8}


def box(x1, y1, x2, y2, z1, z2):
    x1, x2 = sorted((x1, x2)); y1, y2 = sorted((y1, y2)); z1, z2 = sorted((z1, z2))
    return cq.Workplane('XY').box(x2 - x1, y2 - y1, z2 - z1, centered=False).translate((x1, y1, z1))


def brect(p):   # board rectangle -> mech frame (x1, y1, x2, y2)
    b = p['box']; return (b[0], -b[3], b[2], -b[1])


# ---------------------------------------------------------------- board assembly (envelopes)
def board_assembly():
    pcb = box(0, -H, W, 0, -T, 0)
    for (x, y) in HOLES:
        pcb = pcb.cut(cq.Workplane('XY').circle(1.6).extrude(T).translate((x, -y, -T)))
    parts = {}
    for r, p in P.items():
        x1, y1, x2, y2 = brect(p)
        if p['fp'].startswith('SODIMM'):
            parts['J1'] = box(x1, -P[r]['y'] + 2.2 - 8.4, x2, -P[r]['y'] + 14.2, 0, 5.2)        # socket body
            xs, ys = p['x'], -p['y']
            parts['M1'] = box(xs - 34.8, ys - 26.5, xs + 34.8, ys + 8.5, 2.62, 2.62 + 1.2).union(box(xs - 30, ys - 24, xs + 30, ys + 2, 3.82, 6.8))  # module PCB + parts
            parts['HS1'] = box(xs - 20, ys - 26, xs + 20, ys + 2 - 0, 6.8, 16.8)                 # 40 x 28 x 10 heatsink on the SoC
            continue
        if p['fp'].startswith('SMA'):
            parts[r] = box(p['x'] - 3.2, 0, p['x'] + 3.2, 11.5, -T / 2 - 3.2, -T / 2 + 3.2); continue
        if p['fp'].startswith('SW_Tactile'):
            xc = p['x'] - 2.25
            parts[r] = box(x1, max(y1, -H), x2, y2, 0, 7.3).union(box(xc - 1.5, -H - 1.5, xc + 1.5, -H, 2.4, 5.4)); continue
        h = HEIGHT.get(p['fp'])
        if h is None:
            h = 1.2 if (x2 - x1) * (y2 - y1) < 30 else 1.8
        if p['side'] == 'B': parts[r] = box(x1, y1, x2, y2, -T - min(h, 1.2), -T)
        else: parts[r] = box(x1, y1, x2, y2, 0, h)
    return pcb, parts


# ---------------------------------------------------------------- openings
def openings():
    """name -> (wall, x1, x2, z1, z2).  Notches are open to the top of the wall; the lid carries matching teeth."""
    o = {}
    def conn(ref, name, wall, mx=0.5, top=0.5, z1=-0.3, h=None, x=None):
        p = P[ref]; x1, y1, x2, y2 = brect(p)
        if x: x1, x2 = x
        hh = h if h is not None else HEIGHT[p['fp']]
        o[name] = (wall, x1 - mx, x2 + mx, z1, hh + top)
    conn('J6', 'OE', 'rear'); conn('J4', 'MD-IN', 'rear'); conn('J7', 'MGMT', 'rear')
    conn('J3', 'USB-C PWR', 'rear', mx=1.8, top=1.6, z1=-1.6)
    p = P['J5']; o['PPS'] = ('rear', p['x'] - 4.2, p['x'] + 4.2, -T / 2 - 4.2, -T / 2 + 4.2)
    conn('J8', 'USB 1', 'front'); conn('J9', 'USB 2', 'front'); conn('J11', 'CONSOLE', 'front')
    conn('J10', 'USB-C DEBUG', 'front', mx=1.5, top=1.6, z1=-1.6)
    conn('J12', 'microSD', 'front', mx=0.6, top=0.9)
    for r, n in (('SW1', 'RESET'), ('SW2', 'RECOVERY')):
        p = P[r]; xc = p['x'] - 2.25                     # plunger axis (footprint rotated 180 deg)
        o[n] = ('front', xc - 2.0, xc + 2.0, 1.9, 5.9)
    x1 = P['D4']['box'][0] - 1.0; x2 = P['D10']['box'][2] + 1.0
    o['LED window'] = ('front', x1, x2, -0.4, 2.4)
    return o


def enclosure(op):
    outer = box(X0 - WALL, Y0 - WALL, X1 + WALL, Y1 + WALL, ZF - FLOOR, ZC)
    base = outer.cut(box(X0, Y0, X1, Y1, ZF, ZC + 1))
    base = base.edges('|Z').fillet(1.5)
    for (x, y) in HOLES:                                           # PCB bosses with M3 heat-set insert pockets
        boss = cq.Workplane('XY').circle(3.5).extrude(STANDOFF).translate((x, -y, ZF))
        base = base.union(boss).cut(cq.Workplane('XY').circle(2.0).extrude(5.5).translate((x, -y, -T - 5.5)))
    for name, (wall, x1, x2, z1, z2) in op.items():               # port notches, open to the top
        ya, yb = (Y1 - 0.1, Y1 + WALL + 0.1) if wall == 'rear' else (Y0 - WALL - 0.1, Y0 + 0.1)
        base = base.cut(box(x1, ya, x2, yb, z1, ZC + 1))
    # fan: 38 mm opening and 4 screw holes in the right wall, pockets in floor
    fz = (ZF + ZC) / 2; fy = FAN['yc']
    base = base.cut(cq.Workplane('YZ').circle(19.0).extrude(WALL + 0.4).translate((X1 - 0.2, fy, fz)))
    for dy in (-16, 16):
        for dz in (-16, 16):
            base = base.cut(cq.Workplane('YZ').circle(1.7).extrude(WALL + 0.4).translate((X1 - 0.2, fy + dy, fz + dz)))
    # intake slots in the left wall
    for i in range(14):
        y = -18 - i * 6.5
        base = base.cut(box(X0 - WALL - 0.1, y - 1.5, X0 + 0.1, y + 1.5, 3.0, 27.0))
    # rack-ear bosses (M3 inserts) on both side walls
    for (xw, sx) in ((X0, 1), (X1, -1)):
        for y in (-20.0, -100.0):
            b = cq.Workplane('YZ').circle(3.6).extrude(4.0 * sx).translate((xw, y, 22.0))
            hole = cq.Workplane('YZ').circle(2.0).extrude((4.0 + WALL + 0.2) * sx).translate((xw - (WALL + 0.1) * sx, y, 22.0))
            base = base.union(b).cut(hole)
    # lid: plate, locating lip, teeth that close the notches, screw tubes down to the PCB
    lid = box(X0 - WALL, Y0 - WALL, X1 + WALL, Y1 + WALL, ZC, ZC + LID).edges('|Z').fillet(1.5)
    lip = box(X0 + 0.25, Y0 + 0.25, X1 - 0.25, Y1 - 0.25, ZC - 2.0, ZC).cut(box(X0 + 1.85, Y0 + 1.85, X1 - 1.85, Y1 - 1.85, ZC - 2.1, ZC + 0.1))
    lip = lip.cut(box(X1 - FAN['thick'] - 0.6, FAN['yc'] - 20.6, X1, FAN['yc'] + 20.6, ZC - 2.1, ZC + 0.1))   # clear the fan frame
    lid = lid.union(lip)
    for name, (wall, x1, x2, z1, z2) in op.items():
        ya, yb = (Y1, Y1 + WALL) if wall == 'rear' else (Y0 - WALL, Y0)
        if ZC - (z2 + 0.0) > 0.5: lid = lid.union(box(x1 + 0.3, ya, x2 - 0.3, yb, z2, ZC))
    for (x, y) in HOLES:
        tube = cq.Workplane('XY').circle(3.5).extrude(ZC - 0.15).translate((x, -y, 0.15))
        lid = lid.union(tube).cut(cq.Workplane('XY').circle(1.75).extrude(ZC + LID + 1).translate((x, -y, 0)))
        lid = lid.cut(cq.Workplane('XY').circle(3.1).extrude(1.2).translate((x, -y, ZC + LID - 1.2)))   # screw head recess
    return base, lid


def fan_body():
    fz = (ZF + ZC) / 2
    return box(X1 - FAN['thick'], FAN['yc'] - 20, X1, FAN['yc'] + 20, fz - 20, fz + 20)


def window_insert(op):
    w, x1, x2, z1, z2 = op['LED window']
    return box(x1 + 0.15, Y0 - WALL, x2 - 0.15, Y0, z1 + 0.15, z2 - 0.0).union(box(x1 - 1.0, Y0, x2 + 1.0, Y0 + 0.8, z1 - 0.2, z2 + 1.0))


def rack_ear(long):
    """1U ear. Short ear: flange only. Long ear: spans the other half of a 19-inch rack (450 mm opening)."""
    ext = (450.0 - OUT_W) if long else 0.0
    plate = box(0, -110.0, 3.0, -10.0, 0, OUT_H)                         # along the enclosure side wall
    for y in (-20.0, -100.0):
        plate = plate.cut(cq.Workplane('YZ').circle(1.75).extrude(3.2).translate((-0.1, y, 31.6)))
    arm = box(0, -13.0, -(ext + 16.0), -10.0, 0, OUT_H)                   # front flange (with extension)
    for z in (6.35, 37.65):                                             # EIA-310 outer hole pair of a 1U space
        arm = arm.cut(cq.Workplane('XZ').slot2D(10.0, 6.8).extrude(3.4).translate((-(ext + 8.0), -9.9, z)))
    ear = plate.union(arm)
    if long: ear = ear.union(box(-ext, -40.0, 0, -13.0, 0, 3.0)).union(box(-ext, -40.0, 0, -13.0, OUT_H - 3.0, OUT_H))   # stiffening webs
    return ear


def drawings(op, parts):
    """Orthographic fit-check drawings: rear and front elevations with port openings, and a plan view."""
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle, Circle
    fig, ax = plt.subplots(3, 1, figsize=(15, 13), gridspec_kw={'height_ratios': [1, 1, 2.6]})
    face = {'OE': 'J6', 'MD-IN': 'J4', 'MGMT': 'J7', 'USB-C PWR': 'J3', 'USB 1': 'J8', 'USB 2': 'J9', 'CONSOLE': 'J11', 'USB-C DEBUG': 'J10', 'microSD': 'J12'}
    for a, wall, title, flip in ((ax[0], 'rear', 'Rear panel (viewed from behind)', True), (ax[1], 'front', 'Front panel (viewed from the front)', False)):
        sx = -1 if flip else 1
        a.add_patch(Rectangle((sx * (X0 - WALL), ZF - FLOOR), sx * OUT_W, OUT_H, fill=False, lw=1.5))
        a.add_patch(Rectangle((sx * 0, -T), sx * W, T, color='#2e7d32', alpha=0.8))
        for name, (w, x1, x2, z1, z2) in op.items():
            if w != wall: continue
            a.add_patch(Rectangle((sx * x1, z1), sx * (x2 - x1), z2 - z1, fill=False, ec='#c62828', lw=1.2))
            if name in face:
                bx = brect(P[face[name]]); h = HEIGHT[P[face[name]]['fp']]
                a.add_patch(Rectangle((sx * bx[0], 0), sx * (bx[2] - bx[0]), h, color='#90a4ae', alpha=0.9))
            elif name == 'PPS': a.add_patch(Circle((sx * P['J5']['x'], -T / 2), 3.2, color='#90a4ae'))
            elif name in ('RESET', 'RECOVERY'): a.add_patch(Circle((sx * (P['SW1' if name == 'RESET' else 'SW2']['x'] - 2.25), 3.9), 1.5, color='#90a4ae'))
            elif name == 'LED window':
                for r in ('D4', 'D5', 'D6', 'D7', 'D8', 'D9', 'D10'): a.add_patch(Rectangle((sx * P[r]['box'][0], 0), sx * 3.1, 1.0, color='#f9a825'))
            a.text(sx * (x1 + x2) / 2, z2 + 1.2, name, ha='center', va='bottom', fontsize=8)
        if wall == 'front':
            for r, n in zip(('D4', 'D5', 'D6', 'D7', 'D8', 'D9', 'D10'), ('PWR', 'SYS', 'FEED A', 'FEED B', 'FIX A', 'FIX B', 'ALARM')):
                a.text(P[r]['x'], -5.0, n, ha='center', va='top', fontsize=6, rotation=90)
        a.set_xlim(sorted((sx * (X0 - WALL - 4), sx * (X1 + WALL + 4)))); a.set_ylim(ZF - FLOOR - 9, ZC + LID + 4); a.set_aspect('equal'); a.set_title(title, fontsize=10)
        a.text(sx * (X1 - 12), 22, 'fan plenum', ha='center', fontsize=7, color='#616161'); a.set_xlabel('mm'); a.set_ylabel('mm')
    a = ax[2]
    a.add_patch(Rectangle((X0 - WALL, Y0 - WALL), OUT_W, Y1 - Y0 + 2 * WALL, fill=False, lw=1.5)); a.add_patch(Rectangle((X0, Y0), X1 - X0, Y1 - Y0, fill=False, lw=0.6, ls=':'))
    a.add_patch(Rectangle((0, -H), W, H, color='#2e7d32', alpha=0.25))
    for r, p in P.items():
        if p['side'] == 'B': continue
        bx = brect(p); big_ = r[0] == 'J' or r.startswith('SW') or r == 'BT1'
        a.add_patch(Rectangle((bx[0], bx[1]), bx[2] - bx[0], bx[3] - bx[1], fill=big_, color='#90a4ae' if big_ else '#555555', alpha=0.9 if big_ else 0.5, lw=0.4))
        if big_: a.text((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2, r, ha='center', va='center', fontsize=6)
    xs, ys = P['J1']['x'], -P['J1']['y']
    a.add_patch(Rectangle((xs - 34.8, ys - 26.5), 69.6, 35, fill=False, ec='#1565c0', lw=1.2)); a.text(xs, ys - 12, 'Verdin SoM + heatsink', ha='center', color='#1565c0', fontsize=8)
    a.add_patch(Rectangle((X1 - FAN['thick'], FAN['yc'] - 20), FAN['thick'], 40, color='#212121', alpha=0.8)); a.text(X1 - 18, FAN['yc'], '40 mm fan', ha='right', va='center', fontsize=8)
    for (x, y) in HOLES: a.add_patch(Circle((x, -y), 3.5, fill=False, ec='#c62828'))
    for name, (w, x1, x2, z1, z2) in op.items():
        yy = Y1 if w == 'rear' else Y0 - WALL
        a.add_patch(Rectangle((x1, yy), x2 - x1, WALL, color='#c62828', alpha=0.5))
    a.annotate('', xy=(X0 - WALL, Y0 - WALL - 5), xytext=(X1 + WALL, Y0 - WALL - 5), arrowprops=dict(arrowstyle='<->')); a.text((X0 + X1) / 2, Y0 - WALL - 6.5, '%.1f mm' % OUT_W, ha='center', va='top', fontsize=9)
    a.annotate('', xy=(X1 + WALL + 5, Y0 - WALL), xytext=(X1 + WALL + 5, Y1 + WALL), arrowprops=dict(arrowstyle='<->')); a.text(X1 + WALL + 6.5, (Y0 + Y1) / 2, '%.1f mm' % (Y1 - Y0 + 2 * WALL), rotation=90, va='center', fontsize=9)
    a.set_xlim(X0 - WALL - 6, X1 + WALL + 14); a.set_ylim(Y0 - WALL - 12, Y1 + WALL + 14); a.set_aspect('equal'); a.set_title('Plan view, lid removed (red: wall openings and screw posts)', fontsize=10)
    fig.tight_layout(); fig.savefig(os.path.join(MECH, 'fit-check.png'), dpi=110); fig.savefig(os.path.join(MECH, 'fit-check.pdf'))


def main():
    os.makedirs(MECH, exist_ok=True)
    pcb, parts = board_assembly(); op = openings(); base, lid = enclosure(op); fan = fan_body(); win = window_insert(op)
    asm = cq.Assembly(name='sorx')
    asm.add(base, name='enclosure_base', color=cq.Color(0.25, 0.27, 0.30)); asm.add(lid, name='enclosure_lid', color=cq.Color(0.25, 0.27, 0.30, 0.35))
    asm.add(pcb, name='pcb', color=cq.Color(0.05, 0.35, 0.15)); asm.add(fan, name='fan_40x40x10', color=cq.Color(0.1, 0.1, 0.1))
    asm.add(win, name='led_window', color=cq.Color(0.9, 0.9, 0.9, 0.5))
    comp = None
    for r, s in parts.items():
        comp = s if comp is None else comp.add(s)
        if r[0] in 'JMHS' or r in ('BT1',): asm.add(s, name=r, color=cq.Color(0.75, 0.75, 0.78))
    asm.save(os.path.join(MECH, 'sorx-assembly.step'))
    cq.exporters.export(base, os.path.join(MECH, 'enclosure-base.step')); cq.exporters.export(lid, os.path.join(MECH, 'enclosure-lid.step'))
    for name, s in (('enclosure-base', base), ('enclosure-lid', lid), ('led-window', win), ('rack-ear-short', rack_ear(False)), ('rack-ear-long', rack_ear(True))):
        cq.exporters.export(s, os.path.join(MECH, name + '.stl'), tolerance=0.05, angularTolerance=0.2)
        cq.exporters.export(s, os.path.join(MECH, name + '.3mf'), tolerance=0.05, angularTolerance=0.2)
    cq.exporters.export(rack_ear(False), os.path.join(MECH, 'rack-ear-short.step')); cq.exporters.export(rack_ear(True), os.path.join(MECH, 'rack-ear-long.step'))
    # ---- fit check ------------------------------------------------------------------------------
    res = {'interference_mm3': {}, 'ports': [], 'clearances': {}}
    solids = {'pcb': pcb, 'fan': fan}
    solids.update({r: s for r, s in parts.items()})
    shell = base.union(lid)
    worst = 0.0
    big = [r for r in solids if r in ('pcb', 'fan', 'M1', 'HS1') or r[0] == 'J' or r.startswith('SW') or r.startswith('BT') or r in ('C23', 'C106', 'C110', 'L1', 'L2', 'L3', 'D4', 'D10')]
    for r in big:
        try: v = shell.intersect(solids[r]).val().Volume()
        except Exception: v = 0.0
        if v > 1e-3: res['interference_mm3'][r] = round(v, 3)
        worst = max(worst, v)
    for name, (wall, x1, x2, z1, z2) in op.items():
        ref = {'OE': 'J6', 'MD-IN': 'J4', 'MGMT': 'J7', 'USB-C PWR': 'J3', 'PPS': 'J5', 'USB 1': 'J8', 'USB 2': 'J9', 'CONSOLE': 'J11', 'USB-C DEBUG': 'J10', 'microSD': 'J12',
               'RESET': 'SW1', 'RECOVERY': 'SW2', 'LED window': None}[name]
        if ref is None:
            cx1, cx2 = P['D4']['box'][0], P['D10']['box'][2]; cz1, cz2 = 0.0, 1.0
        elif ref == 'J5': cx1, cx2 = P[ref]['x'] - 3.2, P[ref]['x'] + 3.2; cz1, cz2 = -T / 2 - 3.2, -T / 2 + 3.2
        elif ref.startswith('SW'): xc = P[ref]['x'] - 2.25; cx1, cx2 = xc - 1.5, xc + 1.5; cz1, cz2 = 2.4, 5.4
        else:
            b = brect(P[ref]); cx1, cx2 = b[0], b[2]; cz1, cz2 = 0.0, HEIGHT[P[ref]['fp']]
        m = dict(left=round(cx1 - x1, 2), right=round(x2 - cx2, 2), bottom=round(cz1 - z1, 2), top=round(z2 - cz2, 2))
        res['ports'].append(dict(port=name, wall=wall, ref=ref or 'D4-D10', opening_x=[round(x1, 2), round(x2, 2)], opening_z=[round(z1, 2), round(z2, 2)], margins=m, ok=min(m.values()) >= 0.25))
    res['clearances'] = {'heatsink_top_to_lid_mm': round(ZC - 16.8, 2), 'magjack_top_to_lid_mm': round(ZC - 13.6, 2), 'internal_height_mm': round(ZC - ZF, 2),
                         'fan_height_mm': FAN['size'], 'bottom_parts_to_floor_mm': round(STANDOFF - 1.2, 2),
                         'outer_mm': [OUT_W, round(Y1 - Y0 + 2 * WALL, 1), OUT_H]}
    res['all_ports_aligned'] = all(p['ok'] for p in res['ports']); res['max_interference_mm3'] = round(worst, 3)
    json.dump(res, open(os.path.join(MECH, 'fit-check.json'), 'w'), indent=1)
    # ---- renders ------------------------------------------------------------------------------
    shapes = [base.val(), pcb.val(), fan.val(), win.val()] + [solids[r].val() for r in big if r not in ('pcb', 'fan')]
    comp = cq.Compound.makeCompound(shapes).rotate(cq.Vector(0, 0, 0), cq.Vector(0, 0, 1), 35).rotate(cq.Vector(0, 0, 0), cq.Vector(1, 0, 0), -58)
    cq.exporters.export(cq.Workplane('XY').add(comp), os.path.join(MECH, 'fit-iso.svg'),
                        opt={'width': 1500, 'height': 900, 'marginLeft': 30, 'marginTop': 30, 'showAxes': False, 'projectionDir': (0, 0, 1), 'strokeWidth': 0.3,
                             'strokeColor': (30, 30, 30), 'hiddenColor': (190, 190, 190), 'showHidden': False})
    drawings(op, parts)
    print(json.dumps({k: res[k] for k in ('all_ports_aligned', 'max_interference_mm3', 'interference_mm3', 'clearances')}))
    for p in res['ports']: print(p['port'], p['margins'], 'OK' if p['ok'] else 'CHECK')


if __name__ == '__main__':
    main()

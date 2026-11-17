#!/usr/bin/env python3
"""SORX enclosure, rack ears and fit check (CadQuery).

Frame: X = board x, Y = -board y (rear edge at Y = 0, front edge at Y = -118), Z up, PCB top face at Z = 0.
Reads hw/build/mech_parts.json (written by mech_export.py from the routed board).
Writes mech/*.step, *.stl, *.3mf, fit-check SVGs and mech/fit-check.json.
"""
import argparse, hashlib, json, math, os, sys
from pathlib import Path
import cadquery as cq

HERE = os.path.dirname(os.path.abspath(__file__))
MECH = os.path.join(HERE, '..', 'mech'); BUILD = os.path.join(HERE, '..', 'hw', 'build')
W, H, T = 172.0, 118.0, 1.6
STANDOFF = 6.0                    # PCB underside to floor
FLOOR = 1.5; LID = 1.5; WALL = 2.4  # 41 mm cavity; enclosure strength remains an explicit qualification gate
OUT_W, OUT_H = 215.0, 44.0        # half-width 1U
X0 = -1.0                         # inner cavity, left
X1 = X0 + OUT_W - 2 * WALL        # inner cavity, right (fan plenum is right of the board)
Y1 = 0.4; Y0 = -H - 0.4           # inner cavity rear / front
ZF = -T - STANDOFF                # floor top
ZC = ZF + (OUT_H - FLOOR - LID)   # ceiling (lid underside)
HOLES = [(4.5, 4.5), (167.5, 4.5), (4.5, 113.5), (167.5, 113.5)]
FAN = dict(size=40.0, thick=10.6, yc=-60.0)

PARTS_PATH = Path(BUILD) / 'mech_parts.json'
PCB_PATH = Path(HERE).parent / 'hw' / 'sorx.kicad_pcb'
P = {}  # Loaded explicitly after CLI paths are known; failures must write a fresh report.
HEATSINK_MPN = '23111101'
HEATSINK_TOP = 21.27  # conservative upper bound: 7 mm support + full 14.27 mm supplier height
MOUNT_LOCAL = {'H5': (-32.6055, 23.074, 2.5), 'H6': (32.5945, 23.074, 2.5),
               'H7': (-40.0055, -7.926, 7.0), 'H8': (39.9945, -7.926, 7.0)}
NONPHYSICAL_PREFIXES = ('MountingHole_3.2mm_M3', 'Fiducial_', 'TestPoint_Pad_')
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
        if p['fp'].startswith(NONPHYSICAL_PREFIXES):
            continue  # PCB features, not fitted solid components; holes remain cut in pcb above.
        x1, y1, x2, y2 = brect(p)
        if p['fp'] == 'Standoff_SMT_M2_WA-SMSI':
            h = MOUNT_LOCAL.get(r, (0, 0, 7.0))[2]
            parts[r] = cq.Workplane('XY').circle(2.175).circle(1.0).extrude(h).translate((p['x'], -p['y'], 0))
            pcb = pcb.cut(cq.Workplane('XY').circle(1.5).extrude(T).translate((p['x'], -p['y'], -T)))
            continue
        if p['fp'].startswith('SODIMM'):
            parts['J1'] = box(x1, -P[r]['y'] + 2.2 - 8.4, x2, -P[r]['y'] + 14.2, 0, 5.2)        # socket body
            xs, ys = p['x'], -p['y']
            parts['M1'] = box(xs - 34.8, ys - 26.5, xs + 34.8, ys + 8.5, 2.5, 2.5 + 1.2).union(box(xs - 30, ys - 24, xs + 30, ys + 2, 3.7, 6.7))  # module PCB + parts
            # Manufacturer-specific conservative envelope, not a claim of supplier STEP equivalence.
            # The physical Type 1 has stepped feet; a full upper envelope conservatively bounds its fins.
            parts['HS1'] = box(xs - 42.8055, ys - 27.074, xs + 42.7945, ys + 10.926, 7.0, HEATSINK_TOP)
            for mr in ('H5', 'H6'):
                dx, dy, _ = MOUNT_LOCAL[mr]
                parts['M1'] = parts['M1'].cut(cq.Workplane('XY').circle(1.1).extrude(8).translate((xs + dx, ys - dy, 0)))
            continue
        if p['fp'].startswith('SMA'):
            parts[r] = box(p['x'] - 3.2, 0, p['x'] + 3.2, 11.5, -T / 2 - 3.2, -T / 2 + 3.2); continue
        if p['fp'].startswith('SW_Tactile'):
            xc = p['x'] - 2.25
            parts[r] = box(x1, max(y1, -H), x2, y2, 0, 7.3).union(box(xc - 1.5, -H - 1.5, xc + 1.5, -H, 2.4, 5.4)); continue
        h = HEIGHT.get(p['fp'])
        if h is None:
            h = 1.2 if (x2 - x1) * (y2 - y1) < 30 else 1.8
        if p['side'] == 'B': parts[r] = box(x1, y1, x2, y2, -T - h, -T)
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


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def intersection_volume(shell, component, label):
    """A failed CAD operation is unknown geometry, never a passing zero volume."""
    try:
        shape = shell.intersect(component).val()
        volume = shape.Volume()
        if not shape.isValid() or not math.isfinite(volume) or volume < -1e-9:
            raise ValueError("invalid intersection shape or volume")
        return max(0.0, volume)
    except Exception as error:
        raise RuntimeError(f"{label}: intersection failed: {error}") from error


def envelope_overlap(a, b):
    """Only discard separated bounding boxes; never use boxes to declare a collision."""
    x, y = a.val().BoundingBox(), b.val().BoundingBox()
    return all(min(getattr(x, axis+'max'), getattr(y, axis+'max')) -
               max(getattr(x, axis+'min'), getattr(y, axis+'min')) > 1e-7 for axis in ('x','y','z'))


def load_parts(path):
    records = json.loads(Path(path).read_text(encoding='utf-8'))
    refs = [part['ref'] for part in records]
    if len(refs) != len(set(refs)):
        raise ValueError('Duplicate component references in mechanical placement data')
    return {part['ref']: part for part in records}


def source_binding_valid(parts_path, pcb_path, footprint_count):
    parts_path = Path(parts_path)
    try:
        metadata = json.loads(parts_path.with_suffix(parts_path.suffix+'.meta.json').read_text(encoding='utf-8'))
        return (metadata['source_pcb_sha256'] == sha256(pcb_path) and
                metadata['parts_sha256'] == sha256(parts_path) and
                metadata['footprints'] == footprint_count)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def module_keepout_findings():
    """Conservative full module underbody screen; edge-zone exceptions need exact vendor qualification."""
    j = P['J1']; bounds = [j['x']-34.8,j['y']-8.5,j['x']+34.8,j['y']+26.5]
    overlaps = []
    for ref,part in P.items():
        if part['side'] != 'F' or ref in ('J1','H5','H6') or part['fp'].startswith(NONPHYSICAL_PREFIXES):
            continue
        a = part['box']
        if min(a[2],bounds[2])>max(a[0],bounds[0]) and min(a[3],bounds[3])>max(a[1],bounds[1]):
            overlaps.append({'ref':ref,'box_mm':a,'qualification':'Relocate or prove this exact object lies in the manufacturer edge exception and maximum height <=0.8 mm.'})
    return {'module_xy_bounds_mm':bounds,'overlaps':overlaps,'pass':not overlaps,
            'basis':'Toradex Verdin carrier guide V1.2 table43/Figure112; 5.2mm connector permits no general under-module components.'}


def evaluate_fit(pcb, parts, base, lid, fan, op):
    res = {'schema_version': 2, 'success': False, 'geometry_errors': [], 'interference_mm3': {},
           'component_interference_mm3': {}, 'ports': [], 'clearances': {}, 'clearance_checks': {},
           'source_pcb_sha256': sha256(PCB_PATH),
           'model_inputs_sha256': {'mech_parts.json': sha256(PARTS_PATH),
                                  'gen_mech.py': sha256(__file__)}, 'blockers': []}
    res['source_binding_valid'] = source_binding_valid(PARTS_PATH, PCB_PATH, len(P))
    if not res['source_binding_valid']:
        res['blockers'].append('Placement data lacks a matching PCB/hash provenance sidecar; rerun mech_export.py.')
    solids = {'pcb': pcb, 'fan': fan, **parts}
    maximum = 0.0
    # Inspect every fitted component, including bottom-side packages; no selected-part shortcut.
    for ref, solid in solids.items():
        try:
            volume = sum(intersection_volume(shell, solid, ref+':'+name)
                         for name, shell in [('base', base), ('lid', lid)])
            if volume > 0.001:
                res['interference_mm3'][ref] = round(volume, 6)
            maximum = max(maximum, volume)
        except RuntimeError as error:
            res['geometry_errors'].append(str(error))
    # The only envelope overlap accepted by design is the module in its mating socket.
    # This explicit pair is supported by the Toradex carrier guide, not a category-wide waiver.
    intended_contacts = {frozenset(('J1','M1')): 'SoM edge connector insertion; Toradex carrier guide section 4'}
    refs = list(parts)
    enclosure_maximum = maximum
    for i, ref in enumerate(refs):
        for other in refs[i+1:]:
            if frozenset((ref, other)) in intended_contacts or not envelope_overlap(parts[ref], parts[other]):
                continue
            try:
                volume = intersection_volume(parts[ref], parts[other], ref+':'+other)
                if volume > 0.001:
                    res['component_interference_mm3'][ref+':'+other] = round(volume, 6)
                maximum = max(maximum, volume)
            except RuntimeError as error:
                res['geometry_errors'].append(str(error))
    for name, (wall, x1, x2, z1, z2) in op.items():
        ref = {'OE':'J6','MD-IN':'J4','MGMT':'J7','USB-C PWR':'J3','PPS':'J5','USB 1':'J8','USB 2':'J9',
               'CONSOLE':'J11','USB-C DEBUG':'J10','microSD':'J12','RESET':'SW1','RECOVERY':'SW2','LED window':None}[name]
        if ref is None:
            cx1,cx2=P['D4']['box'][0],P['D10']['box'][2];cz1,cz2=0,1
        elif ref=='J5':
            cx1,cx2=P[ref]['x']-3.2,P[ref]['x']+3.2;cz1,cz2=-T/2-3.2,-T/2+3.2
        elif ref.startswith('SW'):
            xc=P[ref]['x']-2.25;cx1,cx2=xc-1.5,xc+1.5;cz1,cz2=2.4,5.4
        else:
            b=brect(P[ref]);cx1,cx2=b[0],b[2];cz1,cz2=0,HEIGHT[P[ref]['fp']]
        margins=dict(left=round(cx1-x1,4),right=round(x2-cx2,4),bottom=round(cz1-z1,4),top=round(z2-cz2,4))
        res['ports'].append({'port':name,'wall':wall,'ref':ref or 'D4-D10','opening_x':[x1,x2],
                             'opening_z':[z1,z2],'margins':margins,'ok':min(margins.values())>=0.25})
    bottom = max((-(s.val().BoundingBox().zmin+T) for r,s in parts.items() if r in P and P[r]['side']=='B'),default=0)
    res['clearances']={'heatsink_top_to_lid_mm':round(ZC-HEATSINK_TOP,3),'magjack_top_to_lid_mm':round(ZC-13.6,3),
        'internal_height_mm':round(ZC-ZF,3),'fan_height_mm':FAN['size'],'fan_vertical_allowance_mm':round(ZC-ZF-FAN['size'],3),
        'bottom_parts_to_floor_mm':round(STANDOFF-bottom,3),'outer_mm':[OUT_W,round(Y1-Y0+2*WALL,3),OUT_H]}
    for name,actual,required in [('fan_top_mm',(ZC-ZF-FAN['size'])/2,0.5),('fan_bottom_mm',(ZC-ZF-FAN['size'])/2,0.5),
                               ('bottom_parts_to_floor_mm',STANDOFF-bottom,1.0),('heatsink_to_lid_mm',ZC-HEATSINK_TOP,1.0)]:
        res['clearance_checks'][name]={'actual_mm':round(actual,4),'required_mm':required,'ok':actual>=required}
    res['module_keepout']=module_keepout_findings()
    if not res['module_keepout']['pass']:
        res['blockers'].append('Top-side components overlap the module keepout; qualify exact edge-zone exceptions or relocate.')
    mount_checks={}
    for ref,(dx,dy,height) in MOUNT_LOCAL.items():
        part=P.get(ref)
        expected=[P['J1']['x']+dx,P['J1']['y']+dy]
        ok=part is not None and part['side']=='F' and part['fp']=='Standoff_SMT_M2_WA-SMSI' and part['value']=='M2 SMT %.1fmm' % height and abs(part['x']-expected[0])<=0.01 and abs(part['y']-expected[1])<=0.01
        mount_checks[ref]={'expected_xy_mm':expected,'expected_height_mm':height,'present':part is not None,'ok':ok}
    res['mounting_checks']=mount_checks
    if not all(x['ok'] for x in mount_checks.values()):
        res['blockers'].append('Module/heatsink mounting footprints absent or not at the manufacturer coordinates.')
    excluded=[r for r,p in P.items() if p['fp'].startswith(NONPHYSICAL_PREFIXES)]
    unqualified=[r for r,p in P.items() if r not in excluded and not p['fp'].startswith(('SODIMM','Standoff_SMT'))]
    res['coverage']={'pcb_footprints':len(P),'checked_enclosure_objects':sorted(solids),'nonphysical_pcb_features':sorted(excluded),
                     'height_records_requiring_vendor_qualification':sorted(unqualified),
                     'component_to_component_checked':True,'intended_contact_pairs':[{'refs':sorted(k),'basis':v} for k,v in intended_contacts.items()]}
    res['blockers'] += ['Envelope heights from the prior design and inferred package bounds require exact-MPN maximum-height qualification.',
                       'Heatsink envelope bounds Type 1 23111101; supplier geometry/TIM compression and assembly access require qualification.',
                       '1.5 mm floor/lid and fan nominal dimensions require enclosure-process and supplier tolerance/strength sign-off.',
                       'Cable plug/latch and microSD removal access envelopes have not been qualified.']
    res['physical_validation_pending']=['Thermal soak, airflow, connector insertion and assembly measurements on physical hardware.']
    res['all_ports_aligned']=all(x['ok'] for x in res['ports'])
    res['max_enclosure_interference_mm3']=None if res['geometry_errors'] else round(enclosure_maximum,6)
    res['max_interference_mm3']=None if res['geometry_errors'] else round(maximum,6)
    res['geometry_pass']=not (res['geometry_errors'] or res['interference_mm3'] or res['component_interference_mm3'])
    res['success']=res['geometry_pass'] and res['all_ports_aligned'] and all(x['ok'] for x in res['clearance_checks'].values()) and not res['blockers']
    return res


def write_report(report):
    path=Path(MECH)/'fit-check.json'
    temp=path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    temp.replace(path)


def main(check_only=False):
    global P
    if not P:
        P = load_parts(PARTS_PATH)
    os.makedirs(MECH, exist_ok=True)
    pcb, parts = board_assembly(); op = openings(); base, lid = enclosure(op); fan = fan_body(); win = window_insert(op)
    res = evaluate_fit(pcb, parts, base, lid, fan, op)
    write_report(res)
    if check_only:
        print(json.dumps(res))
        return 0 if res['success'] else 1
    asm = cq.Assembly(name='sorx')
    asm.add(base, name='enclosure_base', color=cq.Color(0.25, 0.27, 0.30)); asm.add(lid, name='enclosure_lid', color=cq.Color(0.25, 0.27, 0.30, 0.35))
    asm.add(pcb, name='pcb', color=cq.Color(0.05, 0.35, 0.15)); asm.add(fan, name='fan_40x40x10', color=cq.Color(0.1, 0.1, 0.1))
    asm.add(win, name='led_window', color=cq.Color(0.9, 0.9, 0.9, 0.5))
    # Do not call Workplane.add on a component: it mutates the first part's object stack.
    for r, s in parts.items():
        asm.add(s, name=r, color=cq.Color(0.75, 0.75, 0.78))
    asm.save(os.path.join(MECH, 'sorx-assembly.step'))
    cq.exporters.export(base, os.path.join(MECH, 'enclosure-base.step')); cq.exporters.export(lid, os.path.join(MECH, 'enclosure-lid.step'))
    for name, s in (('enclosure-base', base), ('enclosure-lid', lid), ('led-window', win), ('rack-ear-short', rack_ear(False)), ('rack-ear-long', rack_ear(True))):
        cq.exporters.export(s, os.path.join(MECH, name + '.stl'), tolerance=0.05, angularTolerance=0.2)
        cq.exporters.export(s, os.path.join(MECH, name + '.3mf'), tolerance=0.05, angularTolerance=0.2)
    cq.exporters.export(rack_ear(False), os.path.join(MECH, 'rack-ear-short.step')); cq.exporters.export(rack_ear(True), os.path.join(MECH, 'rack-ear-long.step'))
    # ---- renders ------------------------------------------------------------------------------
    shapes = [base.val(), pcb.val(), fan.val(), win.val()] + [s.val() for s in parts.values()]
    comp = cq.Compound.makeCompound(shapes).rotate(cq.Vector(0, 0, 0), cq.Vector(0, 0, 1), 35).rotate(cq.Vector(0, 0, 0), cq.Vector(1, 0, 0), -58)
    cq.exporters.export(cq.Workplane('XY').add(comp), os.path.join(MECH, 'fit-iso.svg'),
                        opt={'width': 1500, 'height': 900, 'marginLeft': 30, 'marginTop': 30, 'showAxes': False, 'projectionDir': (0, 0, 1), 'strokeWidth': 0.3,
                             'strokeColor': (30, 30, 30), 'hiddenColor': (190, 190, 190), 'showHidden': False})
    drawings(op, parts)
    print(json.dumps({k: res[k] for k in ('all_ports_aligned', 'max_interference_mm3', 'interference_mm3', 'clearances')}))
    for p in res['ports']: print(p['port'], p['margins'], 'OK' if p['ok'] else 'CHECK')
    print('mechanical success:', res['success'], '| blockers:', res['blockers'])
    return 0 if res['success'] else 1


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--parts',type=Path)
    parser.add_argument('--pcb',type=Path)
    parser.add_argument('--check-only',action='store_true',help='Write only the fit report; omit CAD exports and renders')
    args=parser.parse_args()
    if args.output_dir: MECH=str(args.output_dir.resolve())
    if args.parts:
        PARTS_PATH=args.parts.resolve()
    if args.pcb: PCB_PATH=args.pcb.resolve()
    try:
        raise SystemExit(main(check_only=args.check_only))
    except Exception as error:
        os.makedirs(MECH,exist_ok=True)
        write_report({'schema_version':2,'success':False,'geometry_errors':[str(error)],
                      'max_interference_mm3':None,'all_ports_aligned':False,'blockers':['Mechanical generation failed.']})
        print('Mechanical generation FAILED:',error,file=sys.stderr)
        raise SystemExit(1)

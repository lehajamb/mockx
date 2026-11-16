#!/usr/bin/env python3
"""Generate the KiCad 7 schematic (hw/sorx.kicad_sch + one file per sheet) from design.py."""
import os, uuid, math, collections, json
from sx import Sym as S, dump, parse, find, first
import kilib
from design import DESIGN, VERDIN_PINS

HW = os.path.join(os.path.dirname(__file__), '..', 'hw')
NS = uuid.UUID('5b0d0c1e-0000-4000-8000-50525800a001')
def U(*k): return str(uuid.uuid5(NS, '/'.join(map(str, k))))
G = 2.54
SHEETS = [('som', 'SoM socket, RTC, FRAM, reset'), ('power', 'USB-C PD input, protection, regulators'), ('eth_md', 'MD-IN port and PPS input'),
          ('eth_oe', 'OE port: Intel I210 on PCIe'), ('eth_mgmt', 'MGMT port: KSZ9131 RGMII PHY'), ('usb_console', 'USB-A ports, debug USB-C, RS-232 console'),
          ('mgmt_io', 'microSD, LEDs, watchdog, sensors, fan')]

def fx(size=1.27, hide=False, just=None):
    e = [S('effects'), [S('font'), [S('size'), size, size]]]
    if just: e.append([S('justify')] + [S(j) for j in just])
    if hide: e.append(S('hide'))
    return e
def prop(k, v, x=0, y=0, hide=True, a=0, just=None):
    return [S('property'), k, v, [S('at'), x, y, a], fx(hide=hide, just=just)]
def pin(typ, num, name, x, y, ang, length=2.54):
    return [S('pin'), S(typ), S('line'), [S('at'), x, y, ang], [S('length'), length], [S('name'), name, fx(1.0)], [S('number'), num, fx(1.0)]]
def rect(x1, y1, x2, y2, fill='background'):
    return [S('rectangle'), [S('start'), x1, y1], [S('end'), x2, y2], [S('stroke'), [S('width'), 0.254], [S('type'), S('default')]], [S('fill'), [S('type'), S(fill)]]]
def text(t, x, y, size=1.0):
    return [S('text'), t, [S('at'), x, y, 0], fx(size)]

def two_pin(name, tag, p1=('1', '1', 'passive'), p2=('2', '2', 'passive'), refp='R'):
    return [S('symbol'), 'SORX:' + name, [S('pin_numbers'), S('hide')], [S('pin_names'), [S('offset'), 0], S('hide')], [S('in_bom'), S('yes')], [S('on_board'), S('yes')],
            prop('Reference', refp, 0, 2.54, False), prop('Value', name, 0, -2.54, False), prop('Footprint', ''), prop('Datasheet', ''),
            [S('symbol'), name + '_0_1', rect(-2.54, -1.27, 2.54, 1.27), text(tag, 0, 0, 0.9)],
            [S('symbol'), name + '_1_1', pin(p1[2], p1[0], p1[1], -5.08, 0, 0), pin(p2[2], p2[0], p2[1], 5.08, 0, 180)]]

def custom_symbols():
    out = {}
    out['R'] = two_pin('R', 'R'); out['C'] = two_pin('C', 'C', refp='C'); out['L'] = two_pin('L', 'L', refp='L'); out['FB'] = two_pin('FB', 'FB', refp='FB')
    out['CP'] = two_pin('CP', 'C+', ('1', '+', 'passive'), ('2', '-', 'passive'), 'C')
    for n, t in (('D', 'A>K'), ('TVS', 'TVS'), ('ESD', 'ESD'), ('LED', 'LED')):
        out[n] = two_pin(n, t, ('2', 'A', 'passive'), ('1', 'K', 'passive'), 'D')
    out['TP'] = [S('symbol'), 'SORX:TP', [S('pin_numbers'), S('hide')], [S('pin_names'), S('hide')], [S('in_bom'), S('no')], [S('on_board'), S('yes')],
                 prop('Reference', 'TP', 0, 2.54, False), prop('Value', 'TP', 0, -2.54, False), prop('Footprint', ''), prop('Datasheet', ''),
                 [S('symbol'), 'TP_0_1', [S('circle'), [S('center'), 1.27, 0], [S('radius'), 0.762], [S('stroke'), [S('width'), 0.254], [S('type'), S('default')]], [S('fill'), [S('type'), S('none')]]]],
                 [S('symbol'), 'TP_1_1', pin('passive', '1', '1', -2.54, 0, 0, 3.048)]]
    out['SOM'] = [S('symbol'), 'SORX:SOM', [S('in_bom'), S('yes')], [S('on_board'), S('no')],
                  prop('Reference', 'M', 0, 5.08, False), prop('Value', 'SOM', 0, -5.08, False), prop('Footprint', ''), prop('Datasheet', ''),
                  [S('symbol'), 'SOM_0_1', rect(-17.78, -3.81, 17.78, 3.81), text('System on module (plugs into J1)', 0, 0, 1.27)]]
    q = [S('symbol'), 'SORX:NMOS_SO8', [S('pin_names'), [S('offset'), 0.5]], [S('in_bom'), S('yes')], [S('on_board'), S('yes')],
         prop('Reference', 'Q', 0, 8.89, False), prop('Value', 'NMOS_SO8', 0, -8.89, False), prop('Footprint', ''), prop('Datasheet', ''),
         [S('symbol'), 'NMOS_SO8_0_1', rect(-5.08, -7.62, 5.08, 7.62)], [S('symbol'), 'NMOS_SO8_1_1']]
    for i, y in zip((1, 2, 3), (5.08, 2.54, 0)): q[-1].append(pin('passive', str(i), 'S', -7.62, y, 0))
    q[-1].append(pin('input', '4', 'G', -7.62, -5.08, 0))
    for i, y in zip((5, 6, 7, 8), (5.08, 2.54, 0, -2.54)): q[-1].append(pin('passive', str(i), 'D', 7.62, y, 180))
    out['NMOS_SO8'] = q
    # Verdin socket: 4 units, odd pins left, even pins right
    v = [S('symbol'), 'SORX:Verdin_SODIMM260', [S('pin_names'), [S('offset'), 0.5]], [S('in_bom'), S('yes')], [S('on_board'), S('yes')],
         prop('Reference', 'J', 0, 45.72, False), prop('Value', 'Verdin_SODIMM260', 0, -45.72, False), prop('Footprint', ''), prop('Datasheet', '')]
    per = 66
    for u in range(4):
        lo = u * per + 1; hi = min(260, lo + per - 1)
        v.append([S('symbol'), 'Verdin_SODIMM260_%d_1' % (u + 1), rect(-25.4, -43.18, 25.4, 43.18)])
        row = 0
        for n in range(lo, hi + 1, 2):
            y = 40.64 - row * G; row += 1
            for num, x, ang in ((n, -27.94, 0), (n + 1, 27.94, 180)):
                if num > 260: continue
                sig = VERDIN_PINS[num]
                typ = 'power_in' if sig in ('GND', 'VCC', 'VCC_BACKUP') else 'power_out' if sig == 'PWR_1V8_MOCI' else 'bidirectional'
                v[-1].append(pin(typ, str(num), sig, x, y, ang))
        if u == 3:
            v[-1].append(pin('passive', '261', 'MP', -27.94, -40.64, 0)); v[-1].append(pin('passive', '262', 'MP', 27.94, -40.64, 180))
    out['Verdin_SODIMM260'] = v
    return out
CUSTOM = custom_symbols()

_symcache = {}
def get_sym(symid):
    if symid not in _symcache:
        lib, name = symid.split(':')
        if lib == 'SORX': s = CUSTOM[name]
        else:
            s = kilib.get_symbol(lib, name); s[1] = symid
        _symcache[symid] = (s, kilib.pins(s))
    return _symcache[symid]

def resolve(part):
    """pin number -> net (None = explicit no-connect).  Raises on unmapped pins."""
    sym, pins = get_sym(part.sym)
    nums = {p['number'] for p in pins}
    out = {}
    for key, net in part.pins.items():
        if key in nums: hit = [key]
        else:
            hit = [p['number'] for p in pins if p['name'] == key]
        assert hit, '%s: no pin %r in %s' % (part.ref, key, part.sym)
        for h in hit:
            assert h not in out or out[h] == net, (part.ref, h)
            out[h] = net
    for k in part.nc:
        assert k in nums, (part.ref, k); assert k not in out, (part.ref, k); out[k] = None
    for p in pins:
        if p['number'] not in out:
            assert p['type'] in ('no_connect',), '%s: pin %s (%s) not mapped' % (part.ref, p['number'], p['name'])
            out[p['number']] = None
    return out

def netmap():
    nm = collections.defaultdict(set)
    for p in DESIGN.parts.values():
        for num, net in resolve(p).items():
            if net: nm[net].add((p.ref, num))
    return nm

def lab_len(net): return len(net) * 1.25 + 4.5

def build_sheet(name, title, root_uuid, sheet_uuid, page, flags):
    items = []; libsyms = collections.OrderedDict()
    parts = [p for p in DESIGN.parts.values() if p.sheet == name]
    W = 540.0; cx = 20.0; cy = 30.0; rowh = 0.0; maxx = 0.0
    placed = []
    def place(ref, symid, value, fp, pinnets, props, unit, units, inbom, onboard, dnp):
        nonlocal cx, cy, rowh, maxx
        sym, pins = get_sym(symid); libsyms[symid] = sym
        up = [p for p in pins if p['unit'] in (0, unit)]
        xs = [-2.54, 2.54]; ys = [-2.54, 2.54]
        for p in up:
            net = pinnets.get(p['number']); L = lab_len(net) if net else 2.0
            a = p['angle']
            xs += [p['x'] - (L if a == 0 else 0), p['x'] + (L if a == 180 else 0)]
            ys += [p['y'] - (L if a == 90 else 0), p['y'] + (L if a == 270 else 0)]
        x0, x1, y0, y1 = min(xs) - 3, max(xs) + 3, min(ys) - 3, max(ys) + 7
        w = x1 - x0; h = y1 - y0
        if cx + w > W and cx > 20.0:
            cx = 20.0; cy += rowh + 4; rowh = 0.0
        ox = math.ceil((cx - x0) / G) * G; oy = math.ceil((cy + y1) / G) * G
        cx = ox + x1 + 2; rowh = max(rowh, h); maxx = max(maxx, cx)
        s = [S('symbol'), [S('lib_id'), symid], [S('at'), ox, oy, 0], [S('unit'), unit], [S('in_bom'), S('yes' if inbom else 'no')], [S('on_board'), S('yes' if onboard else 'no')],
             [S('dnp'), S('yes' if dnp else 'no')], [S('uuid'), U('sym', ref, unit)],
             prop('Reference', ref, ox + x0 + 3, oy - y1 + 2.5, False, just=['left']), prop('Value', value, ox + x0 + 3, oy - y1 + 4.6, False, just=['left']),
             prop('Footprint', fp, ox, oy), prop('Datasheet', '~', ox, oy)]
        for k, v in props:
            if v: s.append(prop(k, v, ox, oy))
        for p in up: s.append([S('pin'), p['number'], [S('uuid'), U('pin', ref, unit, p['number'])]])
        s.append([S('instances'), [S('project'), 'sorx', [S('path'), '/%s/%s' % (root_uuid, sheet_uuid), [S('reference'), ref], [S('unit'), unit]]]])
        items.append(s)
        seen = set()
        for p in up:
            X = round(ox + p['x'], 4); Y = round(oy - p['y'], 4); net = pinnets.get(p['number'])
            if (X, Y, net) in seen: continue
            seen.add((X, Y, net))
            if net is None:
                if p['type'] != 'no_connect': items.append([S('no_connect'), [S('at'), X, Y], [S('uuid'), U('nc', ref, p['number'])]])
                continue
            ang, just = {0: (180, 'right'), 180: (0, 'left'), 90: (270, 'right'), 270: (90, 'left')}[p['angle']]
            items.append([S('global_label'), net, [S('shape'), S('passive')], [S('at'), X, Y, ang], [S('fields_autoplaced')], fx(just=[just]), [S('uuid'), U('lbl', ref, unit, p['number'])],
                          [S('property'), 'Intersheetrefs', '${INTERSHEET_REFS}', [S('at'), 0, 0, 0], fx(hide=True)]])
    for p in parts:
        sym, pins = get_sym(p.sym); pn = resolve(p)
        units = sorted({q['unit'] for q in pins if q['unit'] > 0}) or [1]
        props = [('MPN', p.mpn), ('Manufacturer', p.mfr), ('Description', p.desc), ('Alternates', p.alt)]
        for u in units:
            place(p.ref, p.sym, p.value + (' (DNP)' if p.dnp else ''), p.fp, pn, props, u, units, p.inbom, bool(p.fp), p.dnp)
    for i, net in enumerate(flags):
        place('#FLG%s%02d' % (page, i + 1), 'power:PWR_FLAG', 'PWR_FLAG', '', {'1': net}, [], 1, [1], False, False, False)
    H = cy + rowh + 55; Wp = max(maxx + 20, 297)
    items.insert(0, [S('text'), 'SORX carrier rev A  -  %s' % title, [S('at'), 20, 14, 0], fx(3.0, just=['left'])])
    doc = [S('kicad_sch'), [S('version'), 20230121], [S('generator'), S('sorx_gen_sch')], [S('uuid'), sheet_uuid if name else root_uuid],
           [S('paper'), 'User', round(Wp, 1), round(max(H, 210), 1)],
           [S('title_block'), [S('title'), 'SORX carrier - ' + title], [S('date'), '2026-11-16'], [S('rev'), 'A'], [S('company'), 'SORX'], [S('comment'), 1, 'Generated by tools/gen_sch.py from tools/design.py']],
           [S('lib_symbols')] + list(libsyms.values())] + items
    return doc

def main():
    os.makedirs(HW, exist_ok=True)
    root_uuid = U('root')
    nm = netmap()
    # PWR_FLAG where a net has power_in pins but no power_out pin
    types = {}
    for p in DESIGN.parts.values():
        sym, pins = get_sym(p.sym)
        for q in pins: types[(p.ref, q['number'])] = q['type']
    need = [n for n, nodes in sorted(nm.items()) if any(types[x] == 'power_in' for x in nodes) and not any(types[x] == 'power_out' for x in nodes)]
    sheet_of_net = {}
    for p in DESIGN.parts.values():
        for net in resolve(p).values():
            if net: sheet_of_net.setdefault(net, p.sheet)
    root_items = [[S('text'), 'SORX single-board smart order router  -  carrier board rev A', [S('at'), 20, 20, 0], fx(4.0, just=['left'])],
                  [S('text'), 'Management / control / forwarding plane split. All sheets connect through global labels.\\nSource of truth: tools/design.py.  See docs/ for block diagram, power budget and bring-up.', [S('at'), 20, 30, 0], fx(1.8, just=['left'])]]
    for i, (name, title) in enumerate(SHEETS):
        su = U('sheet', name); page = str(i + 2)
        flags = [n for n in need if sheet_of_net[n] == name]
        doc = build_sheet(name, title, root_uuid, su, page, flags)
        open(os.path.join(HW, 'sorx_%s.kicad_sch' % name), 'w').write(dump(doc) + '\n')
        x = 20 + (i % 4) * 65; y = 50 + (i // 4) * 45
        root_items.append([S('sheet'), [S('at'), x, y], [S('size'), 55, 30], [S('fields_autoplaced')], [S('stroke'), [S('width'), 0.1524], [S('type'), S('solid')]],
                           [S('fill'), [S('color'), 0, 0, 0, 0.0]], [S('uuid'), su],
                           [S('property'), 'Sheetname', '%s' % name, [S('at'), x, y - 0.7, 0], fx(just=['left', 'bottom'])],
                           [S('property'), 'Sheetfile', 'sorx_%s.kicad_sch' % name, [S('at'), x, y + 30.6, 0], fx(just=['left', 'top'])],
                           [S('instances'), [S('project'), 'sorx', [S('path'), '/' + root_uuid, [S('page'), page]]]]])
        root_items.append([S('text'), title, [S('at'), x + 2, y + 12, 0], fx(1.3, just=['left'])])
    root = [S('kicad_sch'), [S('version'), 20230121], [S('generator'), S('sorx_gen_sch')], [S('uuid'), root_uuid], [S('paper'), 'A4'],
            [S('title_block'), [S('title'), 'SORX carrier board'], [S('date'), '2026-11-16'], [S('rev'), 'A'], [S('company'), 'SORX']],
            [S('lib_symbols')]] + root_items + [[S('sheet_instances'), [S('path'), '/', [S('page'), '1']]]]
    open(os.path.join(HW, 'sorx.kicad_sch'), 'w').write(dump(root) + '\n')
    # custom symbol library
    lib = [S('kicad_symbol_lib'), [S('version'), 20220914], [S('generator'), S('sorx_gen_sch')]]
    import copy
    for k, s in CUSTOM.items():
        c = copy.deepcopy(s); c[1] = k; lib.append(c)
    open(os.path.join(HW, 'sorx.kicad_sym'), 'w').write(dump(lib) + '\n')
    open(os.path.join(HW, 'sym-lib-table'), 'w').write('(sym_lib_table\n  (lib (name "SORX")(type "KiCad")(uri "${KIPRJMOD}/sorx.kicad_sym")(options "")(descr "SORX custom symbols"))\n)\n')
    open(os.path.join(HW, 'fp-lib-table'), 'w').write('(fp_lib_table\n  (lib (name "sorx")(type "KiCad")(uri "${KIPRJMOD}/sorx.pretty")(options "")(descr "SORX custom footprints"))\n)\n')
    json.dump({k: sorted(v) for k, v in nm.items()}, open(os.path.join(HW, '..', 'tools', 'netmap.json'), 'w'), indent=0)
    print('sheets written; nets', len(nm), 'pwr flags', len(need))

if __name__ == '__main__':
    main()

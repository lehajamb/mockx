#!/usr/bin/env python3
"""BOM, cost roll-up and stock-check tables from design.py + bom/stock_notes.json."""
import csv, json, os, re, collections
from design import DESIGN
HERE = os.path.dirname(os.path.abspath(__file__)); BOM = os.path.join(HERE, '..', 'bom')
NOTES = json.load(open(os.path.join(BOM, 'stock_notes.json')))
CHECKED = NOTES['_checked']
MOUSER_PREFIX = {'Yageo': '603-', 'Murata': '81-', 'Panasonic': '667-', 'Texas Instruments': '595-', 'Coilcraft': '994-', 'Diodes Inc.': '621-', 'TE Connectivity': '571-',
                 'Infineon': '727-', 'Keystone': '534-', 'Alps Alpine': '688-', 'Wurth Elektronik': '710-', 'GCT': '640-', 'Littelfuse': '576-', 'Pulse Electronics': '673-',
                 'Amphenol RF': '523-', 'onsemi': '863-', 'Intel': '607-', 'Abracon': '815-', 'Winbond': '454-', 'Microchip': '579-', 'STMicroelectronics': '511-', 'FTDI': '895-',
                 'Amphenol ICC': '649-', 'Hirose': '798-', 'Alpha & Omega': '942-', 'Kingbright': '604-', 'Analog Devices': '700-', 'Molex': '538-', 'Toradex': '145-'}
# estimated unit prices (USD) at 10 / 100 / 1000 for parts that were not individually checked
EST = {'XAL5050-103MEC': (2.60, 2.00, 1.45), 'LMR33630ADDAR': (3.20, 2.50, 1.70), 'AZ1117CH-1.2TRG1': (0.45, 0.30, 0.18), 'CY15B104QN-50SXI': (16.0, 14.0, 12.0),
       '3034': (1.10, 0.85, 0.65), 'BAT54WS-7-F': (0.15, 0.08, 0.04), 'SKHHLQA010': (0.45, 0.33, 0.25), 'USB4105-GF-A': (0.85, 0.65, 0.50), 'SMAJ22A': (0.35, 0.22, 0.13),
       'DMT6009LSS-13': (0.90, 0.65, 0.45), '2N7002-7-F': (0.15, 0.07, 0.03), '132289': (4.50, 3.80, 3.10), 'ESD9B5.0ST5G': (0.20, 0.10, 0.06),
       'SN74LVC1G17DBVR': (0.25, 0.12, 0.07), 'SN74LVC1G07DBVR': (0.25, 0.12, 0.07), 'SN74LVC1G08DBVR': (0.25, 0.12, 0.07), 'SN74LVC1G125DBVR': (0.25, 0.12, 0.07),
       'SN74LV1T34DBVR': (0.35, 0.20, 0.12), 'ABM8-25.000MHZ-B2-T': (0.55, 0.42, 0.33), 'W25Q16JVSSIQ': (0.60, 0.45, 0.35), 'BLM18AG601SN1D': (0.08, 0.04, 0.02),
       '692122030100': (1.90, 1.50, 1.20), 'TPS2065CDBVR': (0.75, 0.55, 0.38), 'USBLC6-2SC6': (0.40, 0.25, 0.15), 'TPD4E05U06DQAR': (0.50, 0.30, 0.18), 'FT230XS-R': (2.60, 2.30, 2.00),
       'TRS3232EIPWR': (1.20, 0.85, 0.60), '54602-908LF': (0.90, 0.70, 0.50), 'DM3AT-SF-PEJM5': (2.90, 2.30, 1.80), 'BSS138LT1G': (0.20, 0.10, 0.05), 'AO3401A': (0.35, 0.20, 0.12),
       'TMP1075DR': (0.70, 0.50, 0.35), 'TCA9534PWR': (1.10, 0.80, 0.55), 'KPA-3010SGC': (0.45, 0.30, 0.22), 'KPA-3010EC': (0.45, 0.30, 0.22), 'KPA-3010YC': (0.45, 0.30, 0.22),
       'MAX6369KA+T': (3.40, 2.90, 2.40), '47053-1000': (0.35, 0.25, 0.18), 'EEE-FK1V101XP': (0.45, 0.30, 0.20), 'EEE-FK1A151P': (0.40, 0.27, 0.18), '61300611121': (0.20, 0.14, 0.10)}
def est(p):
    if p.mpn in EST: return EST[p.mpn]
    if p.sym == 'SORX:R': return (0.012, 0.006, 0.003)
    if p.sym == 'SORX:C':
        v = p.value
        if 'uF/35V' in v: return (0.55, 0.35, 0.22) if v.startswith('10') else (0.25, 0.15, 0.09)
        if v == '22uF': return (0.25, 0.15, 0.09)
        if v == '10uF': return (0.12, 0.06, 0.035)
        if v in ('4.7uF', '1uF'): return (0.07, 0.035, 0.018)
        if '50V' in v: return (0.04, 0.02, 0.01)
        return (0.025, 0.011, 0.006)
    raise KeyError(p.mpn)
# items that are not on the PCB BOM but belong to a built unit
EXTRA = [  # (description, mfr, mpn, qty, p10, p100, p1k, note)
    ('PCB, 4-layer 172 x 118 mm, 1.6 mm, ENIG, impedance controlled', 'PCB fab', 'SORX-CARRIER-A', 1, 38.0, 14.0, 8.0, 'estimate; quote with hw/fab/'),
    ('Assembly, double-sided SMT + through-hole connectors', 'EMS', '-', 1, 45.0, 18.0, 9.0, 'estimate'),
    ('Fan 40 x 40 x 10 mm, 5 V, 4-pin PWM', 'Noctua', 'NF-A4x10 5V PWM', 1, 15.0, 13.0, 11.0, 'estimate; any 5 V 4-pin PWM 40 mm fan >= 8 m3/h'),
    ('Heatsink 40 x 28 x 10 mm class with thermal tape, on the SoC', '-', 'select at build', 1, 3.5, 2.5, 1.8, 'estimate; Toradex Verdin heatsink needs the optional standoffs'),
    ('Coin cell CR2032', 'Panasonic', 'CR2032', 1, 0.45, 0.35, 0.28, 'estimate'),
    ('Enclosure base + lid + LED window, printed (PETG/ABS)', 'print shop', 'mech/*.stl', 1, 28.0, 20.0, 14.0, 'estimate; injection moulding would replace this at volume'),
    ('Hardware: 4x M3x40 screw, 8x M3 heat-set insert', '-', '-', 1, 1.2, 0.9, 0.6, 'estimate'),
]
def main():
    groups = collections.OrderedDict()
    for p in DESIGN.parts.values():
        if not p.inbom or not (p.fp or p.sym == 'SORX:SOM'): continue
        groups.setdefault((p.mpn, p.fp, p.dnp), []).append(p)
    def refkey(r): m = re.match(r'([A-Z#]+)(\d+)', r); return (m.group(1), int(m.group(2)))
    rows = []; tot = {10: 0.0, 100: 0.0, 1000: 0.0}; stock_rows = []
    for i, ((mpn, fp, dnp), ps) in enumerate(groups.items(), 1):
        p = ps[0]; vals = list(collections.OrderedDict.fromkeys(q.value for q in ps)); value = vals[0] if len(vals) == 1 else ' / '.join(vals)
        refs = sorted((q.ref for q in ps), key=refkey); n = NOTES.get(mpn)
        if n: p10, p100, p1k = n['p10'], n['p100'], n['p1k']; basis = 'distributor listing %s' % CHECKED
        else: p10, p100, p1k = est(p); basis = 'estimate'
        qty = 0 if dnp else len(ps)
        for k, v in ((10, p10), (100, p100), (1000, p1k)): tot[k] += qty * v
        mouser = (n['mouser'] if n else MOUSER_PREFIX.get(p.mfr, '') + mpn) if mpn else ''
        dk = n['dk'] if n else ''
        single = p.alt.lower().startswith('none')
        rows.append([i, len(ps), ' '.join(refs), value, p.desc, p.mfr, mpn, dk or 'look up by MPN', mouser, 'verified %s' % CHECKED if n else 'Mouser PN built from prefix + MPN, not verified',
                     p.alt, 'yes' if single else 'no', 'DNP' if dnp else '', fp.split(':')[-1], '%.4f' % p10, '%.4f' % p100, '%.4f' % p1k, '%.2f' % (qty * p10), '%.2f' % (qty * p100), '%.2f' % (qty * p1k), basis])
        if n or p.ref[0] in 'UJYLQM' or p.sym in ('SORX:CP',):
            need = qty * 1000
            flag = ''
            if n:
                sn = n['stock_n']
                flag = 'UNKNOWN' if sn is None else ('UNDER 1k' if sn < 1000 else ('below 1k-unit build need (%d)' % need if sn < need else 'ok'))
            stock_rows.append([mpn, p.mfr, qty, n['stock'] if n else 'not checked', n['lead'] if n else '', flag if n else 'NOT CHECKED', n['src'] if n else '', CHECKED if n else ''])
    hdr = ['Item', 'Qty', 'References', 'Value', 'Description', 'Manufacturer', 'MPN', 'Digi-Key PN', 'Mouser PN', 'Distributor PN status', 'Second source / alternates', 'Single-sourced',
           'DNP', 'Footprint', 'Unit $ @10', 'Unit $ @100', 'Unit $ @1k', 'Ext $ @10', 'Ext $ @100', 'Ext $ @1k', 'Price basis']
    with open(os.path.join(BOM, 'sorx-bom.csv'), 'w', newline='') as f:
        w = csv.writer(f); w.writerow(hdr); w.writerows(rows)
    with open(os.path.join(BOM, 'stock-check.csv'), 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['MPN', 'Manufacturer', 'Qty per board', 'Stock seen', 'Factory lead time', 'Flag', 'Source', 'Date checked']); w.writerows(stock_rows)
    som = {10: NOTES['0063']['p10'], 100: NOTES['0063']['p100'], 1000: NOTES['0063']['p1k']}
    ex = {10: sum(e[3] * e[4] for e in EXTRA), 100: sum(e[3] * e[5] for e in EXTRA), 1000: sum(e[3] * e[6] for e in EXTRA)}
    with open(os.path.join(BOM, 'cost-rollup.csv'), 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['Line', 'Per unit @10', 'Per unit @100', 'Per unit @1k', 'Basis'])
        w.writerow(['Carrier BOM without SoM', '%.2f' % (tot[10] - som[10]), '%.2f' % (tot[100] - som[100]), '%.2f' % (tot[1000] - som[1000]), 'sum of sorx-bom.csv minus M1'])
        w.writerow(['SoM (Toradex 0063)', '%.2f' % som[10], '%.2f' % som[100], '%.2f' % som[1000], NOTES['0063']['src']])
        for e in EXTRA: w.writerow([e[0] + ' (' + e[2] + ')', '%.2f' % (e[3] * e[4]), '%.2f' % (e[3] * e[5]), '%.2f' % (e[3] * e[6]), e[7]])
        w.writerow(['TOTAL per unit', '%.2f' % (tot[10] + ex[10]), '%.2f' % (tot[100] + ex[100]), '%.2f' % (tot[1000] + ex[1000]), 'excludes NRE, test fixture, freight, tariffs'])
    print('BOM lines', len(rows), '| placed parts', sum(r[1] for r in rows if not r[12]), '| carrier BOM $ @10/100/1k: %.2f %.2f %.2f' % tuple(tot[k] - som[k] for k in (10, 100, 1000)),
          '| total $: %.2f %.2f %.2f' % tuple(tot[k] + ex[k] for k in (10, 100, 1000)))
    print('single-sourced:', [r[6] for r in rows if r[11] == 'yes'])
    print('stock flags:', [(r[0], r[5]) for r in stock_rows if r[5] not in ('ok', 'NOT CHECKED')], '| not checked:', sum(1 for r in stock_rows if r[5] == 'NOT CHECKED'))
if __name__ == '__main__':
    main()

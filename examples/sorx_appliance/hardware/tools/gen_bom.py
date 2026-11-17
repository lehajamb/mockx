#!/usr/bin/env python3
"""BOM, cost roll-up and stock-check tables from design.py + bom/stock_notes.json."""
import csv, json, os, re, collections
from design import DESIGN
HERE = os.path.dirname(os.path.abspath(__file__)); BOM = os.path.join(HERE, '..', 'bom')
with open(os.path.join(BOM, 'stock_notes.json'), encoding='utf-8') as notes_file:
    NOTES = json.load(notes_file)
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
        if v in ('1uF/50V', '10uF/50V'): return (None, None, None)
        if '50V' in v: return (0.04, 0.02, 0.01)
        return (0.025, 0.011, 0.006)
    return (None, None, None)  # no fabricated price for a new or unquoted component
# items that are not on the PCB BOM but belong to a built unit
EXTRA = [  # (description, mfr, mpn, qty, p10, p100, p1k, note)
    ('PCB, 6-layer 172 x 118 mm, 1.6 mm candidate, ENIG, impedance controlled', 'PCB fab', 'SORX-CARRIER-CODEX-B', 1, None, None, None, 'NOT QUOTED; fabricator stackup approval required'),
    ('Assembly, double-sided SMT + through-hole connectors', 'EMS', '-', 1, 45.0, 18.0, 9.0, 'estimate'),
    ('Fan 40 x 40 x 10 mm, 5 V, 4-pin PWM', 'Noctua', 'NF-A4x10 5V PWM', 1, 15.0, 13.0, 11.0, 'estimate; any 5 V 4-pin PWM 40 mm fan >= 8 m3/h'),
    ('Coin cell CR2032', 'Panasonic', 'CR2032', 1, 0.45, 0.35, 0.28, 'estimate'),
    ('Enclosure base + lid + LED window, printed (PETG/ABS)', 'print shop', 'mech/*.stl', 1, 28.0, 20.0, 14.0, 'estimate; injection moulding would replace this at volume'),
    ('Hardware: 4x M3x40 screw, 8x M3 heat-set insert', '-', '-', 1, 1.2, 0.9, 0.6, 'estimate'),
]
def include_part(part):
    """Keep procurement-only mechanical items as well as placed electronics."""
    return part.inbom


def money(value, places=4):
    return '' if value is None else ('%.*f' % (places, value))


def main():
    groups = collections.OrderedDict()
    for part in DESIGN.parts.values():
        if include_part(part):
            # Missing MPNs cannot collapse different unspecified mechanical items.
            key = (part.mpn or part.value, part.fp, part.dnp)
            groups.setdefault(key, []).append(part)
    def refkey(ref):
        match = re.match(r'([A-Z#]+)(\d+)', ref)
        return match.group(1), int(match.group(2))
    rows, stock_rows, unquoted = [], [], []
    totals = {10: 0.0, 100: 0.0, 1000: 0.0}
    for index, (_, parts) in enumerate(groups.items(), 1):
        part = parts[0]
        mpn, fp, dnp = part.mpn, part.fp, part.dnp
        refs = sorted((q.ref for q in parts), key=refkey)
        values = list(dict.fromkeys(q.value for q in parts))
        note = NOTES.get(mpn)
        if note:
            prices = note['p10'], note['p100'], note['p1k']
            basis = 'historical supplied distributor listing %s; not rechecked' % CHECKED
        else:
            prices = est(part)
            basis = 'inherited estimate; not a quote' if all(v is not None for v in prices) else 'NOT QUOTED'
        qty = 0 if dnp else len(parts)
        extended = [None if price is None else qty * price for price in prices]
        for k, price in zip((10, 100, 1000), extended):
            if price is not None:
                totals[k] += price
        if qty and any(price is None for price in prices):
            unquoted.append(' '.join(refs))
        rows.append([index, len(parts), ' '.join(refs), ' / '.join(values), part.desc, part.mfr, mpn,
                     note.get('dk', '') if note else '', note.get('mouser', '') if note else '',
                     'historical supplied evidence %s; not rechecked' % CHECKED if note else 'NOT CHECKED',
                     part.alt, 'yes' if part.alt.lower().startswith('none') else 'not qualified',
                     'DNP' if dnp else '', fp.split(':')[-1],
                     *[money(v) for v in prices], *[money(v, 2) for v in extended], basis])
        if note or part.ref[0] in 'UJYLQMH' or part.sym in ('SORX:CP', 'SORX:MECHANICAL_ITEM'):
            stock_rows.append([mpn, part.mfr, qty, note['stock'] if note else 'not checked',
                               note['lead'] if note else '', 'HISTORICAL; RECHECK' if note else 'NOT CHECKED',
                               note['src'] if note else '', CHECKED if note else ''])
    hdr = ['Item', 'Qty', 'References', 'Value', 'Description', 'Manufacturer', 'MPN', 'Digi-Key PN', 'Mouser PN',
           'Distributor PN status', 'Second source / alternates', 'Single-sourced', 'DNP', 'Footprint',
           'Unit $ @10', 'Unit $ @100', 'Unit $ @1k', 'Ext $ @10', 'Ext $ @100', 'Ext $ @1k', 'Price basis']
    with open(os.path.join(BOM, 'sorx-bom.csv'), 'w', newline='', encoding='utf-8') as output:
        writer = csv.writer(output); writer.writerow(hdr); writer.writerows(rows)
    with open(os.path.join(BOM, 'stock-check.csv'), 'w', newline='', encoding='utf-8') as output:
        writer = csv.writer(output)
        writer.writerow(['MPN', 'Manufacturer', 'Qty per board', 'Stock seen', 'Factory lead time', 'Flag', 'Source', 'Date checked'])
        writer.writerows(stock_rows)
    with open(os.path.join(BOM, 'cost-rollup.csv'), 'w', newline='', encoding='utf-8') as output:
        writer = csv.writer(output)
        writer.writerow(['Line', 'Per unit @10', 'Per unit @100', 'Per unit @1k', 'Basis'])
        writer.writerow(['BOM known-price subtotal including SoM and specified mounting items',
                         *[money(totals[k], 2) for k in (10, 100, 1000)],
                         'INCOMPLETE: unquoted references ' + ', '.join(unquoted)])
        for entry in EXTRA:
            prices = [None if p is None else entry[3] * p for p in entry[4:7]]
            writer.writerow([entry[0] + ' (' + entry[2] + ')', *[money(v, 2) for v in prices], entry[7]])
        writer.writerow(['COMPLETE UNIT TOTAL', '', '', '', 'UNAVAILABLE until all PCB, mechanical and component quotes are complete'])
    print('BOM lines', len(rows), '| unquoted references:', ', '.join(unquoted))
    print('Stock rows', len(stock_rows), '| independently rechecked in this run: 0')


if __name__ == '__main__':
    main()

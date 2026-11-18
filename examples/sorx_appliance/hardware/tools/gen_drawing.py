#!/usr/bin/env python3
"""Dimensioned board outline drawing (mech/board-outline-dimensioned.pdf/.png) from hw/build/mech_parts.json."""
import json, os
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle
HERE = os.path.dirname(os.path.abspath(__file__)); MECH = os.path.join(HERE, '..', 'mech')
P = {p['ref']: p for p in json.load(open(os.path.join(HERE, '..', 'hw', 'build', 'mech_parts.json')))}
W, H = 172.0, 118.0; HOLES = [(4.5, 4.5), (167.5, 4.5), (4.5, 113.5), (167.5, 113.5)]
fig, ax = plt.subplots(figsize=(16.5, 11.7))
ax.add_patch(Rectangle((0, 0), W, H, fill=False, lw=1.8))
def dim(x1, y1, x2, y2, text, off=0, rot=0):
    ax.annotate('', xy=(x1, y1), xytext=(x2, y2), arrowprops=dict(arrowstyle='<->', lw=0.8))
    ax.text((x1 + x2) / 2 + (off if rot else 0), (y1 + y2) / 2 + (0 if rot else off), text, ha='center', va='center', fontsize=8, rotation=rot, bbox=dict(fc='white', ec='none', pad=1))
dim(0, -9, W, -9, '172.00'); dim(-9, 0, -9, H, '118.00', rot=90)
for (x, y) in HOLES:
    ax.add_patch(Circle((x, y), 1.6, fill=False, lw=1.0)); ax.plot([x - 2.6, x + 2.6], [y, y], 'k-', lw=0.4); ax.plot([x, x], [y - 2.6, y + 2.6], 'k-', lw=0.4)
ax.text(4.5, 9.5, '4x dia 3.20 (M3)\nat 4.50 from edges', fontsize=7, ha='left', va='top')
dim(4.5, -4, 167.5, -4, '163.00'); dim(-4, 4.5, -4, 113.5, '109.00', rot=90)
names = {'J3': 'USB-C PWR', 'J6': 'OE', 'J4': 'MD-IN', 'J7': 'MGMT', 'J5': 'PPS SMA', 'J8': 'USB 1', 'J9': 'USB 2', 'J11': 'CONSOLE', 'J10': 'USB-C DEBUG', 'SW1': 'RESET', 'SW2': 'RECOVERY',
         'J12': 'microSD', 'J1': 'SoM socket J1', 'J13': 'FAN', 'BT1': 'CR2032', 'J2': 'AUX (DNP)'}
for r, n in names.items():
    b = P[r]['box']; ax.add_patch(Rectangle((b[0], b[1]), b[2] - b[0], b[3] - b[1], fill=True, fc='#cfd8dc', ec='#455a64', lw=0.8))
    cx = (b[0] + b[2]) / 2; cy = (b[1] + b[3]) / 2
    ax.text(cx, cy, n, ha='center', va='center', fontsize=7, rotation=90 if (b[3] - b[1]) > 1.6 * (b[2] - b[0]) else 0)
    if b[1] < 8 and r != 'J13':
        ax.plot([cx, cx], [-13, 0], 'k-', lw=0.3); ax.text(cx, -14.5, '%.2f' % cx, ha='center', va='bottom', fontsize=7, rotation=90)
    if b[3] > H - 6:
        ax.plot([cx, cx], [H, H + 13], 'k-', lw=0.3); ax.text(cx, H + 14.5, '%.2f' % cx, ha='center', va='top', fontsize=7, rotation=90)
x1 = P['D4']['box'][0]; x2 = P['D10']['box'][2]
ax.add_patch(Rectangle((x1, H - 2.4), x2 - x1, 2.2, fc='#ffe082', ec='#f9a825')); ax.text((x1 + x2) / 2, H - 5, '7 LEDs, 6.00 pitch, first at x = 14.00', ha='center', fontsize=7)
xs, ys = P['J1']['x'], P['J1']['y']
ax.add_patch(Rectangle((xs - 34.8, ys - 8.5), 69.6, 35.0, fill=False, ec='#1565c0', lw=1.0, ls='--')); ax.text(xs, ys + 22, 'Verdin module outline 69.6 x 35.0 (nominal)', color='#1565c0', ha='center', fontsize=7)
ax.text(W + 6, 20, 'SORX carrier rev A\nBoard outline, top view\nUnits: mm. Origin: rear-left corner.\nRear edge at top (y = 0), front edge at bottom.\nThickness 1.6 +/-0.16, 4 layers.\nConnector centre positions are along x.\nRJ45 jacks and USB-A overhang the edge by 1.0.\nNo components under the module (5.2 mm socket).\nBottom side: I210 block under the OE jack, max 1.2 high.',
        fontsize=8, va='top')
ax.set_xlim(-22, W + 70); ax.set_ylim(H + 26, -24); ax.set_aspect('equal'); ax.axis('off')
fig.savefig(os.path.join(MECH, 'board-outline-dimensioned.pdf')); fig.savefig(os.path.join(MECH, 'board-outline-dimensioned.png'), dpi=80)
print('drawing written')

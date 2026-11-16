#!/usr/bin/env python3
"""Closed-form (IPC-2141 style) impedance estimate for the outer-layer pairs of the SORX stackup."""
import math
H, T, ER = 0.10, 0.035, 4.1   # prepreg height L1-L2 / L3-L4 (mm), copper thickness (mm), dielectric constant
def z0(w): return 87.0 / math.sqrt(ER + 1.41) * math.log(5.98 * H / (0.8 * w + T))
def zdiff(w, s): return 2 * z0(w) * (1 - 0.48 * math.exp(-0.96 * s / H))
if __name__ == '__main__':
    print('| Class | Use | Width mm | Gap mm | Z0 single ohm | Zdiff ohm | Target |\n|---|---|---:|---:|---:|---:|---|')
    for name, use, w, s, tgt in (('DP100', 'Ethernet MDI', 0.127, 0.18, '100 ohm +/-10%'), ('DP90', 'USB 2.0, USB 3.0, PCIe', 0.155, 0.18, '90 ohm +/-10% (PCIe 85 ohm +/-15%)')):
        print('| %s | %s | %.3f | %.2f | %.1f | %.1f | %s |' % (name, use, w, s, z0(w), zdiff(w, s), tgt))
    print('| Default | single-ended signals | 0.150 | - | %.1f | - | 50 ohm nominal |' % z0(0.15))

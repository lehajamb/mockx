#!/usr/bin/env python3
"""Differential-pair report: length per net, intra-pair skew, via count, layers."""
import sys, collections, math, pcbnew, proj
def report(path):
    b = pcbnew.LoadBoard(path)
    L = collections.defaultdict(float); V = collections.Counter(); LY = collections.defaultdict(set)
    for t in b.GetTracks():
        n = t.GetNetname()
        if proj.classify(n) not in ('DP100', 'DP90'): continue
        if t.GetClass() == 'PCB_VIA': V[n] += 1
        else:
            L[n] += pcbnew.ToMM(t.GetLength()); LY[n].add(b.GetLayerName(t.GetLayer()))
    rows = []
    for n in sorted(L):
        if n.endswith('_P'):
            m = n[:-2] + '_N'
            rows.append((n[:-2], proj.classify(n), L[n], L[m], abs(L[n] - L[m]), V[n], V[m], ','.join(sorted(LY[n] | LY[m]))))
    for a, b_ in (('FT_DP', 'FT_DM'),):
        if a in L: rows.append(('FT_D', 'DP90', L[a], L[b_], abs(L[a] - L[b_]), V[a], V[b_], ','.join(sorted(LY[a] | LY[b_]))))
    return rows
if __name__ == '__main__':
    print('%-12s %-6s %7s %7s %6s %s' % ('pair', 'class', 'P mm', 'N mm', 'skew', 'vias P/N layers'))
    for r in report(sys.argv[1]): print('%-12s %-6s %7.2f %7.2f %6.2f %d/%d %s' % r)

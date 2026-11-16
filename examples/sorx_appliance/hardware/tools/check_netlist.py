#!/usr/bin/env python3
"""Export the netlist from the generated schematic with kicad-cli and compare it with design.py."""
import subprocess, json, os, sys
from sx import parse, find, first
HW = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'hw')
out = '/tmp/sorx.net'
r = subprocess.run(['kicad-cli', 'sch', 'export', 'netlist', '--format', 'kicadsexpr', '-o', out, os.path.join(HW, 'sorx.kicad_sch')], capture_output=True, text=True)
if r.returncode: print(r.stdout[-800:], r.stderr[-800:]); sys.exit(1)
root = parse(open(out).read())
comps = {first(c, 'ref')[1] for c in find(first(root, 'components'), 'comp')}
sch = {}
for n in find(first(root, 'nets'), 'net'):
    name = first(n, 'name')[1].lstrip('/')
    nodes = {(first(x, 'ref')[1], first(x, 'pin')[1]) for x in find(n, 'node')}
    sch[name] = nodes
model = {k: {tuple(x) for x in v} for k, v in json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'netmap.json'))).items()}
bad = 0
for k, v in model.items():
    if sch.get(k) != v:
        bad += 1
        if bad <= 12: print('MISMATCH', k, 'model-only', sorted(v - sch.get(k, set()))[:6], 'sch-only', sorted(sch.get(k, set()) - v)[:6])
extra = [k for k in sch if k not in model and len(sch[k]) > 1]
unconn = [k for k in sch if k not in model and len(sch[k]) == 1]
print('components in schematic netlist:', len(comps)); print('model nets:', len(model), 'matching:', len(model) - bad, 'mismatching:', bad)
print('extra multi-node nets in schematic:', extra[:10]); print('single-node (no-connect) nets in schematic:', len(unconn))
sys.exit(1 if bad or extra else 0)

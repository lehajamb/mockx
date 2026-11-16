#!/usr/bin/env python3
"""Fabrication, assembly and mechanical exports from hw/sorx.kicad_pcb and hw/sorx.kicad_sch (KiCad 7 CLI)."""
import os, subprocess, shutil, sys
HERE = os.path.dirname(os.path.abspath(__file__)); HW = os.path.join(HERE, '..', 'hw'); MECH = os.path.join(HERE, '..', 'mech'); FAB = os.path.join(HW, 'fab')
PCB = os.path.join(HW, 'sorx.kicad_pcb'); SCH = os.path.join(HW, 'sorx.kicad_sch')
def run(*a):
    r = subprocess.run(['kicad-cli'] + list(a), capture_output=True, text=True)
    print(('ok   ' if r.returncode == 0 else 'FAIL ') + ' '.join(a[:3]), (r.stdout + r.stderr).strip().splitlines()[-1][:110] if (r.stdout + r.stderr).strip() else '')
shutil.rmtree(FAB, ignore_errors=True)
for d in ('gerbers', 'drill', 'assembly'): os.makedirs(os.path.join(FAB, d))
run('pcb', 'export', 'gerbers', '-o', os.path.join(FAB, 'gerbers') + '/', '--layers', 'F.Cu,In1.Cu,In2.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts', '--subtract-soldermask', PCB)
run('pcb', 'export', 'drill', '-o', os.path.join(FAB, 'drill') + '/', '--format', 'excellon', '--excellon-separate-th', '--excellon-units', 'mm', '--generate-map', '--map-format', 'pdf', PCB)
run('pcb', 'export', 'pos', '-o', os.path.join(FAB, 'assembly', 'sorx-pos.csv'), '--format', 'csv', '--units', 'mm', '--side', 'both', PCB)
run('pcb', 'export', 'pdf', '-o', os.path.join(FAB, 'assembly', 'sorx-assembly-top.pdf'), '--layers', 'F.Fab,F.SilkS,Edge.Cuts', '--black-and-white', PCB)
run('pcb', 'export', 'pdf', '-o', os.path.join(FAB, 'assembly', 'sorx-assembly-bottom.pdf'), '--layers', 'B.Fab,B.SilkS,Edge.Cuts', '--mirror', '--black-and-white', PCB)
for l in ('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'):
    run('pcb', 'export', 'pdf', '-o', os.path.join(FAB, 'sorx-copper-%s.pdf' % l.replace('.', '_')), '--layers', l + ',Edge.Cuts', PCB)
run('pcb', 'export', 'dxf', '-o', os.path.join(MECH, 'board-outline.dxf'), '--layers', 'Edge.Cuts', '--output-units', 'mm', PCB)
run('pcb', 'export', 'step', '-o', os.path.join(MECH, 'sorx-board.step'), '--force', '--subst-models', PCB)
run('sch', 'export', 'pdf', '-o', os.path.join(HW, 'sorx-schematic.pdf'), SCH)
run('sch', 'export', 'netlist', '-o', os.path.join(HW, 'sorx.net'), SCH)
shutil.make_archive(os.path.join(HW, 'sorx-fab-revA'), 'zip', FAB)
print(sorted(os.listdir(os.path.join(FAB, 'gerbers'))))

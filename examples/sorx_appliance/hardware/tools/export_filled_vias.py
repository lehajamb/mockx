#!/usr/bin/env python3
"""Identify the three explicit debug-power via-in-pad requirements on the final board.

This is a reviewed-hole manifest in native KiCad coordinates, not a replacement
for the fabricator's drill/CAM files or process acceptance.
"""
import argparse,hashlib,json
from pathlib import Path

def collect(board):
    import pcbnew
    selected=[]
    for reference,pins in [('FB2',['1']),('J10',['A4','B9']),('J10',['A9','B4'])]:
        fp=board.FindFootprintByReference(reference)
        if fp is None:raise ValueError('Missing '+reference)
        pads=[p for p in fp.Pads()if p.GetNumber()in pins]
        if len(pads)!=len(pins)or any(p.GetNetname()!='VBUS_DBG_RAW'for p in pads):raise ValueError('Unexpected power pad mapping')
        xy=pads[0].GetPosition()
        if any(p.GetPosition()!=xy for p in pads):raise ValueError('Stacked USB-C pad positions disagree')
        vias=[v for v in board.GetTracks()if v.GetClass()=='PCB_VIA'and v.GetPosition()==xy]
        if len(vias)!=1:raise ValueError('Expected exactly one selected via at '+reference+'.'+','.join(pins))
        v=vias[0]
        if v.GetNetname()!='VBUS_DBG_RAW'or v.GetWidth()!=pcbnew.FromMM(.6)or v.GetDrillValue()!=pcbnew.FromMM(.3):raise ValueError('Unexpected selected via identity/size')
        selected.append(dict(via_uuid=v.m_Uuid.AsString(),reference=reference,pad_numbers=pins,pad_uuids=[p.m_Uuid.AsString()for p in pads],net=v.GetNetname(),x_mm=xy.x/1e6,y_mm=xy.y/1e6,nominal_copper_diameter_mm=.6,nominal_drill_mm=.3,process='IPC-4761 Type VII filled and copper capped; planar solderable pad surface; supplier acceptance required'))
    return selected

def main():
    import pcbnew
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('board',type=Path);ap.add_argument('output',type=Path);a=ap.parse_args();b=pcbnew.LoadBoard(str(a.board));data=dict(board=str(a.board),board_sha256=hashlib.sha256(a.board.read_bytes()).hexdigest(),kicad=pcbnew.GetBuildVersion(),coordinates='Native KiCad board coordinates in millimetres; this JSON is not an NC drill file',selected_power_pad_vias=collect(b),thermal_vias='U2/U13 thermal lands have separate filled/capped assembly requirements; this manifest identifies only the three debug-power additions.',fabrication_ready=False);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(data,indent=2)+'\n');print('Identified',len(data['selected_power_pad_vias']),'required filled/capped power-pad vias')
if __name__=='__main__':main()

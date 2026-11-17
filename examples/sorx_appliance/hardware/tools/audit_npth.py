#!/usr/bin/env python3
"""Audit component copper pads against actual NPTH drills on both faces.

Own-footprint pads are listed separately: a manufacturer's intentional annular
mounting land must not be confused with another component placed over the hole.
"""
import argparse,json,hashlib
from pathlib import Path


def audit(path):
    import pcbnew
    from shapely.geometry import Point,LineString
    from shapely.affinity import rotate
    from shapely.ops import unary_union
    from high_speed_audit import polygons
    board=pcbnew.LoadBoard(str(path));holes=[];pads=[]
    for fp in board.GetFootprints():
        for p in fp.Pads():
            xy=(p.GetPosition().x/1e6,p.GetPosition().y/1e6)
            if p.GetAttribute()==pcbnew.PAD_ATTRIB_NPTH:
                dx,dy=p.GetDrillSize().x/1e6,p.GetDrillSize().y/1e6
                if dx<=0 or dy<=0:continue
                if abs(dx-dy)<1e-9:shape=Point(xy).buffer(dx/2,64)
                elif dx>dy:shape=LineString([(xy[0]-(dx-dy)/2,xy[1]),(xy[0]+(dx-dy)/2,xy[1])]).buffer(dy/2,64)
                else:shape=LineString([(xy[0],xy[1]-(dy-dx)/2),(xy[0],xy[1]+(dy-dx)/2)]).buffer(dx/2,64)
                shape=rotate(shape,p.GetOrientationDegrees(),origin=xy)
                holes.append(dict(ref=fp.GetReference(),pad=p.GetNumber(),uuid=p.m_Uuid.AsString(),xy=xy,drill_mm=[dx,dy],shape=shape))
            elif any(p.IsOnLayer(i) for i in range(32) if board.IsLayerEnabled(i)):
                pads.append(dict(ref=fp.GetReference(),pad=p.GetNumber(),uuid=p.m_Uuid.AsString(),xy=xy,net=p.GetNetname(),
                    layers=[board.GetLayerName(i) for i in range(32) if board.IsLayerEnabled(i) and p.IsOnLayer(i)],
                    shape=unary_union(polygons(p.GetEffectivePolygon()))))
    collisions=[];own=[]
    for hole in holes:
        for pad in pads:
            distance=hole['shape'].distance(pad['shape'])
            if distance>=.25-1e-6:continue
            entry=dict(hole={k:v for k,v in hole.items() if k!='shape'},component_pad={k:v for k,v in pad.items() if k!='shape'},
                copper_to_drill_mm=distance,physical_overlap_mm2=hole['shape'].intersection(pad['shape']).area)
            (own if hole['ref']==pad['ref'] else collisions).append(entry)
    return dict(pcb=str(path),pcb_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),kicad=pcbnew.GetBuildVersion(),
        npth_count=len(holes),copper_pad_count=len(pads),clearance_review_mm=.25,other_component_findings=collisions,own_footprint_review=own,
        physical_overlap_count=sum(f['physical_overlap_mm2']>1e-6 for f in collisions))


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('pcb',type=Path);ap.add_argument('output',type=Path);a=ap.parse_args()
    result=audit(a.pcb);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('npth_count','copper_pad_count','physical_overlap_count')}))
    for f in result['other_component_findings']:
        print(f['hole']['ref'],f['component_pad']['ref']+'.'+f['component_pad']['pad'],f['component_pad']['layers'],round(f['copper_to_drill_mm'],4),round(f['physical_overlap_mm2'],4))
    return 1 if result['other_component_findings'] else 0


if __name__=='__main__':raise SystemExit(main())

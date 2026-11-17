#!/usr/bin/env python3
"""Finish audited control/decoupling islands, including explicit In2 transitions.

Requires an unchanged power-copper inventory relative to the audited power07
checkpoint. Current allocations are branch-specific; this never licenses a
narrow main-current path. Any new native non-open DRC finding rejects output.
"""
from pathlib import Path
import argparse,json,math,shutil,hashlib

ALLOCATIONS={
    "+1V8":(.15,.005,"isolated pullups/74LVC1G07 and local capacitor only"),
    "+4V5_PD":(.15,.001,"TP21 measurement-only branch"),
    "+3V3":(.25,.10,"isolated TRS3232/watchdog/translators branch"),
    "+5V":(.50,.10,"isolated4.7uF C3/C4 decoupling branches; transient qualification open"),
    "VBUS_IN":(.25,.036,"Q2 source/reference and R12 sense branch; excludes main input"),
    "+1V2_PHY":(.15,.30,"single short QFN supply pin4 escape; max route5mm"),
    "VBUS_DBG_RAW":(.15,.10,"USB-C fine-pitch escape to debug UART island"),
}


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    import pcbnew
    from shapely.geometry import Point,LineString
    import route_completion as rc
    from verification_core import parse_drc
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source',type=Path);ap.add_argument('output',type=Path);ap.add_argument('evidence',type=Path)
    ap.add_argument('--audit-source',type=Path,default=Path('runs/route-electrical-07-power.kicad_pcb'))
    a=ap.parse_args();a.source=a.source.resolve();a.output=a.output.resolve();a.evidence=a.evidence.resolve()
    if a.output.exists() or a.evidence.exists():raise ValueError('Use new output and evidence paths')
    b=pcbnew.LoadBoard(str(a.source));base=pcbnew.LoadBoard(str(a.audit_source.resolve()))
    def inventory(board):
        out={}
        for t in board.GetTracks():
            if t.GetNetname() not in ALLOCATIONS:continue
            out[t.m_Uuid.AsString()]=(t.GetNetname(),str(t.GetStart()),str(t.GetEnd()),t.GetWidth(),t.GetLayer(),t.GetClass())
        return out
    if inventory(b)!=inventory(base):raise ValueError('Power copper changed since island audit; re-audit branch loads first')
    a.evidence.mkdir(parents=True);a.output.parent.mkdir(parents=True,exist_ok=True)
    for s in ('.kicad_pro','.kicad_dru'):
        if a.source.with_suffix(s).is_file():shutil.copy2(a.source.with_suffix(s),a.output.with_suffix(s))
    if not a.output.with_suffix('.kicad_pro').exists():raise ValueError('Source project required')
    records=[]
    def drc(name):
        p=a.evidence/(name+'.txt');pcbnew.ZONE_FILLER(b).Fill(b.Zones())
        pcbnew.WriteDRCReport(b,str(p),pcbnew.EDA_UNITS_MILLIMETRES,True)
        return p,parse_drc(p.read_text())
    before_path,before=drc('before-drc');before_other={x['fingerprint'] for x in before['findings'] if x['section']!='unconnected pads'}
    for width in sorted({v[0] for v in ALLOCATIONS.values()}):
        rc.W_TRK=width;rc.VIA_D=.8 if width>=.5 else .6;rc.VIA_DR=.4 if width>=.5 else .3
        g=rc.Grid(b);g.load(b)
        report,_=drc('width-%s-before'%width)
        pairs=[e for e in rc.parse_unconnected(report) if e[0][2] in ALLOCATIONS and ALLOCATIONS[e[0][2]][0]==width]
        pairs.sort(key=lambda e:math.dist(e[0][0],e[1][0]))
        def via_clear(net,xy):
            ix,iy=g.cell(*xy)
            if not(0<=ix<g.nx and 0<=iy<g.ny) or g.H[iy,ix]:return False
            if min(xy[0]-g.x0,xy[1]-g.y0,g.x0+g.w-xy[0],g.y0+g.h-xy[1])<rc.EDGE+rc.VIA_D/2:return False
            point=Point(xy)
            for other,items in g.items.items():
                if other==net:continue
                for layers,shape,hole,trk in items:
                    if point.distance(shape)<rc.VIA_D/2+rc.CLR+rc.EPS+(rc.HOLE_EXTRA if hole else 0):return False
            return not any(disallow and shape.buffer(rc.VIA_D/2+rc.EPS).intersects(point) for shape,disallow in g.rule_areas)
        def anchors(xy,net,target):
            lines=[]
            for t in b.GetTracks():
                if t.GetClass()!='PCB_TRACK' or t.GetLayer()!=pcbnew.In2_Cu or t.GetNetname()!=net:continue
                line=LineString([(t.GetStart().x/1e6,t.GetStart().y/1e6),(t.GetEnd().x/1e6,t.GetEnd().y/1e6)])
                if line.distance(Point(xy))<.002:lines.append((line,t.m_Uuid.AsString()))
            points=[]
            for line,uuid in lines:
                steps=max(2,int(line.length/.25)+1)
                for i in range(steps+1):
                    pos=tuple(line.interpolate(i/steps,normalized=True).coords[0])
                    if via_clear(net,pos):points.append((pos,uuid))
            points.sort(key=lambda p:math.dist(p[0],target))
            return points[:24]
        for ends in pairs:
            (start,la,net,da),(end,lb,_,db)=ends
            w,current,why=ALLOCATIONS[net];entry=dict(net=net,width_mm=w,current_allocation_a=current,rationale=why,endpoints=ends,accepted_route=False);records.append(entry)
            if not la or not lb:entry['refusal']='Unknown layer';continue
            result=None;anchor=None
            if 'I' in la or 'I' in lb:
                if la=='I' and lb!='I':origin,target,tl=start,end,lb
                elif lb=='I' and la!='I':origin,target,tl=end,start,la
                else:entry['refusal']='Both endpoints are In2; needs a reviewed planar route';continue
                for point,uuid in anchors(origin,net,target):
                    trial=g.route(net,point,rc.ROUTING,target,tl,rip=False)
                    if trial is not None:result=trial;anchor=(point,uuid);break
            else:
                result=g.route(net,start,la,end,lb,rip=False) or g.route(net,end,lb,start,la,rip=False)
            if result is None:entry['refusal']='No clearance-safe path at branch width';print('NO PATH',net,flush=True);continue
            runs,vias=result;length=sum(math.dist(x,y) for _,ps in runs for x,y in zip(ps,ps[1:]))
            if net=='+1V2_PHY' and length>5:entry['refusal']='QFN escape would exceed5mm';continue
            if anchor:
                if not any(math.dist(p,anchor[0])<1e-6 for p in vias):vias.append(anchor[0])
                entry['in2_anchor']={'xy':anchor[0],'existing_track_uuid':anchor[1],'via_diameter_mm':rc.VIA_D,'drill_mm':rc.VIA_DR}
            rc.commit(b,g,net,b.GetNetsByName()[net],runs,vias,width)
            entry.update(accepted_route=True,runs=runs,vias=vias,length_mm=length)
            print('ROUTED',net,width,round(length,3),'mm','In2 anchor' if anchor else '',flush=True)
    _,after=drc('after-drc');new=[x for x in after['findings'] if x['section']!='unconnected pads' and x['fingerprint'] not in before_other]
    accepted=not new and after['counts']['unconnected pads']<=before['counts']['unconnected pads']
    pcbnew.SaveBoard(str(a.output),b)
    result=dict(status='ACCEPTED FOR FURTHER DEVELOPMENT' if accepted else 'REJECTED',fabrication_ready=False,
        source=str(a.source),source_sha256=sha(a.source),output=str(a.output),output_sha256=sha(a.output),
        audit_source_sha256=sha(a.audit_source),before=before['counts'],after=after['counts'],new_other_findings=new,routes=records,
        tool_sha256=sha(__file__),router_sha256=sha(rc.__file__),thermal_qualification=False)
    (a.evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('status','before','after')}),flush=True)
    return 0 if accepted else 1


if __name__=='__main__':raise SystemExit(main())

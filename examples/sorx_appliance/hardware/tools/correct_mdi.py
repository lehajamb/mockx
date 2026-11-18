#!/usr/bin/env python3
"""Create an isolated MDI correction candidate with explicit one-via escapes.

Uses the dedicated In3 signal layer in the independent six-layer stackup. The
result remains an engineering candidate until path/coupling/reference and native
DRC reviews pass. No source board or unrelated copper is overwritten.
"""
import argparse,hashlib,json,math,os,re,shutil,subprocess,sys
from pathlib import Path
import numpy as np
import pcbnew
from shapely.geometry import Point,LineString
import route_completion as router
import high_speed_audit as hs
from correct_high_speed import add_track,xy,pad_map,run_audit
NETS={'OE_MDI0_P','OE_MDI0_N','OE_MDI1_P','OE_MDI1_N','OE_MDI3_P','OE_MDI3_N'}


def filter_source(source,out):
    text=source.read_text(encoding='utf-8');codes={int(c) for c,n in re.findall(r'\(net (\d+) "([^"]+)"\)',text) if n in NETS}
    if len(codes)!=6:raise ValueError('Missing MDI nets')
    kept=[];removed=[]
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(('(segment ','(via ','(arc ')):
            net=re.search(r'\(net (\d+)\)',line)
            if net is None:raise ValueError('Unsupported multiline route primitive')
            if int(net.group(1)) in codes:removed.append(line);continue
        kept.append(line)
    out.write_text(''.join(kept),encoding='utf-8')
    if source.with_suffix('.kicad_pro').exists():shutil.copy2(source.with_suffix('.kicad_pro'),out.with_suffix('.kicad_pro'))
    return len(removed)


def via_free(grid,net):
    free=grid.H==0
    for layer in router.COPPER:
        own=np.zeros_like(grid.V[layer])
        for layers,shape,hole,track in grid.items.get(net,[]):
            if layer in layers:grid.stamp(own,shape,router.CLR+router.VIA_D/2+router.EPS+(router.HOLE_EXTRA if hole else 0))
        free=free & ((grid.V[layer]-own)==0)
    return free


def commit_runs(board,grid,net,result):
    runs,vias=result
    if vias:raise ValueError('Unexpected unplanned via')
    for layer,points in runs:
        for a,b in zip(points,points[1:]):add_track(board,net,router.LAY[layer],a,b,router.W_TRK)
        if len(points)>1:grid.add(net,[layer],LineString(points).buffer(router.W_TRK/2))
    return runs


def mdi_bundle(board,positive,negative,start,middle,end,anchors,positive_side):
    # Reserve the complete two-conductor envelope, then offset its centerline.
    # 0.155mm conductors at0.400mm centers require a0.555mm copper envelope.
    router.W_TRK=.555;router.EPS=.045
    bundle=router.Grid(board);bundle.load(board)
    for array in bundle.V.values():array.fill(60000)
    for layer in ('F','B'):bundle.T[layer].fill(60000)
    def available(point):
        cx,cy=bundle.cell(*point);choices=[]
        for iy in range(cy-20,cy+21):
            for ix in range(cx-20,cx+21):
                if bundle.T['C'][iy,ix]==0:
                    value=bundle.xy(ix,iy);choices.append(((value[0]-point[0])**2+(value[1]-point[1])**2,value))
        if not choices:raise ValueError('No bundle guide clearance near '+str(point))
        return min(choices)[1]
    guides=[available(p) for p in [start,*middle,end]]
    center=[]
    for a,b in zip(guides,guides[1:]):
        result=bundle.route('<'+positive+'-bundle>',a,'C',b,'C')
        if result is None or result[1] or any(layer!='C' for layer,_ in result[0]):raise ValueError('No protected pair corridor '+positive)
        for _,points in result[0]:
            for point in points:
                if not center or point!=center[-1]:center.append(point)
    line=LineString(center).simplify(.025)
    if not line.is_simple:raise ValueError('Bundle centerline self-intersects')
    rails={}
    router.W_TRK=.155
    for net,side in [(positive,positive_side),(negative,'right' if positive_side=='left' else 'left')]:
        rail=line.parallel_offset(.2,side,resolution=4,join_style=1)
        if rail.geom_type!='LineString' or not rail.is_simple:raise ValueError('Unsupported pair offset geometry')
        points=list(rail.coords)
        if Point(points[-1]).distance(Point(center[0]))<Point(points[0]).distance(Point(center[0])):points.reverse()
        for a,b in zip(points,points[1:]):add_track(board,net,pcbnew.In3_Cu,a,b,router.W_TRK)
        rails[net]=points
    grid=router.Grid(board);grid.load(board)
    for v in grid.V.values():v.fill(60000)
    for layer in ('F','B'):grid.T[layer].fill(60000)
    records=[]
    for net in [positive,negative]:
        records.append({'net':net,'paired_rail':rails[net],'bundle_guides':guides})
        for escape_index,(a,b) in enumerate([(anchors[net][0],rails[net][0]),(rails[net][-1],anchors[net][1])]):
            result=grid.route(net,a,'C',b,'C')
            if result is None or result[1] or any(layer!='C' for layer,_ in result[0]):raise ValueError('No pair endpoint escape '+net)
            tuned_runs=[]
            for layer,points in result[0]:
                tuned_runs.append((layer,points))
                for c,d in zip(points,points[1:]):add_track(board,net,pcbnew.In3_Cu,c,d,router.W_TRK)
                grid.add(net,['C'],LineString(points).buffer(router.W_TRK/2))
            records.append({'net':net,'escape':tuned_runs})
    return records


def worker(args):
    board=pcbnew.LoadBoard(str(args.source))
    if board.GetCopperLayerCount()!=6:raise ValueError('Requires explicit six-layer stackup')
    router.W_TRK=.127;router.CLR=.18;router.EPS=.025
    pads=pad_map(board);grid=router.Grid(board);grid.load(board)
    records=[];selected={}
    prefixes={'OE_MDI0_P':[(31.35,37.0625),(31.35,35.0584),(32.3391,34.0693)],'OE_MDI0_N':[(30.85,37.0625),(30.85,36.2338),(30.6761,35.9894)],'OE_MDI1_P':[(29.85,37.0625),(29.85,36.0377),(31.596,34.2917),(31.596,33.8)],
              'OE_MDI1_N':[(29.35,37.0625),(29.35,36.0966),(31.1063,34.3403),(31.1063,33.8)],
              'OE_MDI3_P':[(27.35,37.0625),(27.35,35.3659),(25.8492,33.8651)],
              'OE_MDI3_N':[(26.85,37.0625),(25.2814,35.4939),(25.2814,33.5)]}
    for net,points in prefixes.items():
        for a,b in zip(points,points[1:]):add_track(board,net,pcbnew.B_Cu,a,b,router.W_TRK)
        grid.add(net,['B'],LineString(points).buffer(router.W_TRK/2))
    # Staggered B-to-In3 transitions reverse pair ordering without a same-layer
    # crossover, while preserving the logical endpoint polarity.
    definitions=[('OE_MDI0_P','U10:58','J6:11',(33.0,35.5)),('OE_MDI0_N','U10:57','J6:10',(30.6761,35.9894)),('OE_MDI1_P','U10:55','J6:4',(31.8,30.8)),('OE_MDI1_N','U10:54','J6:5',(31.35,32.5)),
                 ('OE_MDI3_P','U10:50','J6:8',(25.85,31.0)),('OE_MDI3_N','U10:49','J6:9',(25.2,32.4))]
    for net,source,target,desired in definitions:
        free=via_free(grid,net)
        cx,cy=grid.cell(*desired);options=[]
        for iy in range(cy-24,cy+25):
            for ix in range(cx-24,cx+25):
                if free[iy,ix]:
                    point=grid.xy(ix,iy);options.append((math.dist(point,desired),point))
        options.sort()
        # Prevent route() from selecting additional transitions; selected via
        # candidates are checked against all copper and drill exclusions first.
        saved={k:v.copy() for k,v in grid.V.items()}
        for v in grid.V.values():v.fill(60000)
        chosen=(desired,([],[])) if net=='OE_MDI0_N' else None
        for _,point in ([] if chosen else options[:40]):
            result=grid.route(net,prefixes[net][-1],'B',point,'B')
            if result is not None and not result[1]:chosen=(point,result);break
        for k,v in saved.items():grid.V[k]=v
        if chosen is None:
            fallback=grid.route(net,prefixes[net][-1],'B',via_goal=True)
            if fallback is not None and len(fallback[1])==1:
                chosen=(fallback[1][0],(fallback[0],[]))
        if chosen is None:raise ValueError('No legal single-transition escape '+net)
        point,result=chosen
        runs=commit_runs(board,grid,net,result)
        via=pcbnew.PCB_VIA(board);via.SetPosition(pcbnew.VECTOR2I(round(point[0]*1e6),round(point[1]*1e6)))
        via.SetWidth(round(router.VIA_D*1e6));via.SetDrill(round(router.VIA_DR*1e6));via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu);via.SetNet(board.GetNetsByName()[net]);via.SetLocked(True);board.Add(via)
        grid.add(net,router.COPPER,Point(point).buffer(router.VIA_D/2));grid.block_drill(*point,router.VIA_DR)
        selected[net]=(point,target);records.append({'net':net,'escape':runs,'via_mm':point});print(json.dumps(records[-1]),flush=True)
    anchors={net:[point,xy(pads[target])] for net,(point,target) in selected.items()}
    for pair,start,end,side in [('OE_MDI3',(27.5,30.95),(27.5,28.9),'left'),('OE_MDI1',(32.65,29.5),(32.65,26.5),'left'),('OE_MDI0',(33.6,32.0),(33.6,28.9),'right')]:
        records.extend(mdi_bundle(board,pair+'_P',pair+'_N',start,[],end,anchors,side))
    pcbnew.ZONE_FILLER(board).Fill(board.Zones());pcbnew.SaveBoard(str(args.output),board)
    if args.source.with_suffix('.kicad_pro').exists():shutil.copy2(args.source.with_suffix('.kicad_pro'),args.output.with_suffix('.kicad_pro'))
    pcbnew.WriteDRCReport(board,str(args.output.with_suffix('.drc.txt')),pcbnew.EDA_UNITS_MILLIMETRES,True)
    args.output.with_suffix('.routes.json').write_text(hs.report_json(records),encoding='utf-8')
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('output',type=Path);parser.add_argument('--evidence',type=Path,required=True);parser.add_argument('--worker',action='store_true')
    args=parser.parse_args()
    if args.source.resolve()==args.output.resolve() or args.output.exists():raise ValueError('Require new output checkpoint')
    if args.worker:return worker(args)
    args.evidence.mkdir(parents=True,exist_ok=False)
    run_audit(args.source,args.evidence/'before.json')
    filtered=args.evidence/'filtered.kicad_pcb';count=filter_source(args.source,filtered)
    command=[sys.executable,__file__,str(filtered),str(args.output),'--evidence',str(args.evidence),'--worker']
    with (args.evidence/'worker.log').open('w',encoding='utf-8') as log:
        result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,text=True)
    report={'success':False,'source_sha256':hashlib.sha256(args.source.read_bytes()).hexdigest(),'removed_primitives':count,'command':command,'returncode':result.returncode}
    if result.returncode==0:
        report['after']=run_audit(args.output,args.evidence/'after.json')['summary']
    (args.evidence/'run.json').write_text(hs.report_json(report),encoding='utf-8')
    print(hs.report_json(report));return result.returncode

if __name__=='__main__':raise SystemExit(main())

#!/usr/bin/env python3
"""Repair PCIe TX/clock endpoint connectivity in an isolated six-layer candidate.

All four conductors use explicit paired interface endpoints and at most two
through transitions. Native DRC and endpoint audits are mandatory; no result
constitutes impedance/timing/fabrication qualification.
"""
import argparse,hashlib,json,math,os,re,shutil,subprocess,sys
from pathlib import Path
import pcbnew
from shapely.geometry import LineString,Point,box
import route_completion as router
import high_speed_audit as hs
from correct_high_speed import add_track,xy,pad_map,run_audit
from verification_core import parse_drc
from pcie_candidate_checks import assess_pcie_candidate
PAIRS={'PCIE_TX','PCIE_CLK'}
NETS={'PCIE_TX_P','PCIE_TX_N','PCIE_CLK_P','PCIE_CLK_N'}
RIP_NETS=NETS|{'PCIE_RST#_1V8'}
ENDPOINTS=[('PCIE_CLK_P','U10:26','J1:228'),('PCIE_CLK_N','U10:25','J1:226'),('PCIE_TX_N','U10:23','J1:238'),('PCIE_TX_P','U10:24','J1:240')]


def filtered(source,output):
    text=source.read_text(encoding='utf-8');codes={int(c) for c,n in re.findall(r'\(net (\d+) "([^"]+)"\)',text) if n in RIP_NETS}
    if len(codes)!=5:raise ValueError('Missing PCIe nets')
    kept=[];removed=[]
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(('(segment ','(via ','(arc ')):
            net=re.search(r'\(net (\d+)\)',line)
            if net is None:raise ValueError('Unsupported routing serialization')
            if int(net.group(1)) in codes:removed.append(line);continue
        kept.append(line)
    output.write_text(''.join(kept),encoding='utf-8')
    if source.with_suffix('.kicad_pro').exists():shutil.copy2(source.with_suffix('.kicad_pro'),output.with_suffix('.kicad_pro'))
    return removed


def matching_detour(points,extra,grid,net,guard_start=0.0):
    amplitude=extra/(2*(math.sqrt(2)-1))
    for index,(a,b) in enumerate(zip(points,points[1:])):
        length=math.dist(a,b);guard=guard_start if index==0 else 0.0
        if length-guard<2*amplitude+.05:continue
        dx=(b[0]-a[0])/length;dy=(b[1]-a[1])/length
        begin=(a[0]+guard*dx,a[1]+guard*dy)
        for sign in [1,-1]:
            nx=-dy*sign;ny=dx*sign
            first=(begin[0]+amplitude*(dx+nx),begin[1]+amplitude*(dy+ny))
            second=(b[0]+amplitude*(-dx+nx),b[1]+amplitude*(-dy+ny))
            replacement=[a]+([begin] if guard else [])+[first,second,b]
            candidate=points[:index]+replacement+points[index+2:]
            line=LineString(candidate)
            if not line.is_simple:continue
            conflict=False
            for other,items in grid.items.items():
                if other==net:continue
                for layers,shape,hole,_ in items:
                    if 'C' in layers and line.distance(shape)<router.W_TRK/2+router.CLR+.01+(router.HOLE_EXTRA if hole else 0):
                        conflict=True;break
                if conflict:break
            if not conflict:
                return candidate,{'extra_planar_mm':line.length-LineString(points).length,'amplitude_mm':amplitude,'original_segment':[a,b],'replacement':replacement}
    raise ValueError('No clear measured matching detour '+net)


def route_bundle(board,positive,negative,start,middle,end,anchors):
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
    for net,side in [(positive,'left'),(negative,'right')]:
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
                if net=='PCIE_TX_P' and escape_index==1:
                    points,tuning=matching_detour(points,.20,grid,net);records.append({'net':net,'matching':tuning})
                if net=='PCIE_CLK_P' and escape_index==0:
                    points,tuning=matching_detour(points,.70,grid,net,guard_start=.4);records.append({'net':net,'matching':tuning})
                tuned_runs.append((layer,points))
                for c,d in zip(points,points[1:]):add_track(board,net,pcbnew.In3_Cu,c,d,router.W_TRK)
                grid.add(net,['C'],LineString(points).buffer(router.W_TRK/2))
            records.append({'net':net,'escape':tuned_runs})
    return records


def worker(args):
    board=pcbnew.LoadBoard(str(args.source))
    if board.GetCopperLayerCount()!=6:raise ValueError('Requires six-layer stackup')
    router.W_TRK=.155;router.CLR=.18;router.EPS=.025
    pads=pad_map(board)
    prefixes={'PCIE_CLK_P':[(29.85,45.9375),(29.85,47.5997),(30.7427,48.4924)],
              'PCIE_CLK_N':[(30.35,45.9375),(30.35,47.3108),(31.6376,48.5984)],
              'PCIE_TX_P':[(30.85,45.9375),(30.85,46.3767),(32.4506,47.9773),(33.0772,47.9773),(33.8464,48.7465),(33.8464,50.3356),(33.7664,50.4156),(33.7664,50.9922),(34.8741,52.0999)],
              'PCIE_TX_N':[(31.35,45.9375),(31.35,45.292),(31.6107,45.0313)]}
    fixed={'PCIE_CLK_N':(31.6376,48.5984),'PCIE_TX_N':(31.6107,45.0313)}
    for net,device,module in ENDPOINTS:
        if Point(xy(pads[device])).distance(Point(prefixes[net][0]))>0.001:raise ValueError('Requalify changed I210 escape '+net)
        for a,b in zip(prefixes[net],prefixes[net][1:]):add_track(board,net,pcbnew.B_Cu,a,b,router.W_TRK)
    for net,point in fixed.items():
        via=pcbnew.PCB_VIA(board);via.SetPosition(pcbnew.VECTOR2I(round(point[0]*1e6),round(point[1]*1e6)))
        via.SetWidth(600000);via.SetDrill(300000);via.SetViaType(pcbnew.VIATYPE_THROUGH);via.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu);via.SetNet(board.GetNetsByName()[net]);via.SetLocked(True);board.Add(via)
    grid=router.Grid(board);grid.load(board)
    anchors={};records=[]
    for net,device,module in ENDPOINTS:
        anchors[net]=[]
        for endpoint,layer in [(device,'B'),(module,'F')]:
            if layer=='B' and net in fixed:
                anchors[net].append(fixed[net]);records.append({'net':net,'endpoint':endpoint,'retained_prefix':prefixes[net],'retained_via':fixed[net]});continue
            start=prefixes[net][-1] if layer=='B' else xy(pads[endpoint])
            if layer=='F' and net.startswith('PCIE_TX_'):
                target=(49.4,61.55) if net.endswith('_P') else (50.3,61.55)
                points=[start,(start[0],61.15),(target[0],61.15+abs(target[0]-start[0])),target]
                router.commit(board,grid,net,board.GetNetsByName()[net],[('F',points)],[target])
                anchors[net].append(target);records.append({'net':net,'endpoint':endpoint,'splayed_escape':points,'via':target});continue
            result=grid.route(net,start,layer,via_goal=True,via_forbidden=box(-10,-10,200,61.05) if layer=='F' else None)
            if result is None or len(result[1])!=1:raise ValueError('No unique legal endpoint transition for '+endpoint+' '+net)
            runs,vias=result
            if any(l!=layer for l,_ in runs):raise ValueError('Unexpected endpoint escape layer')
            router.commit(board,grid,net,board.GetNetsByName()[net],runs,vias)
            anchors[net].append(vias[0]);records.append({'net':net,'endpoint':endpoint,'runs':runs,'via':vias[0]});print(json.dumps(records[-1]),flush=True)
    records.extend(route_bundle(board,'PCIE_TX_P','PCIE_TX_N',(36,51.7),[],(48,60.5),anchors))
    records.extend(route_bundle(board,'PCIE_CLK_P','PCIE_CLK_N',(33,52),[(44,62.5)],(51.5,62.0),anchors))
    pcbnew.ZONE_FILLER(board).Fill(board.Zones());pcbnew.SaveBoard(str(args.output),board)
    if args.source.with_suffix('.kicad_pro').exists():shutil.copy2(args.source.with_suffix('.kicad_pro'),args.output.with_suffix('.kicad_pro'))
    assert pcbnew.WriteDRCReport(board,str(args.output.with_suffix('.drc.txt')),pcbnew.EDA_UNITS_MILLIMETRES,True)
    args.output.with_suffix('.routes.json').write_text(hs.report_json(records),encoding='utf-8')
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('source',type=Path);parser.add_argument('output',type=Path);parser.add_argument('--evidence',type=Path,required=True);parser.add_argument('--worker',action='store_true')
    args=parser.parse_args()
    if args.source.resolve()==args.output.resolve() or args.output.exists():raise ValueError('Use new isolated output')
    if args.worker:return worker(args)
    args.evidence.mkdir(parents=True,exist_ok=False);digest=hashlib.sha256(args.source.read_bytes()).hexdigest()
    before=run_audit(args.source,args.evidence/'before.json')
    scratch=args.evidence/'filtered.kicad_pcb';removed=filtered(args.source,scratch)
    (args.evidence/'removed-copper.json').write_text(hs.report_json({'source_sha256':digest,'records':removed}),encoding='utf-8')
    cmd=[sys.executable,__file__,str(scratch),str(args.output),'--evidence',str(args.evidence),'--worker'];
    with (args.evidence/'worker.log').open('w',encoding='utf-8') as log:
        r=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,text=True)
    result={'fabrication_ready':False,'source_sha256':digest,'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'command':cmd,'returncode':r.returncode}
    if r.returncode==0:
        after=run_audit(args.output,args.evidence/'after.json');selected=[p for p in after.get('pairs',[]) if p.get('name') in PAIRS]
        checks=assess_pcie_candidate(after,args.output.with_suffix('.drc.txt').read_text(encoding='utf-8'))
        result.update(checks)
        result.update({'pair_summary':[{k:p.get(k) for k in ['name','errors','planar_skew_mm','via_symmetry_pass','via_budget_pass','layer_sequence_symmetry_pass']} for p in selected],'output_sha256':hashlib.sha256(args.output.read_bytes()).hexdigest()})
    if hashlib.sha256(args.source.read_bytes()).hexdigest()!=digest:raise ValueError('Source mutated during routing')
    (args.evidence/'result.json').write_text(hs.report_json(result),encoding='utf-8');print(hs.report_json(result))
    return 0 if result.get('targeted_geometric_checks_pass') is True else 1

if __name__=='__main__':raise SystemExit(main())

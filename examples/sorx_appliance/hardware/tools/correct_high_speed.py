#!/usr/bin/env python3
"""Create an isolated high-speed correction candidate; never overwrite its input.

Place U19 at the external port, remove ESD branches, pair the TX/RX trunks and
match their actual endpoint paths. RX uses In3; its layer-specific impedance is
NOT qualified. Three explicitly identified +1V8 segments require a later power
repair. The narrow geometric acceptance gate is never fabrication approval.
"""
import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import pcbnew
from shapely.geometry import LineString, Point
import high_speed_audit as hs
import route_completion as router
from usb_candidate_checks import assess_usb_candidate

USB_NETS={'USB2_SSTX_P','USB2_SSTX_N','USB2_SSRX_P','USB2_SSRX_N'}
PORT_POWER_OBSTACLES={'0725c8e0-d079-49bf-8822-283b3a84f6a6','131fd0e9-dba6-4720-94ac-dd5874184433','cb7209b7-9238-43b4-83a0-621ca612fcbb'}


def xy(item):
    p=item.GetPosition();return (p.x/1e6,p.y/1e6)


def add_track(board,net,layer,a,b,width):
    if math.dist(a,b)<1e-6:return
    item=pcbnew.PCB_TRACK(board)
    item.SetStart(pcbnew.VECTOR2I(round(a[0]*1e6),round(a[1]*1e6)))
    item.SetEnd(pcbnew.VECTOR2I(round(b[0]*1e6),round(b[1]*1e6)))
    item.SetWidth(round(width*1e6));item.SetLayer(layer);item.SetNet(board.GetNetsByName()[net]);item.SetLocked(True)
    board.Add(item)


def input_routes(geometry):
    routes={}
    for spec in hs.definitions():
        if spec['name'] not in ('USB2_SSTX','USB2_SSRX'):continue
        for polarity in ('p','n'):
            a,b,nets=spec[polarity];net=nets[0]
            graph=hs.RouteGraph([t for t in geometry['tracks'] if t['net']==net],
                [p for p in geometry['pads'] if p['net']==net],[v for v in geometry['vias'] if v['net']==net])
            if graph.errors:raise ValueError(graph.errors)
            route=graph.route(a,b)
            port_via=max((v for v in geometry['vias'] if v['id'] in route['via_ids']),key=lambda v:v['xy'][1])
            routes[net]={'route':route,'port_via':port_via,'vias':[v for v in geometry['vias'] if v['id'] in route['via_ids']]}
    return routes


def prep(board,position,rotation,routes):
    footprints={f.GetReference():f for f in board.GetFootprints()}
    esd=footprints['U19']
    old_xy=xy(esd)
    # Audited USB3 copper was removed from serialized records before this fresh process loaded the board.
    for net,data in routes.items():
        route=data['route']
        for edge in route['edges']:
            if edge['kind'] in ('track','pad_copper','via_copper'):
                if edge['layer']=='F.Cu' and 80<min(edge['start'][1],edge['end'][1])<97:continue
                layer=board.GetLayerID(edge['layer']);a,b=edge['start'],edge['end']
                if edge['layer']=='B.Cu':
                    if net.endswith('_N'):continue
                    if min(a[1],b[1])>=90:continue
                    if max(a[1],b[1])>90:
                        if abs(a[0]-b[0])>1e-5:raise ValueError('Unsupported nonvertical USB trunk cutoff')
                        a=(a[0],min(a[1],90));b=(b[0],min(b[1],90));data['trunk_anchor']=(a[0],90)
                    if 'SSRX' in net:layer=pcbnew.In3_Cu
                if net=='USB2_SSTX_P' and edge['layer']=='F.Cu':
                    a=(a[0],97.25) if abs(a[0]-63)<1e-5 and abs(a[1]-97.2304)<1e-5 else a
                    b=(b[0],97.25) if abs(b[0]-63)<1e-5 and abs(b[1]-97.2304)<1e-5 else b
                if net=='USB2_SSRX_P' and layer==pcbnew.In3_Cu and max(a[1],b[1])>=90 and min(a[1],b[1])<84:
                    low=min([a,b],key=lambda q:q[1]);x=low[0];amplitude=2.25
                    points=[low,(x,84),(x-amplitude,84+amplitude),(x-amplitude,90-amplitude),(x,90)]
                    for c,d in zip(points,points[1:]):add_track(board,net,layer,c,d,edge.get('width_mm',0.155))
                else:add_track(board,net,layer,a,b,edge.get('width_mm',0.155))
        if net.endswith('_N'):
            source_via=min(data['vias'],key=lambda v:v['xy'][1]);sx,sy=source_via['xy']
            rail=67.169 if 'SSTX' in net else 65.769
            y0=52.5 if 'SSTX' in net else 54.1
            points=[(sx,sy),(sx,y0),(rail,y0+abs(sx-rail)),(rail,90)]
            layer=pcbnew.B_Cu if 'SSTX' in net else pcbnew.In3_Cu
            for a,b in zip(points,points[1:]):add_track(board,net,layer,a,b,.155)
            data['trunk_anchor']=(rail,90)
        for data_via in data['vias']:
            if data_via['id']==data['port_via']['id']:continue
            via=pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(round(data_via['xy'][0]*1e6),round(data_via['xy'][1]*1e6)))
            via.SetWidth(round(data_via['diameter_mm']*1e6));via.SetDrill(round(data_via['drill_mm']*1e6))
            via.SetViaType(pcbnew.VIATYPE_THROUGH);via.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu)
            via.SetNet(board.GetNetsByName()[net]);via.SetLocked(True);board.Add(via)
    esd.SetPosition(pcbnew.VECTOR2I(round(position[0]*1e6),round(position[1]*1e6)))
    esd.SetOrientationDegrees(rotation)
    pass_map={'1':'USB2_SSTX_P','10':'USB2_SSTX_P','2':'USB2_SSTX_N','9':'USB2_SSTX_N','4':'USB2_SSRX_P','7':'USB2_SSRX_P','5':'USB2_SSRX_N','6':'USB2_SSRX_N'}
    for pad in esd.Pads():
        if pad.GetNumber() in pass_map:pad.SetNet(board.GetNetsByName()[pass_map[pad.GetNumber()]])
    return board,{'ref':'U19','old_xy_mm':old_xy,'new_xy_mm':position,'rotation_deg':rotation,'side':'F'}


def pad_map(board):
    return {f.GetReference()+':'+p.GetNumber():p for f in board.GetFootprints() for p in f.Pads()}


def place_clear(board):
    esd=next(f for f in board.GetFootprints() if f.GetReference()=='U19')
    geometry=[]
    for f in board.GetFootprints():
        if f.GetReference()=='U19':continue
        for pad in f.Pads():
            if pad.IsOnLayer(pcbnew.F_Cu):
                for shape in hs.polygons(pad.GetEffectivePolygon()):geometry.append((pad.GetNetname(),shape))
    for item in board.GetTracks():
        if item.GetClass()=='PCB_VIA':
            geometry.append((item.GetNetname(),Point(xy(item)).buffer(item.GetWidth()/2e6)))
        elif item.GetLayer()==pcbnew.F_Cu:
            a,b=item.GetStart(),item.GetEnd()
            geometry.append((item.GetNetname(),LineString([(a.x/1e6,a.y/1e6),(b.x/1e6,b.y/1e6)]).buffer(item.GetWidth()/2e6)))
    for pad in esd.Pads():
        for shape in hs.polygons(pad.GetEffectivePolygon()):
            for net,other in geometry:
                if net and net==pad.GetNetname():continue
                if shape.distance(other)<0.180-1e-6:
                    return False
    return True


def connect_usb(board,routes):
    router.W_TRK=0.155;router.CLR=0.18;router.EPS=0.025
    pads=pad_map(board)
    escapes={}
    for net,a,b in [('USB2_SSTX_P','U19:10','U19:1'),('USB2_SSTX_N','U19:9','U19:2'),('USB2_SSRX_P','U19:7','U19:4'),('USB2_SSRX_N','U19:6','U19:5')]:
        entrance,exit_=xy(pads[a]),xy(pads[b])
        distance=math.dist(entrance,exit_);dx=(exit_[0]-entrance[0])/distance;dy=(exit_[1]-entrance[1])/distance
        first=(entrance[0]-0.8*dx,entrance[1]-0.8*dy);last=(exit_[0]+0.8*dx,exit_[1]+0.8*dy)
        escapes[net]=(first,last)
        add_track(board,net,pcbnew.F_Cu,entrance,exit_,router.W_TRK)
        add_track(board,net,pcbnew.F_Cu,exit_,last,router.W_TRK)
    new_entries={}
    for net,padname,offset in [('USB2_SSTX_P','U19:10',1.2),('USB2_SSTX_N','U19:9',2.1),('USB2_SSRX_P','U19:7',1.2),('USB2_SSRX_N','U19:6',2.1)]:
        x,y=xy(pads[padname]);point=(x+(0.2 if net.endswith('_N') else 0),y-offset);new_entries[net]=point
        via=pcbnew.PCB_VIA(board);via.SetPosition(pcbnew.VECTOR2I(round(point[0]*1e6),round(point[1]*1e6)))
        via.SetWidth(600000);via.SetDrill(300000);via.SetViaType(pcbnew.VIATYPE_THROUGH);via.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu)
        via.SetNet(board.GetNetsByName()[net]);via.SetLocked(True);board.Add(via)
        add_track(board,net,pcbnew.F_Cu,point,(x,y),router.W_TRK)
    grid=router.Grid(board);grid.load(board)
    # Endpoint escapes are intentionally top-layer only. Never hide an extra via
    # in a congested escape: it would violate the retained two-via USB3 budget.
    for array in grid.V.values():array.fill(60000)
    pads=pad_map(board);records=[]
    spec=[('USB2_SSTX_P','U19:10','U19:1','J8:9'),('USB2_SSTX_N','U19:9','U19:2','J8:8'),
          ('USB2_SSRX_P','U19:7','U19:4','J8:6'),('USB2_SSRX_N','U19:6','U19:5','J8:5')]
    for output_phase in [True,False]:
      for net,entry,esd,port in spec:
        start=routes[net]['trunk_anchor'];middle=xy(pads[esd]);end=min((point for edge in routes[net]['route']['edges'] if edge.get('layer')=='F.Cu' and min(edge['start'][1],edge['end'][1])>=97 for point in [edge['start'],edge['end']]),key=lambda point:point[1])
        if net=='USB2_SSTX_P':end=(63.0,97.25)
        for a,b in ([(escapes[net][1],end)] if output_phase else [(start,new_entries[net])]):
            route_layer='F' if output_phase else ('C' if 'SSRX' in net else 'B')
            result=grid.route(net,a,route_layer,b,route_layer)
            if result is None:
                raise ValueError('No '+route_layer+' USB3 path '+net+' '+str((a,b)))
            runs,vias=result
            if vias or any(layer!=route_layer for layer,_ in runs):raise ValueError('Unexpected layer transition in USB3 escape')
            for layer,points in runs:
                for p,q in zip(points,points[1:]):
                    add_track(board,net,router.LAY[layer],p,q,router.W_TRK)
                grid.add(net,[layer],LineString(points).buffer(router.W_TRK/2))
            records.append({'net':net,'from':a,'to':b,'runs':runs})
    # Both protection GND pads must reach a ground plane; do not claim their old
    # connections moved with the component. Rebuild occupancy for real vias.
    ground_grid=router.Grid(board);ground_grid.load(board)
    for name in ('U19:3','U19:8'):
        result=ground_grid.route('GND',xy(pads[name]),'F',via_goal=True)
        if result is None:raise ValueError('No protected GND escape for '+name)
        runs,vias=result
        router.commit(board,ground_grid,'GND',board.GetNetsByName()['GND'],runs,vias)
        records.append({'ground_pad':name,'runs':runs,'vias_mm':vias})
    return records


def run_audit(source,output):
    command=[sys.executable,str(Path(__file__).with_name('high_speed_audit.py')),str(source),'--output',str(output)]
    result=subprocess.run(command,capture_output=True,text=True)
    output.with_suffix('.run.json').write_text(hs.report_json({'command':command,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr}))
    if result.returncode not in (0,1) or not output.exists():raise RuntimeError('Audit subprocess failed')
    report=json.loads(output.read_text())
    if report.get('source_pcb_sha256')!=hashlib.sha256(source.read_bytes()).hexdigest():raise RuntimeError('Audit is not bound to candidate')
    return report


def worker(args):
    # KiCad's Python bindings are not safely re-entrant across repeated LoadBoard
    # lifecycles in this image. One board per worker, with audits in fresh processes.
    source_hash=hashlib.sha256(args.source.read_bytes()).hexdigest()
    routes=json.loads(args.route_data.read_text())
    source_text=args.source.read_text(encoding='utf-8')
    codes={int(code) for code,name in re.findall(r'\(net (\d+) "([^"]+)"\)',source_text) if name in USB_NETS}
    if len(codes)!=4:raise ValueError('Missing USB3 net definitions')
    kept=[];removed=[]
    for line in source_text.splitlines(keepends=True):
        if line.lstrip().startswith(('(segment ','(via ','(arc ')):
            match=re.search(r'\(net (\d+)\)',line)
            if match is None:raise ValueError('Only validated single-line routing records may be filtered')
            uuid=re.search(r'\(tstamp ([^)]+)\)',line)
            if int(match.group(1)) in codes or (uuid and uuid.group(1) in PORT_POWER_OBSTACLES):
                removed.append(line);continue
        kept.append(line)
    if not removed:raise ValueError('No audited USB3 copper removed')
    args.output.with_suffix('.removed-copper.json').write_text(hs.report_json({'source_sha256':hashlib.sha256(args.source.read_bytes()).hexdigest(),'records':removed,'ordinary_net_repair_required':'+1V8 exact port obstacles'}),encoding='utf-8')
    filtered=Path('/tmp')/('sorx-codex-usb3-filtered-%d.kicad_pcb'%os.getpid())
    filtered.write_text(''.join(kept),encoding='utf-8')
    if args.source.with_suffix('.kicad_pro').exists():shutil.copy2(args.source.with_suffix('.kicad_pro'),filtered.with_suffix('.kicad_pro'))
    board=pcbnew.LoadBoard(str(filtered))
    if board.GetCopperLayerCount()!=6:raise ValueError('Requires the explicit six-layer candidate')
    footprints={f.GetReference():f for f in board.GetFootprints()}
    for ref,expected in [('J1',(78,64)),('J8',(68,110.62))]:
        if math.dist(xy(footprints[ref]),expected)>0.001:raise ValueError('Requalify fixed routing anchors after moving '+ref)
    x,y,rotation=map(float,args.positions.split(','))
    if rotation!=90:raise ValueError('This validated port fanout requires U19 rotation90')
    board,move=prep(board,(x,y),rotation,routes)
    if not place_clear(board):raise ValueError('New U19 pad clearance conflicts with retained copper')
    records=connect_usb(board,routes)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(args.output),board)
    if args.source.with_suffix('.kicad_pro').exists():shutil.copy2(args.source.with_suffix('.kicad_pro'),args.output.with_suffix('.kicad_pro'))
    pcbnew.WriteDRCReport(board,str(args.output.with_suffix('.drc.txt')),pcbnew.EDA_UNITS_MILLIMETRES,True)
    report=run_audit(args.output,args.output.with_suffix('.audit.json'))
    selected=[p for p in report['pairs'] if p['name'] in ('USB2_SSTX','USB2_SSRX')]
    checks=assess_usb_candidate(report,args.output.with_suffix('.drc.txt').read_text())
    args.output.with_suffix('.checks.json').write_text(hs.report_json(checks),encoding='utf-8')
    if not checks['targeted_geometric_checks_pass']:raise ValueError('; '.join(checks['failures']))
    if hashlib.sha256(args.source.read_bytes()).hexdigest()!=source_hash:raise ValueError('Source changed during candidate routing')
    offpath=sum(p[c]['off_path_track_length_mm'] for p in selected for c in ('p','n'))
    mismatch=max(p['planar_skew_mm'] for p in selected)
    result={'source_sha256':source_hash,'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'checks':checks,'move':move,'routes':records,'off_path_mm':offpath,'max_pair_mismatch_mm':mismatch,'candidate':str(args.output)}
    args.output.with_suffix('.result.json').write_text(hs.report_json(result))
    print('Candidate offpath',offpath,'max mismatch',mismatch,flush=True)
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--extract-routes',action='store_true')
    parser.add_argument('--route-data',type=Path)
    parser.add_argument('--positions',default='66,96,90')
    args=parser.parse_args()
    if args.source.resolve()==args.output.resolve() or args.output.exists():raise ValueError('Use a new independent output checkpoint')
    if args.extract_routes:
        args.output.write_text(hs.report_json(input_routes(hs.load_geometry(args.source))),encoding='utf-8')
        return 0
    if args.worker:
        try:return worker(args)
        except Exception as error:
            import traceback
            traceback.print_exc()
            args.output.with_suffix('.error.json').write_text(hs.report_json({'error':str(error)}))
            print('Rejected:',error,flush=True)
            return 1
    args.evidence.mkdir(parents=True,exist_ok=False)
    run_audit(args.source,args.evidence/'before.json')
    route_data=args.evidence/'input-routes.json'
    subprocess.run([sys.executable,__file__,str(args.source),str(route_data),'--evidence',str(args.evidence),'--extract-routes'],check=True)
    attempts=[];best=None
    for index,setting in enumerate(args.positions.split(';')):
        trial=args.evidence/('trial-%02d.kicad_pcb'%index)
        command=[sys.executable,__file__,str(args.source),str(trial),'--evidence',str(args.evidence),'--positions',setting,'--route-data',str(route_data),'--worker']
        print('Attempt',index,setting,flush=True)
        result=subprocess.run(command,capture_output=True,text=True)
        trial.with_suffix('.log').write_text(result.stdout+result.stderr)
        entry={'setting':setting,'command':command,'returncode':result.returncode}
        if result.returncode==0:
            entry.update(json.loads(trial.with_suffix('.result.json').read_text()))
            score=(entry['off_path_mm'],entry['max_pair_mismatch_mm'])
            if best is None or score<best[0]:best=(score,trial,entry)
        else:
            entry['error']=json.loads(trial.with_suffix('.error.json').read_text()).get('error') if trial.with_suffix('.error.json').exists() else result.stderr[-1000:]
        attempts.append(entry)
        print(hs.report_json({k:v for k,v in entry.items() if k not in ('routes','command')}),flush=True)
        (args.evidence/'attempts.json').write_text(hs.report_json(attempts))
    result={'success':False,'source_sha256':hashlib.sha256(args.source.read_bytes()).hexdigest(),'attempts':attempts,
            'scope':'USB3 topology, paired trunks and planar matching; +1V8 repair and SI qualification remain required'}
    if best:
        args.output.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(best[1],args.output)
        if best[1].with_suffix('.kicad_pro').exists():shutil.copy2(best[1].with_suffix('.kicad_pro'),args.output.with_suffix('.kicad_pro'))
        shutil.copy2(best[1].with_suffix('.drc.txt'),args.evidence/'candidate-drc.txt')
        result.update({'output':str(args.output),'output_sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),'selected':best[2],
                       'topology_gate_pass':best[0][0]<=0.01})
    (args.evidence/'result.json').write_text(hs.report_json(result))
    print(hs.report_json({k:v for k,v in result.items() if k not in ('attempts','selected')}))
    return 0 if result.get('topology_gate_pass') else 1

if __name__=='__main__':raise SystemExit(main())

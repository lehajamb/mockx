#!/usr/bin/env python3
"""Conservative endpoint-path geometry audit; not a signal-integrity simulation.

Requires KiCad pcbnew and Shapely. Never modifies the PCB. Incomplete paths,
unsupported geometry, violated limits and unqualified stackup produce exit 1.
"""
import argparse
from collections import defaultdict
import hashlib
import heapq
import json
import math
from pathlib import Path
import sys
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

EPS = 1e-6
TOR = 'https://docs.toradex.com/102492-layout-design-guide.pdf'
INTEL = 'https://cdrdv2-public.intel.com/333016/333016%20-%20I210_Datasheet_v_3_7.pdf'


def key(net, layer, xy):
    return (net, layer, round(xy[0], 6), round(xy[1], 6))


def geometry_points(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == 'Point':
        return [tuple(geometry.coords[0])]
    if geometry.geom_type in ('LineString', 'LinearRing'):
        return [tuple(geometry.coords[0]), tuple(geometry.coords[-1])]
    return [point for item in geometry.geoms for point in geometry_points(item)]


class RouteGraph:
    """Noded track centerlines with explicit copper pads, vias and series devices."""
    def __init__(self, tracks, pads, vias, links=()):
        self.tracks, self.pads, self.vias = tracks, pads, vias
        self.adj = defaultdict(list)
        self.edges = []
        self.errors = []
        cuts = {t['id']: {0.0, t['line'].length} for t in tracks}
        for i, a in enumerate(tracks):
            for b in tracks[i+1:]:
                if (a['net'], a['layer']) != (b['net'], b['layer']):
                    continue
                overlap = a['line'].intersection(b['line'])
                if overlap.length > EPS:
                    self.errors.append('Overlapping track centerlines: '+a['id']+' / '+b['id'])
                for xy in geometry_points(overlap):
                    cuts[a['id']].add(a['line'].project(Point(xy)))
                    cuts[b['id']].add(b['line'].project(Point(xy)))
        for t in tracks:
            for v in vias:
                if v['net'] == t['net'] and t['layer'] in v['layers']:
                    if t['line'].distance(Point(v['xy'])) < EPS:
                        cuts[t['id']].add(t['line'].project(Point(v['xy'])))
                    if v.get('diameter_mm',0)>0:
                        land=Point(v['xy']).buffer(v['diameter_mm']/2,64)
                        for xy in geometry_points(t['line'].intersection(land)):
                            cuts[t['id']].add(t['line'].project(Point(xy)))
            for p in pads:
                if p['net'] != t['net'] or t['layer'] not in p['layers']:
                    continue
                for xy in geometry_points(t['line'].intersection(p['shape'])):
                    cuts[t['id']].add(t['line'].project(Point(xy)))
        for t in tracks:
            distances = sorted(cuts[t['id']])
            for start, end in zip(distances, distances[1:]):
                if end-start < EPS:
                    continue
                a, b = tuple(t['line'].interpolate(start).coords[0]), tuple(t['line'].interpolate(end).coords[0])
                self.add(key(t['net'], t['layer'], a), key(t['net'], t['layer'], b), end-start,
                         {'kind':'track','object':t['id'],'net':t['net'],'layer':t['layer'],
                          'start':a,'end':b,'width_mm':t['width_mm']})
        for v in vias:
            nodes = [key(v['net'], layer, v['xy']) for layer in v['layers']]
            for a, b in zip(nodes, nodes[1:]):
                self.add(a, b, 0.0, {'kind':'via','object':v['id'],'xy':v['xy'],'layers':[a[1],b[1]],
                                    'barrel_length_mm':None})
        geometry_nodes = list(self.adj)
        for v in vias:
            radius=v.get('diameter_mm',0)/2
            if radius<=0:continue
            for n in geometry_nodes:
                if n[0]==v['net'] and n[1] in v['layers']:
                    radial=math.dist(v['xy'],n[2:])
                    if EPS<radial<=radius+EPS:
                        self.add(key(v['net'],n[1],v['xy']),n,radial,
                                 {'kind':'via_copper','object':v['id'],'net':v['net'],'layer':n[1],
                                  'start':v['xy'],'end':n[2:],
                                  'length_convention':'Radial pad-center geometric estimate; not barrel-current or propagation-delay extraction.'})
        for p in pads:
            node = ('pad',p['id'])
            self.adj[node]
            for n in geometry_nodes:
                if n[0] == p['net'] and n[1] in p['layers'] and p['shape'].buffer(EPS).covers(Point(n[2:])):
                    self.add(node,n,math.dist(p['xy'],n[2:]),
                             {'kind':'pad_copper','object':p['id'],'net':p['net'],'layer':n[1],
                              'start':p['xy'],'end':n[2:]})
        for link in links:
            self.add(('pad',link['a']),('pad',link['b']),0.0,
                     {'kind':'series_component',**link})

    def add(self, a, b, length, data):
        index = len(self.edges)
        self.edges.append({'length_mm':length,**data})
        self.adj[a].append((b,length,index))
        self.adj[b].append((a,length,index))

    def route(self, source, destination):
        start, end = ('pad',source), ('pad',destination)
        if start not in self.adj or end not in self.adj:
            raise ValueError('Missing endpoint '+source+' or '+destination)
        queue = [(0.0,0,start)]
        distance, previous = {start:0.0}, {}
        serial = 0
        while queue:
            cost, _, node = heapq.heappop(queue)
            if cost > distance[node]+1e-12:
                continue
            if node == end:
                break
            for other, length, edge in self.adj[node]:
                candidate = cost+length
                if candidate < distance.get(other,math.inf)-1e-12:
                    distance[other] = candidate
                    previous[other] = (node,edge)
                    serial += 1
                    heapq.heappush(queue,(candidate,serial,other))
        if end not in distance:
            raise ValueError('No copper/declared-series-component path: '+source+' -> '+destination)
        indices, node = [], end
        while node != start:
            node, edge = previous[node]
            indices.append(edge)
        indices.reverse()
        edges = [self.edges[i] for i in indices]
        via_ids = sorted({e['object'] for e in edges if e['kind']=='via'})
        layer_sequence = []
        for e in edges:
            if e['kind'] == 'track' and (not layer_sequence or layer_sequence[-1] != e['layer']):
                layer_sequence.append(e['layer'])
        used_tracks = [e for e in edges if e['kind']=='track']
        off_path = []
        selected = set(indices)
        pad_shapes = defaultdict(list)
        for p in self.pads:
            pad_shapes[p['net']].append(p['shape'])
        for v in self.vias:
            if v.get('diameter_mm',0)>0:pad_shapes[v['net']].append(Point(v['xy']).buffer(v['diameter_mm']/2,64))
        pad_unions = {net:unary_union(shapes) for net,shapes in pad_shapes.items()}
        for i, e in enumerate(self.edges):
            if e['kind'] != 'track' or i in selected:
                continue
            line = LineString([e['start'],e['end']])
            remaining = line.difference(pad_unions.get(e['net'],Polygon()))
            if remaining.length > 0.001:
                off_path.append({'object':e['object'],'length_mm':round(remaining.length,6),
                                 'net':e['net'],'layer':e['layer'],'start':e['start'],'end':e['end']})
        return {'source':source,'destination':destination,'planar_length_mm':distance[end],
                'track_length_mm':sum(e['length_mm'] for e in used_tracks),
                'pad_copper_length_mm':sum(e['length_mm'] for e in edges if e['kind']=='pad_copper'),
                'via_radial_estimate_mm':sum(e['length_mm'] for e in edges if e['kind']=='via_copper'),
                'series_components':[e for e in edges if e['kind']=='series_component'],
                'via_count':len(via_ids),'via_ids':via_ids,'layer_sequence':layer_sequence,
                'via_barrel_length_mm':None if via_ids else 0.0,'edges':edges,
                'off_path_track_length_mm':sum(e['length_mm'] for e in off_path),'off_path_tracks':off_path}


def chain_points(chain):
    return [(chain.CPoint(i).x/1e6,chain.CPoint(i).y/1e6) for i in range(chain.PointCount())]


def polygons(polyset):
    result=[]
    for i in range(polyset.OutlineCount()):
        outer=chain_points(polyset.Outline(i))
        holes=[chain_points(polyset.Hole(i,j)) for j in range(polyset.HoleCount(i))]
        if len(outer)<3:
            continue
        shape=Polygon(outer,[h for h in holes if len(h)>=3])
        if not shape.is_valid:
            # KiCad's fractured polygons include zero-area bridges. Accept only an area-preserving repair.
            repaired=shape.buffer(0)
            if not repaired.is_valid or abs(repaired.area-shape.area)>1e-6:
                raise ValueError('Invalid KiCad polygon; cannot safely determine reference coverage')
            shape=repaired
        result.append(shape)
    return result


def load_geometry(path, board=None):
    import pcbnew
    if board is None:
        board=pcbnew.LoadBoard(str(path))
    layers=[board.GetLayerName(i) for i in range(32) if board.IsLayerEnabled(i)]
    layer_ids={board.GetLayerName(i):i for i in range(32) if board.IsLayerEnabled(i)}
    tracks,pads,vias,errors=[],[],[],[]
    for item in board.GetTracks():
        object_id=item.m_Uuid.AsString()
        net=item.GetNetname()
        if item.GetClass()=='PCB_VIA':
            vias.append({'id':object_id,'net':net,'xy':(item.GetPosition().x/1e6,item.GetPosition().y/1e6),
                         'layers':[layer for layer in layers if item.IsOnLayer(layer_ids[layer])],
                         'diameter_mm':item.GetWidth()/1e6,'drill_mm':item.GetDrillValue()/1e6})
        elif item.GetClass()=='PCB_TRACK':
            a,b=item.GetStart(),item.GetEnd()
            line=LineString([(a.x/1e6,a.y/1e6),(b.x/1e6,b.y/1e6)])
            if line.length<EPS:
                errors.append('Zero-length track '+object_id)
                continue
            tracks.append({'id':object_id,'net':net,'layer':board.GetLayerName(item.GetLayer()),
                           'line':line,'width_mm':item.GetWidth()/1e6})
        else:
            errors.append('Unsupported routed primitive '+item.GetClass()+' '+object_id+' net '+net)
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            if not pad.GetNetname():
                continue
            pads.append({'id':footprint.GetReference()+':'+pad.GetNumber(),'ref':footprint.GetReference(),
                         'pin':pad.GetNumber(),'net':pad.GetNetname(),'value':footprint.GetValue(),
                         'xy':(pad.GetPosition().x/1e6,pad.GetPosition().y/1e6),
                         'layers':[layer for layer in layers if pad.IsOnLayer(layer_ids[layer])],
                         'shape':unary_union(polygons(pad.GetEffectivePolygon()))})
    grounds=defaultdict(list)
    for zone in board.Zones():
        if zone.GetIsRuleArea() or zone.GetNetname()!='GND':
            continue
        for layer,layer_id in layer_ids.items():
            if zone.IsOnLayer(layer_id) and zone.HasFilledPolysForLayer(layer_id):
                grounds[layer]+=polygons(zone.GetFilledPolysList(layer_id))
    return {'tracks':tracks,'pads':pads,'vias':vias,'ground':{k:unary_union(v) for k,v in grounds.items()},
            'layers':layers,'errors':errors,'pcbnew_version':pcbnew.GetBuildVersion()}


def definitions():
    pairs=[]
    jack_p=[11,4,3,8];jack_n=[10,5,2,9]
    for prefix,source,destination,positive,negative in [
        ('MD','J1','J4',[225,233,239,247],[227,231,241,245]),
        ('OE','U10','J6',[58,55,53,50],[57,54,52,49]),
        ('MG','U13','J7',[2,5,7,10],[3,6,8,11])]:
        for i in range(4):
            name=prefix+'_MDI'+str(i)
            pairs.append({'name':name,'family':'MDI','p':(source+':'+str(positive[i]),destination+':'+str(jack_p[i]),[name+'_P']),
                          'n':(source+':'+str(negative[i]),destination+':'+str(jack_n[i]),[name+'_N']),
                          'skew_limit_mm':0.25,'max_length_mm':100.0,'max_vias':2,
                          'source':TOR+'#page=34','limit_status':'Toradex carrier target; MG device-specific acceptance pending' if prefix=='MG' else 'Toradex carrier target',
                          'intel_mdi_limit_mm':1.25 if prefix=='OE' else None})
    for name,pins,nets,via_limit in [
        ('PCIE_TX',('J1:240','U10:24','J1:238','U10:23'),(['PCIE_TX_P'],['PCIE_TX_N']),2),
        ('PCIE_RX',('U10:21','J1:234','U10:20','J1:232'),(['OE_PET_P','PCIE_RX_P'],['OE_PET_N','PCIE_RX_N']),4),
        ('PCIE_CLK',('J1:228','U10:26','J1:226','U10:25'),(['PCIE_CLK_P'],['PCIE_CLK_N']),None),
        ('USB2_SSTX',('J1:171','J8:9','J1:169','J8:8'),(['USB2_SSTX_P'],['USB2_SSTX_N']),2),
        ('USB2_SSRX',('J8:6','J1:177','J8:5','J1:175'),(['USB2_SSRX_P'],['USB2_SSRX_N']),2),
        ('USB2_D',('J1:183','J8:3','J1:181','J8:2'),(['USB2_D_P'],['USB2_D_N']),None),
        ('USB1_D',('J1:165','J9:3','J1:163','J9:2'),(['USB1_D_P'],['USB1_D_N']),None),
        ('USB_DEBUG_A',('J10:A6','U24:8','J10:A7','U24:9'),(['DBG_D_P','FT_DP'],['DBG_D_N','FT_DM']),None),
        ('USB_DEBUG_B',('J10:B6','U24:8','J10:B7','U24:9'),(['DBG_D_P','FT_DP'],['DBG_D_N','FT_DM']),None)]:
        usb2=name in ('USB1_D','USB2_D','USB_DEBUG_A','USB_DEBUG_B')
        pairs.append({'name':name,'family':'USB2' if usb2 else ('PCIe' if name.startswith('PCIE') else 'USB3'),
                      'p':(pins[0],pins[1],nets[0]),'n':(pins[2],pins[3],nets[1]),
                      'skew_limit_mm':1.1 if usb2 else 0.15,'max_length_mm':300.0 if name.startswith('PCIE') else 200.0,
                      'max_vias':via_limit,'source':TOR+('#page=33' if name.startswith('PCIE') else '#page=36'),
                      'limit_status':'Toradex carrier target; I210-specific PCIe routing confirmation pending' if name.startswith('PCIE') else 'Toradex carrier target'})
    return pairs


def series_links(pads,nets):
    if len(nets)==1:
        return []
    grouped=defaultdict(list)
    for p in pads:
        grouped[p['ref']].append(p)
    found=[]
    for ref,items in grouped.items():
        if len(items)==2 and {p['net'] for p in items}==set(nets):
            allowed=(ref.startswith('C') and items[0]['value']=='100nF' and any(n.startswith('OE_PET') for n in nets)) or (ref.startswith('R') and items[0]['value']=='27' and any(n.startswith('DBG_D') for n in nets))
            if allowed:
                found.append({'a':items[0]['id'],'b':items[1]['id'],'object':ref,'value':items[0]['value'],
                              'pad_center_span_mm':math.dist(items[0]['xy'],items[1]['xy']),
                              'electrical_delay_qualified':False})
    if len(found)!=1:
        raise ValueError('Expected exactly one declared series component joining '+str(nets)+', found '+str(len(found)))
    return found


def reference_audit(route,geometry):
    layers=geometry['layers']
    mapping={'F.Cu':'In1.Cu','B.Cu':'In2.Cu' if len(layers)==4 else 'In4.Cu'}
    if layers==['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu']:
        mapping['In3.Cu']='In4.Cu'
    # This projection is a necessary geometry check, not an impedance model.
    # In3 also faces In2 power; field distribution/delay remains a release gate.
    result={'mapping':mapping,'missing_reference':[],'foreign_reference_copper':[],
            'transition_return_vias':[],'unsupported_signal_layers':[]}
    for edge in route['edges']:
        if edge['kind']!='track':
            continue
        layer=mapping.get(edge['layer'])
        if layer not in layers:
            result['unsupported_signal_layers'].append(edge['layer'])
            continue
        line=LineString([edge['start'],edge['end']])
        ground=geometry['ground'].get(layer,Polygon())
        uncovered=line.difference(ground.buffer(-edge['width_mm']/2))
        if uncovered.length>0.001:
            result['missing_reference'].append({'track':edge['object'],'reference_layer':layer,
                'uncovered_centerline_mm':round(uncovered.length,6),'locations':geometry_points(uncovered)})
        for t in geometry['tracks']:
            if t['layer']==layer and t['net']!='GND' and line.buffer(edge['width_mm']/2).intersects(t['line'].buffer(t['width_mm']/2)):
                result['foreign_reference_copper'].append({'signal_track':edge['object'],'reference_track':t['id'],
                    'reference_layer':layer,'reference_net':t['net'],'locations':geometry_points(line.intersection(t['line'].buffer((t['width_mm']+edge['width_mm'])/2)))})
    for via_id in route['via_ids']:
        via=next(v for v in geometry['vias'] if v['id']==via_id)
        grounds=[v for v in geometry['vias'] if v['net']=='GND']
        nearest=min(grounds,key=lambda v:math.dist(via['xy'],v['xy']),default=None)
        distance=math.dist(via['xy'],nearest['xy']) if nearest else None
        result['transition_return_vias'].append({'signal_via':via_id,'xy':via['xy'],
            'nearest_ground_via':nearest['id'] if nearest else None,'distance_mm':distance,
            'project_review_target_mm':1.0,'within_target':distance is not None and distance<=1.0,
            'qualification':'proximity diagnostic only; plane connectivity and return inductance require review'})
    result['unsupported_signal_layers']=sorted(set(result['unsupported_signal_layers']))
    result['continuous_projected_ground']=not(result['missing_reference'] or result['unsupported_signal_layers'])
    return result


def coupling_diagnostic(a,b,step=0.25,max_gap=0.30):
    partner=[e for e in b['edges'] if e['kind']=='track']
    total,uncoupled,worst=0.0,0.0,0.0
    runs=[]
    for edge in a['edges']:
        if edge['kind']!='track':
            continue
        line=LineString([edge['start'],edge['end']]);length=line.length
        count=max(1,math.ceil(length/step));unit=length/count
        dx,dy=edge['end'][0]-edge['start'][0],edge['end'][1]-edge['start'][1]
        candidates=[p for p in partner if p['layer']==edge['layer']]
        bad=0
        for i in range(count):
            point=line.interpolate((i+0.5)*unit)
            best=None
            for p in candidates:
                other=LineString([p['start'],p['end']]);ox,oy=p['end'][0]-p['start'][0],p['end'][1]-p['start'][1]
                parallel=abs(dx*ox+dy*oy)/(length*other.length)>=math.cos(math.radians(15))
                gap=point.distance(other)-(edge['width_mm']+p['width_mm'])/2
                if parallel and (best is None or gap<best):
                    best=gap
            total+=unit
            if best is None or best>max_gap:
                uncoupled+=unit;bad+=1
            if best is not None:
                worst=max(worst,best)
        if bad:
            runs.append({'track':edge['object'],'layer':edge['layer'],'start':edge['start'],'end':edge['end'],
                         'sampled_unpaired_mm':round(bad*unit,6)})
    return {'sample_step_max_mm':step,'project_edge_gap_review_target_mm':max_gap,'parallel_angle_tolerance_deg':15,
            'sampled_length_mm':total,'sampled_unpaired_mm':uncoupled,'worst_parallel_edge_gap_mm':worst,'runs':runs,
            'qualification':'Geometry diagnostic; no field-solver coupling or impedance claim. Inspect endpoints and bends manually.'}


def audit(path):
    source=Path(path);digest=hashlib.sha256(source.read_bytes()).hexdigest()
    g=load_geometry(source)
    report={'schema_version':1,'success':False,'source_pcb_sha256':digest,'pcbnew_version':g['pcbnew_version'],
            'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'copper_layers':g['layers'],
            'errors':g['errors'],'pairs':[],
            'qualification_blockers':['Fabricator-approved dielectric/impedance model and propagation-delay extraction absent.',
                'Via barrel/stub delay and component internal delay are not modeled; planar matching is only a geometric prerequisite.',
                'Coupling, escape symmetry and return-via proximity diagnostics require object-specific engineering review.',
                'I210 PCIe routing requirements require vendor confirmation; MG PHY-specific acceptance also remains open.',
                'RGMII, SDIO and other single-ended timing groups require separate timing/constraint validation.']}
    for spec in definitions():
        pair={k:v for k,v in spec.items() if k not in ('p','n')};pair['errors']=[]
        for polarity in ('p','n'):
            start,end,nets=spec[polarity]
            tracks=[t for t in g['tracks'] if t['net'] in nets]
            pads=[p for p in g['pads'] if p['net'] in nets]
            vias=[v for v in g['vias'] if v['net'] in nets]
            try:
                graph=RouteGraph(tracks,pads,vias,series_links(pads,nets))
                pair['errors']+=graph.errors
                route=graph.route(start,end)
                route['nets']=nets
                route['net_total_track_length_mm']=sum(t['line'].length for t in tracks)
                route['reference']=reference_audit(route,g)
                pair[polarity]=route
            except Exception as error:
                pair['errors'].append(polarity+': '+str(error))
        if 'p' in pair and 'n' in pair:
            p,n=pair['p'],pair['n']
            pair['planar_skew_mm']=abs(p['planar_length_mm']-n['planar_length_mm'])
            pair['planar_length_gate_pass']=bool(pair['planar_skew_mm']<spec['skew_limit_mm'] and max(p['planar_length_mm'],n['planar_length_mm'])<spec['max_length_mm'])
            pair['net_total_skew_diagnostic_mm']=abs(p['net_total_track_length_mm']-n['net_total_track_length_mm'])
            pair['via_symmetry_pass']=p['via_count']==n['via_count']
            pair['layer_sequence_symmetry_pass']=p['layer_sequence']==n['layer_sequence']
            pair['via_budget_pass']=spec['max_vias'] is None or max(p['via_count'],n['via_count'])<=spec['max_vias']
            pair['coupling']={'p_to_n':coupling_diagnostic(p,n),'n_to_p':coupling_diagnostic(n,p)}
            if spec.get('intel_mdi_limit_mm'):
                pair['intel_mdi_length_gate_pass']=pair['planar_skew_mm']<=spec['intel_mdi_limit_mm']
            pair['object_reviews_required']=[]
            for polarity,route in [('p',p),('n',n)]:
                if route['off_path_track_length_mm']>0.01:
                    pair['object_reviews_required'].append(polarity+': off-path branches/loops require inspection and stub removal where applicable')
                if any(not v['within_target'] for v in route['reference']['transition_return_vias']):
                    pair['object_reviews_required'].append(polarity+': return-via proximity target exceeded')
            if any(item['sampled_unpaired_mm']>0.01 for item in pair['coupling'].values()):
                pair['object_reviews_required'].append('Uncoupled path portions require escape/bend/coupling qualification with object-specific evidence')
            pair['geometric_prerequisites_pass']=all([pair['planar_length_gate_pass'],pair['via_symmetry_pass'],
                pair['layer_sequence_symmetry_pass'],pair['via_budget_pass'],not pair['errors'],
                p['reference']['continuous_projected_ground'],n['reference']['continuous_projected_ground'],not pair['object_reviews_required']])
        else:
            pair['geometric_prerequisites_pass']=False
        report['pairs'].append(pair)
    if hashlib.sha256(source.read_bytes()).hexdigest()!=digest:
        report['errors'].append('PCB changed during audit')
    report['summary']={'pairs':len(report['pairs']),'complete_paths':sum('p' in p and 'n' in p for p in report['pairs']),
        'geometric_prerequisites_pass':sum(p['geometric_prerequisites_pass'] for p in report['pairs']),
        'planar_skew_failures':[p['name'] for p in report['pairs'] if 'planar_length_gate_pass' in p and not p['planar_length_gate_pass']]}
    report['success']=not report['errors'] and not report['qualification_blockers'] and all(p['geometric_prerequisites_pass'] for p in report['pairs'])
    return report


def report_json(value):
    # Shapely geometry calculations can return NumPy scalar types.
    import numpy as np
    def native(item):
        if isinstance(item,np.generic):
            return item.item()
        raise TypeError('Unsupported report value '+type(item).__name__)
    return json.dumps(value,indent=2,default=native)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pcb',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=audit(args.pcb)
        encoded=report_json(result)
    except Exception as error:
        result={'schema_version':1,'success':False,'errors':[str(error)]}
        encoded=report_json(result)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    temporary=args.output.with_suffix(args.output.suffix+'.tmp')
    temporary.write_text(encoded,encoding='utf-8');temporary.replace(args.output)
    print(json.dumps({'success':result['success'],'summary':result.get('summary'),'errors':result['errors']}))
    return 0 if result['success'] else 1


if __name__=='__main__':
    raise SystemExit(main())

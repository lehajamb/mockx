#!/usr/bin/env python3
"""Extract all rail trace/barrel branches and solve DC nodal current distribution.

Conductor-only engineering estimate, not a field solver or thermal qualification.
Pads/contact spreading, connector resistance and semiconductor drops are excluded.
Copper thickness/temperature assumptions are explicit and intentionally pessimistic.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve


def solve_resistors(edges,sources,loads):
    """Positive voltages denote drop from ideal source; loads are positive amperes."""
    sources=set(sources);adj=defaultdict(set)
    for a,b,r in edges:
        if r<=0 or not math.isfinite(r):raise ValueError('Invalid resistance')
        adj[a].add(b);adj[b].add(a)
    reached=set(sources);stack=list(sources)
    while stack:
        for n in adj[stack.pop()]-reached:reached.add(n);stack.append(n)
    if any(n not in reached for n in loads):raise ValueError('Disconnected required load')
    nodes=sorted(reached-sources,key=str);index={n:i for i,n in enumerate(nodes)}
    row=[];col=[];data=[];rhs=np.array([loads.get(n,0.0) for n in nodes])
    for a,b,r in edges:
        if a not in reached or b not in reached:continue
        g=1/r
        for x,y in [(a,b),(b,a)]:
            if x in index:
                row.append(index[x]);col.append(index[x]);data.append(g)
                if y in index:row.append(index[x]);col.append(index[y]);data.append(-g)
    values=spsolve(coo_matrix((data,(row,col)),shape=(len(nodes),len(nodes))).tocsr(),rhs)
    volts={**{n:0.0 for n in sources},**dict(zip(nodes,values))}
    currents=[(volts.get(a,0)-volts.get(b,0))/r for a,b,r in edges]
    if not all(math.isfinite(v) for v in volts.values()):raise ValueError('Singular network')
    return volts,currents


class Union:
    def __init__(self):self.parents={}
    def find(self,n):
        self.parents.setdefault(n,n)
        if self.parents[n]!=n:self.parents[n]=self.find(self.parents[n])
        return self.parents[n]
    def join(self,a,b):self.parents[self.find(b)]=self.find(a)


def extract(geometry,net,rho,thickness,plating):
    from high_speed_audit import RouteGraph
    from shapely.geometry import Point
    tracks=[t for t in geometry['tracks'] if t['net']==net]
    pads=[p for p in geometry['pads'] if p['net']==net]
    vias=[v for v in geometry['vias'] if v['net']==net]
    graph=RouteGraph(tracks,pads,vias)
    union=Union();ends={}
    for a,items in graph.adj.items():
        for b,_,i in items:ends[i]=(a,b)
    for i,e in enumerate(graph.edges):
        if e['kind'] in ('pad_copper','via_copper'):union.join(*ends[i])
    # Two same-net pads can touch even without an explicit track between them.
    for i,a in enumerate(pads):
        for b in pads[i+1:]:
            if set(a['layers'])&set(b['layers']) and a['shape'].intersects(b['shape']):
                union.join(('pad',a['id']),('pad',b['id']))
    # A via contained in a same-net pad has direct copper connectivity.
    for v in vias:
        for p in pads:
            if p['shape'].covers(Point(v['xy'])):
                for layer in set(p['layers'])&set(v['layers']):
                    union.join(('pad',p['id']),(net,layer,round(v['xy'][0],6),round(v['xy'][1],6)))
    z=dict(zip(geometry['layers'],[.0175,.1525,.4375,1.1625,1.4475,1.5825]))
    if len(z)!=6:raise ValueError('Only explicitly qualified six-layer candidate supported')
    byvia={v['id']:v for v in vias};edges=[];details=[];seen={}
    for i,e in enumerate(graph.edges):
        if e['kind'] not in ('track','via'):continue
        a,b=map(union.find,ends[i])
        if a==b:continue
        if e['kind']=='track':r=rho*e['length_mm']/(e['width_mm']*thickness)
        else:
            via=byvia[e['object']];length=abs(z[e['layers'][1]]-z[e['layers'][0]])
            r=rho*length/(math.pi*(via['drill_mm']+plating)*plating)
        pair=tuple(sorted((a,b),key=str))
        # Coincident centerline segments represent one piece of copper, not parallel wires.
        identity=(pair,e['kind'])
        if identity in seen:
            j=seen[identity]
            if r>edges[j][2]:edges[j]=(a,b,r);details[j]=e
            continue
        seen[identity]=len(edges);edges.append((a,b,r));details.append(e)
    return edges,details,union,pads,graph.errors


def evaluate(geometry,net,source_ids,sink_ids,current,rho,thickness,plating,short_sinks=True):
    edges,details,union,pads,warnings=extract(geometry,net,rho,thickness,plating)
    sources=[union.find(('pad',p)) for p in source_ids]
    sinks=[union.find(('pad',p)) for p in sink_ids]
    if short_sinks:
        merge=Union()
        for n in sinks:merge.join(sinks[0],n)
        edges2=[];details2=[]
        for (a,b,r),d in zip(edges,details):
            a,b=merge.find(a),merge.find(b)
            if a!=b:edges2.append((a,b,r));details2.append(d)
        edges,details=edges2,details2;sources=list(map(merge.find,sources));sinks=list(map(merge.find,sinks))
    loads={sinks[0]:current}
    volts,currents=solve_resistors(edges,sources,loads)
    contributions=[]
    for (a,b,r),d,i in zip(edges,details,currents):
        if abs(i)<1e-8:continue
        contributions.append({**d,'ohms':r,'current_a':abs(i),'power_w':i*i*r,
                              'drop_v':abs(i*r),'load_fraction':abs(i)/current})
    contributions.sort(key=lambda x:x['power_w'],reverse=True)
    return {'net':net,'source_pads':source_ids,'sink_pads':sink_ids,'load_a':current,
            'terminal_assumption':'sink pads shorted by ideal module/package' if short_sinks else 'entire rail load at this single pin; other pads unloaded',
            'voltage_drop_v':volts[sinks[0]],'equivalent_ohms':volts[sinks[0]]/current,
            'total_copper_loss_w':sum(x['power_w'] for x in contributions),
            'shared_full_current_sections':[x for x in contributions if x['load_fraction']>.999],
            'all_current_carrying_sections':contributions,'graph_warnings':warnings,
            'trace_count':sum(x['kind']=='track' for x in details),'barrel_section_count':sum(x['kind']=='via' for x in details)}


def main():
    from high_speed_audit import load_geometry
    from power_budget import calculate
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('board',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();geometry=load_geometry(args.board)
    temperature=85;rho=1.724e-5*(1+.00393*(temperature-20));thickness=.025;plating=.020
    cases=[]
    padmap={p['id']:p for p in geometry['pads']}
    def ids(ref,net):return [p['id'] for p in geometry['pads'] if p['ref']==ref and p['net']==net]
    definitions=[('VBUS_IN',ids('J3','VBUS_IN'),ids('Q1','VBUS_IN'),calculate(12.5)['input_w']/15),
                 ('VIN_PROT',ids('Q1','VIN_PROT'),ids('U3','VIN_PROT'),calculate(12.5)['input_w']/15),
                 ('+5V',['L1:2'],ids('J1','+5V'),calculate(12.5)['+5V'][1])]
    for net,source,sink,current in definitions:
        if not source or not sink:raise ValueError('Missing terminals '+net)
        cases.append(evaluate(geometry,net,source,sink,current,rho,thickness,plating))
        if net=='+5V':
            for pin in sink:cases.append(evaluate(geometry,net,source,[pin],current,rho,thickness,plating,False))
    output={'board':str(args.board),'board_sha256':hashlib.sha256(args.board.read_bytes()).hexdigest(),
            'assumptions':{'temperature_c':temperature,'copper_thickness_mm':thickness,'barrel_plating_mm':plating,
            'rho_ohm_mm':rho,'nominal_board_thickness_mm':1.6,'source_revision':'current board hash',
            'pads':'ideal equipotential copper; spreading/contact/connector resistance excluded',
            'zones':'No power zones modeled; GND return drop excluded',
            'thermal':'85C is a scenario, not predicted self heating or an approved temperature',
            'interpretation':'Full trace/barrel network. Conservative material assumptions but NOT a mathematical upper bound on complete rail drop because pads/contacts/ground are omitted.',
            'unknown_module_division':'All pads ideal-short common-load solution plus one-at-a-time worst allocation scenarios; no assumed equal division.'},
            'cases':cases}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(output,indent=2)+'\n')
    for c in cases:print(c['net'],c['sink_pads'],round(c['voltage_drop_v'],6),'V',round(c['total_copper_loss_w'],6),'W',len(c['shared_full_current_sections']),'shared sections')
if __name__=='__main__':main()

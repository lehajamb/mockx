"""Pure report checks for the narrowly scoped PCIe TX/clock copper correction.

A passing result permits this routing checkpoint to enter subsequent repair
stages. It is never a fabrication or signal-integrity approval.
"""
import math
import re
from collections import Counter
from verification_core import parse_drc
PCIE_PAIRS={'PCIE_TX','PCIE_CLK'}
PCIE_NETS={'PCIE_TX_P','PCIE_TX_N','PCIE_CLK_P','PCIE_CLK_N'}


def _assess_candidate(report,drc_text,pair_names,net_names):
    failures=[];native=[];native_counts={}
    pairs=report.get('pairs') if isinstance(report,dict) else None
    if not isinstance(pairs,list) or any(not isinstance(p,dict) for p in pairs):
        failures.append('Malformed endpoint report');pairs=[]
    selected=[p for p in pairs if p.get('name') in pair_names]
    if len(selected)!=len(pair_names) or {p['name'] for p in selected}!=pair_names:
        failures.append('Required pairs must be present exactly once: '+', '.join(sorted(pair_names)))
    if not isinstance(report,dict) or not isinstance(report.get('errors'),list) or report['errors']:
        failures.append('Geometry extraction errors or missing error status')
    for pair in selected:
        name=pair['name']
        if not isinstance(pair.get('errors'),list) or pair['errors']:
            failures.append(name+': path graph errors or missing error status')
        if not all(isinstance(pair.get(c),dict) for c in ('p','n')):
            failures.append(name+': incomplete endpoints');continue
        for flag in ('planar_length_gate_pass','via_symmetry_pass','via_budget_pass','layer_sequence_symmetry_pass'):
            if pair.get(flag) is not True:failures.append(name+': '+flag+' must be explicitly true')
        for polarity in ('p','n'):
            value=pair[polarity].get('off_path_track_length_mm')
            if type(value) not in (int,float) or not math.isfinite(value) or value<0 or value>0.01:
                failures.append(name+': invalid or excessive off-path '+polarity+' copper')
    try:
        parsed=parse_drc(drc_text)
        native_counts=parsed['counts']
        for finding in parsed['findings']:
            severity=re.search(r'\bSeverity: (error|warning)\b',finding['objects'])
            if severity is None:raise ValueError('Finding lacks recognized severity')
            objects=finding['objects']
            relevant=any(net in objects for net in net_names)
            if relevant and (severity[1]=='error' or finding['section']=='unconnected pads'):
                native.append(finding)
    except (TypeError,ValueError) as error:
        failures.append('Invalid native DRC report: '+str(error))
    if native:failures.append('Native DRC reported a PCIe error or required open')
    return {'targeted_geometric_checks_pass':not failures,'fabrication_ready':False,
            'failures':failures,'native_pcie_errors':native,'native_counts':native_counts,
            'not_qualified':['layer-specific impedance and delay','escape/coupling discontinuities',
                             'reference transitions and return inductance','whole-board electrical and mechanical release gates']}

def assess_pcie_candidate(report,drc_text):
    return _assess_candidate(report,drc_text,PCIE_PAIRS,PCIE_NETS)


def assess_pcie_rx_candidate(report,drc_text,before_drc_text):
    result=_assess_candidate(report,drc_text,{'PCIE_RX'},{'PCIE_RX_P','PCIE_RX_N','OE_PET_P','OE_PET_N'})
    pairs=report.get('pairs') if isinstance(report,dict) else None
    for pair in pairs if isinstance(pairs,list) else []:
        if not isinstance(pair,dict) or pair.get('name')!='PCIE_RX':continue
        for polarity in ('p','n'):
            path=pair.get(polarity,{})
            if not isinstance(path,dict) or type(path.get('via_count')) is not int or path['via_count']!=1 or path.get('layer_sequence')!=['B.Cu','F.Cu']:
                result['failures'].append('RX correction must retain the original single-via B/F topology')
    try:
        before=parse_drc(before_drc_text);after=parse_drc(drc_text)
        def counts(native):
            count=Counter()
            for finding in native['findings']:
                if finding['section']=='unconnected pads':
                    nets=set(re.findall(r'\[([^]]+)\]',finding['objects']))
                    if len(nets)!=1:raise ValueError('Cannot bind native open to one exact net')
                    count[next(iter(nets))]+=1
            return count
        old,new=counts(before),counts(after)
        for net,value in new.items():
            if value>old[net]:result['failures'].append('New required open on '+net)
        old_nonopen={f['fingerprint'] for f in before['findings'] if f['section']!='unconnected pads'}
        if any(f['fingerprint'] not in old_nonopen for f in after['findings'] if f['section']!='unconnected pads'):
            result['failures'].append('New native non-open finding')
    except (TypeError,ValueError) as error:
        result['failures'].append('Invalid native before/after comparison: '+str(error))
    result['targeted_geometric_checks_pass']=not result['failures']
    return result

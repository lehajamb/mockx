"""Fail-closed acceptance of dedicated required-power completion."""
from collections import Counter
import re

def open_counts(report):
    counts=Counter()
    for finding in report['findings']:
        if finding['section']!='unconnected pads':continue
        nets=set(re.findall(r'\[([^]\n]+)\]',finding['objects']))
        if not nets:raise ValueError('Unconnected report lacks an identifiable net')
        for net in nets:counts[net]+=1
    return counts

def accept_power_change(before,after,new_other,required_nets):
    old,new=open_counts(before),open_counts(after)
    increased={n:{'before':old[n],'after':count}for n,count in new.items()if count>old[n]}
    remaining={n:new[n]for n in required_nets if new[n]}
    detail={'before_open_by_net':dict(old),'after_open_by_net':dict(new),
            'increased_open_by_net':increased,'required_power_opens':remaining}
    return not new_other and not increased and not remaining,detail

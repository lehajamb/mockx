"""Minimal S-expression reader/writer for KiCad files."""
import re
class Sym(str): pass
_tok = re.compile(r'\s*(\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+)')
def parse(text):
    pos = 0; stack = [[]]
    n = len(text)
    while True:
        m = _tok.match(text, pos)
        if not m: break
        t = m.group(1); pos = m.end()
        if t == '(': stack.append([])
        elif t == ')':
            l = stack.pop(); stack[-1].append(l)
        elif t[0] == '"': stack[-1].append(t[1:-1].replace('\\"', '"').replace('\\\\', '\\'))
        else: stack[-1].append(Sym(t))
    return stack[0][0]
def q(s): return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'
def dump(x, ind=0):
    if isinstance(x, list):
        simple = all(not isinstance(e, list) for e in x)
        if simple or len(dump_flat(x)) < 100: return dump_flat(x)
        pad = '  ' * (ind + 1)
        head = dump(x[0]) if x else ''
        rest = x[1:]
        out = '(' + head
        i = 0
        while i < len(rest) and not isinstance(rest[i], list):
            out += ' ' + dump(rest[i]); i += 1
        for e in rest[i:]:
            out += '\n' + pad + dump(e, ind + 1)
        return out + '\n' + '  ' * ind + ')'
    if isinstance(x, Sym): return str(x)
    if isinstance(x, (int,)): return str(x)
    if isinstance(x, float): return ('%.4f' % x).rstrip('0').rstrip('.') if x == x else '0'
    return q(x)
def dump_flat(x):
    if isinstance(x, list): return '(' + ' '.join(dump_flat(e) for e in x) + ')'
    return dump(x)
def find(x, key):
    return [e for e in x if isinstance(e, list) and e and e[0] == key]
def first(x, key):
    r = find(x, key); return r[0] if r else None

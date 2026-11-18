"""Access to KiCad 7 symbol libraries: flattened symbols and pin tables."""
import os, copy
from sx import parse, find, first, Sym
SYMDIR = '/usr/share/kicad/symbols'
_cache = {}
def _lib(lib):
    if lib not in _cache:
        root = parse(open(os.path.join(SYMDIR, lib + '.kicad_sym')).read())
        _cache[lib] = {s[1]: s for s in find(root, 'symbol')}
    return _cache[lib]
def get_symbol(lib, name):
    """Return flattened symbol s-expr named 'lib:name' (extends resolved)."""
    L = _lib(lib); s = copy.deepcopy(L[name])
    ext = first(s, 'extends')
    if ext:
        par = get_symbol(lib, ext[1])
        base = copy.deepcopy(par); base[1] = name
        pname = ext[1]
        for sub in find(base, 'symbol'):
            sub[1] = name + sub[1][len(pname):]
        props = {p[1]: p for p in find(s, 'property')}
        for i, e in enumerate(base):
            if isinstance(e, list) and e and e[0] == 'property' and e[1] in props:
                base[i] = props.pop(e[1])
        for p in props.values(): base.append(p)
        s = base
    return s
def pins(sym):
    """List of dict(unit, number, name, type, x, y, angle, hide)."""
    out = []
    for sub in find(sym, 'symbol'):
        parts = sub[1].rsplit('_', 2); unit = int(parts[-2])
        if int(parts[-1]) > 1: continue
        for p in find(sub, 'pin'):
            at = first(p, 'at')
            out.append(dict(unit=unit, number=first(p, 'number')[1], name=first(p, 'name')[1], type=str(p[1]),
                            x=float(at[1]), y=float(at[2]), angle=int(float(at[3])) if len(at) > 3 else 0,
                            hide=any(e == 'hide' for e in p if not isinstance(e, list))))
    return out
def prop(sym, key):
    for p in find(sym, 'property'):
        if p[1] == key: return p[2]
    return ''
if __name__ == '__main__':
    import sys
    for spec in sys.argv[1:]:
        lib, name = spec.split(':'); s = get_symbol(lib, name); P = pins(s)
        print('##', spec, 'fp=', prop(s, 'Footprint'), 'units=', sorted({p['unit'] for p in P}))
        print(' '.join('%s=%s%s' % (p['number'], p['name'], '' if p['type'] in ('passive','bidirectional','input') else '/' + p['type'][:3]) for p in sorted(P, key=lambda p: (len(p['number']), p['number']))))

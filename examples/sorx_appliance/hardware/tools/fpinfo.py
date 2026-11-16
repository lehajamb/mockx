import pcbnew
FP='/usr/share/kicad/footprints/'
def bb(f, layer):
    xs=[];ys=[]
    for g in f.GraphicalItems():
        if g.GetLayer()==layer and isinstance(g, pcbnew.FP_SHAPE):
            b=g.GetBoundingBox(); xs+=[b.GetLeft(),b.GetRight()]; ys+=[b.GetTop(),b.GetBottom()]
    if not xs: return None
    return tuple(round(pcbnew.ToMM(v),2) for v in (min(xs),max(xs),min(ys),max(ys)))
if __name__=='__main__':
    import sys
    for spec in sys.argv[1:]:
        lib,name=spec.split(':')
        f=pcbnew.FootprintLoad(FP+lib+'.pretty',name)
        print(name[:30],'| fab',bb(f,pcbnew.F_Fab),'| crt',bb(f,pcbnew.F_CrtYd),'| dwg',bb(f,pcbnew.Dwgs_User), bb(f,pcbnew.Cmts_User))

"""Move obstructed board labels locally without changing copper or footprints."""
import argparse, hashlib, json, shutil, subprocess, sys
from pathlib import Path

def main():
    import pcbnew
    from shapely.geometry import box
    from shapely.ops import unary_union
    from shapely.affinity import translate
    from high_speed_audit import polygons
    from verification_core import parse_drc
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('source',type=Path);ap.add_argument('output',type=Path);ap.add_argument('evidence',type=Path);a=ap.parse_args()
    if a.output.exists() or a.evidence.exists():raise ValueError('Fresh paths required')
    a.output.parent.mkdir(parents=True,exist_ok=True);a.evidence.mkdir(parents=True)
    for name in ('fp-lib-table',):
        src=a.source.parent/name
        if src.exists():shutil.copyfile(src,a.output.parent/name)
    for suffix in ('.kicad_pro','.kicad_dru'):
        src=a.source.with_suffix(suffix)
        if src.exists():shutil.copyfile(src,a.output.with_suffix(suffix))
    b=pcbnew.LoadBoard(str(a.source));pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.WriteDRCReport(b,str(a.evidence/'before.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True)
    def bounds(item):
        r=item.GetBoundingBox();return box(r.GetLeft()/1e6,r.GetTop()/1e6,r.GetRight()/1e6,r.GetBottom()/1e6)
    obstacle=[]
    for fp in b.GetFootprints():
        fab=[]
        for pad in fp.Pads():
            if pad.IsOnLayer(pcbnew.F_Mask):obstacle.extend(polygons(pad.GetEffectivePolygon()))
        for g in fp.GraphicalItems():
            if g.GetLayer()==pcbnew.F_SilkS:obstacle.append(bounds(g))
            if g.GetLayer()==pcbnew.F_Fab and fp.GetLayer()==pcbnew.F_Cu:fab.append(bounds(g))
        if fab:obstacle.append(unary_union(fab).envelope)
        if fp.Reference().IsVisible() and fp.Reference().GetLayer()==pcbnew.F_SilkS:obstacle.append(bounds(fp.Reference()))
    drawings=list(b.GetDrawings());texts=[x for x in drawings if x.GetClass()=='PCB_TEXT' and x.GetLayer()==pcbnew.F_SilkS]
    for x in drawings:
        if x.GetLayer()==pcbnew.F_SilkS and x.GetClass()!='PCB_TEXT':obstacle.append(bounds(x))
    fixed=unary_union(obstacle).buffer(.15)
    targets={'PWR 15-20V PD','SD','Verdin iMX8M Plus  J1','CR2032 +','DEBUG','PPS IN','FAN','ALARM','USB 2 / REC'}
    edge=b.GetBoardEdgesBoundingBox();inside=box(edge.GetLeft()/1e6+.5,edge.GetTop()/1e6+.5,edge.GetRight()/1e6-.5,edge.GetBottom()/1e6-.5)
    changes=[];unresolved=[]
    offsets=sorted(((i*.5,j*.5) for i in range(-16,17) for j in range(-16,17) if i*i+j*j<=256),key=lambda p:(p[0]*p[0]+p[1]*p[1],abs(p[0]),p[1],p[0]))
    for text in texts:
        if text.GetText() not in targets:continue
        shape=bounds(text);others=unary_union([bounds(t).buffer(.15) for t in texts if t is not text]);taken=fixed.union(others)
        chosen=None
        for dx,dy in offsets:
            proposed=translate(shape,dx,dy)
            if inside.covers(proposed) and not taken.intersects(proposed):chosen=(dx,dy);break
        if chosen is None:unresolved.append(text.GetText());continue
        dx,dy=chosen
        if not dx and not dy:continue
        pos=text.GetPosition();dest=pcbnew.VECTOR2I(pos.x+round(dx*1e6),pos.y+round(dy*1e6))
        changes.append({'uuid':text.m_Uuid.AsString(),'text':text.GetText(),'before_mm':[pos.x/1e6,pos.y/1e6],'after_mm':[dest.x/1e6,dest.y/1e6]});text.SetPosition(dest)
    pcbnew.SaveBoard(str(a.output),b)
    subprocess.run([sys.executable,__file__,'--check',str(a.output),str(a.evidence/'after.txt')],check=True)
    before=parse_drc((a.evidence/'before.txt').read_text());after=parse_drc((a.evidence/'after.txt').read_text());old={f['fingerprint'] for f in before['findings'] if f['section']!='unconnected pads'}
    new=[f for f in after['findings'] if f['section']!='unconnected pads' and f['fingerprint'] not in old]
    accepted=not new and before['counts']['unconnected pads']==after['counts']['unconnected pads']
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    result={'accepted':accepted,'source_sha256':sha(a.source),'output_sha256':sha(a.output),'tool_sha256':sha(Path(__file__)),'changes':changes,'unresolved':unresolved,'before':before['counts'],'after':after['counts'],'new_findings':new,'fabrication_ready':False}
    (a.evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    return 0 if accepted else 1
if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--check':
        import pcbnew
        b=pcbnew.LoadBoard(sys.argv[2]);pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.SaveBoard(sys.argv[2],b);pcbnew.WriteDRCReport(b,sys.argv[3],pcbnew.EDA_UNITS_MILLIMETRES,True)
    else:raise SystemExit(main())

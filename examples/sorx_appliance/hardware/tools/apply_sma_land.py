"""Apply only the dimension-checked SMA pad correction to a fresh checkpoint."""
import argparse, hashlib, json, math, os, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from verification_core import parse_drc
from gen_sma import NAME

def candidate_passed(before,after,new_other):
    return not new_other and after["unconnected pads"] <= before["unconnected pads"]

def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def finish(path, report):
    import pcbnew
    board=pcbnew.LoadBoard(str(path)); pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(path),board)
    f=board.FindFootprintByReference('J5')
    for pad in f.Pads():
        expected=-.36 if pad.GetNumber()=='1' else -.51
        if abs(pad.GetPos0().x/1e6-expected)>1e-6:raise ValueError('SMA local pad position failed roundtrip')
    pcbnew.WriteDRCReport(board,str(report),pcbnew.EDA_UNITS_MILLIMETRES,True)
def main():
    import pcbnew
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument('source',type=Path);ap.add_argument('output',type=Path);ap.add_argument('evidence',type=Path);a=ap.parse_args()
    if a.output.exists() or a.evidence.exists():raise ValueError('Fresh paths required')
    a.evidence.mkdir(parents=True); a.output.parent.mkdir(parents=True,exist_ok=True)
    b=pcbnew.LoadBoard(str(a.source)); f=b.FindFootprintByReference('J5')
    if str(f.GetFPID().GetLibItemName())!='SMA_Amphenol_132289_EdgeMount':raise ValueError('Unexpected source footprint')
    pads=list(f.Pads())
    if len(pads)!=5:raise ValueError('Expected five SMA copper pads')
    pcbnew.ZONE_FILLER(b).Fill(b.Zones());pcbnew.WriteDRCReport(b,str(a.evidence/'before.txt'),pcbnew.EDA_UNITS_MILLIMETRES,True)
    changes=[]; angle=math.radians(f.GetOrientationDegrees())
    for pad in pads:
        signal=pad.GetNumber()=='1';size=pad.GetSize();pos=pad.GetPosition()
        if (size.x,size.y)!=(1500000,5080000):raise ValueError('Unexpected original pad dimensions')
        dx=-.36 if signal else -.51
        dest=pcbnew.VECTOR2I(pos.x+round(dx*math.cos(angle)*1e6),pos.y-round(dx*math.sin(angle)*1e6))
        changes.append({'uuid':pad.m_Uuid.AsString(),'pad':pad.GetNumber(),'net':pad.GetNetname(),'before_nm':[pos.x,pos.y,size.x,size.y],'after_nm':[dest.x,dest.y,1500000 if signal else 1600000,5200000 if signal else 4900000]})
        pad.SetPosition(dest);pad.SetLocalCoord();pad.SetSize(pcbnew.VECTOR2I(1500000 if signal else 1600000,5200000 if signal else 4900000))
    replacement=pcbnew.FootprintLoad(str(Path(__file__).resolve().parents[1]/'hw/sorx.pretty'),NAME)
    replacement.SetFPID(pcbnew.LIB_ID('sorx',NAME));replacement.SetReference(f.GetReference());replacement.SetValue(f.GetValue())
    replacement.SetPosition(f.GetPosition());replacement.SetOrientation(f.GetOrientation());replacement.SetPath(f.GetPath())
    for new_text,old_text in ((replacement.Reference(),f.Reference()),(replacement.Value(),f.Value())):
        new_text.SetPosition(old_text.GetPosition());new_text.SetPos0(old_text.GetPos0())
        new_text.SetTextAngle(old_text.GetTextAngle());new_text.SetTextSize(old_text.GetTextSize());new_text.SetTextThickness(old_text.GetTextThickness())
        new_text.SetVisible(old_text.IsVisible());new_text.SetLayer(old_text.GetLayer())
    for pad in replacement.Pads():pad.SetNet(b.FindNet('PPS_RAW' if pad.GetNumber()=='1' else 'GND'))
    b.Add(replacement);b.Remove(f);pcbnew.SaveBoard(str(a.output),b)
    for suffix in ('.kicad_pro','.kicad_dru'):
        if a.source.with_suffix(suffix).exists():shutil.copy2(a.source.with_suffix(suffix),a.output.with_suffix(suffix))
    subprocess.run([sys.executable,__file__,'--finish',str(a.output),str(a.evidence/'after.txt')],check=True)
    before=parse_drc((a.evidence/'before.txt').read_text());after=parse_drc((a.evidence/'after.txt').read_text())
    old={f['fingerprint'] for f in before['findings'] if f['section']!='unconnected pads'}
    new=[f for f in after['findings'] if f['section']!='unconnected pads' and f['fingerprint'] not in old]
    result={'command':[sys.executable,*sys.argv],'utc':datetime.now(timezone.utc).isoformat(),'image':os.environ.get('SORX_IMAGE_ID'),'kicad':pcbnew.GetBuildVersion(),'source':str(a.source),'output':str(a.output),'source_sha256':sha(a.source),'output_sha256':sha(a.output),'tool_sha256':sha(__file__),'footprint_sha256':sha(Path(__file__).resolve().parents[1]/'hw/sorx.pretty'/(NAME+'.kicad_mod')),'before':before['counts'],'after':after['counts'],'new_other_findings':new,'pad_changes':changes,'fabrication_ready':False}
    (a.evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    return 0 if candidate_passed(before["counts"],after["counts"],new) else 1
if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--finish':finish(Path(sys.argv[2]),Path(sys.argv[3]))
    else:raise SystemExit(main())

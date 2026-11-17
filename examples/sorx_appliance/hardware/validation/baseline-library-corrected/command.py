"""Recheck immutable baseline with the corrected headless library environment."""
import collections,hashlib,json,os,subprocess,sys,tempfile,zipfile
from pathlib import Path,PurePosixPath
import pcbnew
sys.path.insert(0,"/work/hardware/tools")
from verification_core import parse_drc
out=Path(__file__).resolve().parent
archive=Path("/work/baseline/sorx-hw-2026-11-16-final.zip")
expected="2f51a36928e87cb51dcb15f3fa5f04b5b789473a67bb6b2b4529f671bc91addd"
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(archive)==expected
assert os.environ.get("KICAD7_FOOTPRINT_DIR")=="/usr/share/kicad/footprints"
with tempfile.TemporaryDirectory(prefix="sorx-codex-baseline-library-") as tmp:
 root=Path(tmp)
 with zipfile.ZipFile(archive) as z:
  for info in z.infolist():
   path=PurePosixPath(info.filename)
   if path.is_absolute() or ".." in path.parts or "\\" in info.filename or ":" in info.filename:
    raise ValueError("Unsafe archive path")
   if info.is_dir():continue
   dest=root.joinpath(*path.parts);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(z.read(info))
 pcb=root/"sorx-hw/hw/sorx.kicad_pcb"
 os.chdir(pcb.parent);b=pcbnew.LoadBoard(str(pcb));pcbnew.ZONE_FILLER(b).Fill(b.Zones())
 pcbnew.WriteDRCReport(b,str(out/"drc-refilled.txt"),pcbnew.EDA_UNITS_MILLIMETRES,True)
 report=parse_drc((out/"drc-refilled.txt").read_text());counts=collections.Counter(x["category"] for x in report["findings"])
 record={"archive_sha256":expected,"pcb_sha256":sha(pcb),"image":os.environ["SORX_IMAGE_ID"],"kicad":pcbnew.GetBuildVersion(),"environment":{"KICAD7_FOOTPRINT_DIR":os.environ["KICAD7_FOOTPRINT_DIR"]},"command":["python3","../validation/baseline-library-corrected/command.py"],"counts":report["counts"],"categories":dict(counts),"artifacts":{p.name:sha(p) for p in out.iterdir() if p.is_file()},"purpose":"Same-source baseline with corrected library resolution; historical255 findings remain unchanged evidence."}
 (out/"result.json").write_text(json.dumps(record,indent=2)+"\n")
 print(json.dumps(record))
 if counts.get("lib_footprint_issues",0) or report["counts"]["unconnected pads"]!=51:raise SystemExit(1)

import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"tools"))
from kicad_environment import kicad_environment
import pcbnew
env=kicad_environment()
assert os.environ["KICAD7_FOOTPRINT_DIR"] == env["KICAD7_FOOTPRINT_DIR"]
fp=pcbnew.FootprintLoad(env["KICAD7_FOOTPRINT_DIR"]+"/Capacitor_SMD.pretty", "C_0402_1005Metric")
assert fp is not None
result={"container_image":os.environ["SORX_IMAGE_ID"],"KICAD7_FOOTPRINT_DIR":env["KICAD7_FOOTPRINT_DIR"],"kicad_version":pcbnew.GetBuildVersion(),"loaded_footprint":"Capacitor_SMD:C_0402_1005Metric","pads":len(list(fp.Pads())),"success":True}
print(json.dumps(result,indent=2))
Path(__file__).with_name("launcher-smoke.json").write_text(json.dumps(result,indent=2)+"\n")

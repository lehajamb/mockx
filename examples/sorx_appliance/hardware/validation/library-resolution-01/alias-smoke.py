import json,hashlib
from pathlib import Path
import pcbnew
a=Path("/hardware/hw/sorx.pretty");b=Path("/work/hardware/hw/sorx.pretty");f=sorted(a.glob("*.kicad_mod"))[0]
assert hashlib.sha256(f.read_bytes()).digest()==hashlib.sha256((b/f.name).read_bytes()).digest()
fp=pcbnew.FootprintLoad(str(a),f.stem);assert fp is not None
mount=[line for line in Path("/proc/mounts").read_text().splitlines() if line.split()[1]=="/hardware/hw"]
assert len(mount)==1 and "ro" in mount[0].split()[3].split(",")
r={"alias":"/hardware/hw","same_clone_content":True,"read_only":True,"loaded_footprint":f.stem,"kicad":pcbnew.Version(),"success":True};print(json.dumps(r,indent=2));Path(__file__).with_name("alias-smoke.json").write_text(json.dumps(r,indent=2)+"\n")

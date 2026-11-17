"""Hash-checked Toradex solids, transformed from supplier to carrier coordinates.

The generic module maximum-height solids are conservative fit envelopes.
The uncompressed TIM is retained as a reference, never treated as rigid metal
or silently compressed to obtain a passing fit check.
"""
import hashlib
import json
from functools import lru_cache
from pathlib import Path
import cadquery as cq
VENDOR=Path(__file__).resolve().parents[1]/"mech/vendor/toradex"

def checked_asset(asset_id, root=VENDOR):
    root=Path(root)
    manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    entry=next(item for item in manifest["assets"] if item["id"]==asset_id)
    path=root/(asset_id+".step")
    actual=hashlib.sha256(path.read_bytes()).hexdigest()
    if actual!=entry["step_sha256"]:
        raise ValueError("Supplier geometry SHA-256 mismatch: "+asset_id)
    return path,actual

@lru_cache(maxsize=4)
def _solids(path, sha256):
    # The digest is a cache key; checked_asset is called before every cache access.
    return tuple(cq.importers.importStep(path).val().Solids())

def supplier_parts(x,y):
    path,sha=checked_asset("verdin-family-max")
    module=_solids(str(path),sha)
    if len(module)!=255:
        raise ValueError("Unexpected supplier module topology")
    fit=cq.Compound.makeCompound([module[i] for i in (0,249,250)])
    fit=fit.rotate((0,0,0),(0,0,1),180).translate((x+34.7945,-(y-7.926),3.7))
    path,sha=checked_asset("heatsink-type1-v11")
    heat=_solids(str(path),sha)
    if len(heat)!=6:
        raise ValueError("Unexpected supplier heatsink topology")
    def placed(solid):
        return cq.Workplane("XY").add(solid.rotate((0,0,0),(1,0,0),90).translate((x-0.0055,-(y+8.074),7.0)))
    parts={"M1":cq.Workplane("XY").add(fit),"HS1":placed(heat[0])}
    parts.update({ref:placed(heat[index]) for ref,index in (("SC1",2),("SC2",5),("SC3",4),("SC4",3))})
    return parts,placed(heat[1])

def provenance():
    return {str(p.relative_to(VENDOR)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(VENDOR.rglob("*")) if p.is_file()}

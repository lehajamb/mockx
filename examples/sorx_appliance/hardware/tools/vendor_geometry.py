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
    # Supplier screws all start at raw Y=2.85. Near metal seats are Y=3.00;
    # seat those individual screws 0.15mm higher, without altering any solid.
    for ref in ("SC3","SC4"):
        parts[ref]=parts[ref].translate((0,0,.15))
    return parts,placed(heat[1])

def socket_shape(x,y):
    root=VENDOR.parent/"te"
    manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    path=root/"2309409-2.step"; sha=hashlib.sha256(path.read_bytes()).hexdigest()
    if sha!=manifest["files"][path.name]:
        raise ValueError("Supplier geometry SHA-256 mismatch: TE2309409-2")
    solids=_solids(str(path),sha)
    if len(solids)!=1:raise ValueError("Unexpected TE socket topology")
    # 260 tail-bottom faces at supplier Y=-2.875 establish PCB top Z=0.
    # Peg axes raw Z=-20.85 map to carrier Y=J1.y-7.926, X=J1.x-.0055+-34.4.
    placed=solids[0].rotate((0,0,0),(1,0,0),90).translate((x-.0055,-(y+12.924),2.875))
    return cq.Workplane("XY").add(placed)

def provenance():
    return {str(p.relative_to(VENDOR.parent)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(VENDOR.parent.rglob("*")) if p.is_file()}

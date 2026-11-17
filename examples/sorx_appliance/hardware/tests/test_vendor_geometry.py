"""Supplier placement checks against independent assembly drawing dimensions."""
import importlib.util, unittest, sys, tempfile, json, hashlib
from pathlib import Path
if importlib.util.find_spec("cadquery") is None:
    raise unittest.SkipTest("Dedicated hardware container dependency unavailable: cadquery")
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import vendor_geometry as v
class SupplierGeometryTests(unittest.TestCase):
    def test_tampered_supplier_step_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/"sample.step").write_bytes(b"modified")
            (root/"manifest.json").write_text(json.dumps({"assets":[{"id":"sample","step_sha256":hashlib.sha256(b"original").hexdigest()}]}))
            with self.assertRaisesRegex(ValueError,"SHA-256 mismatch"):
                v.checked_asset("sample",root)
    def test_heatsink_and_screws_match_mount_drawing(self):
        parts,tim=v.supplier_parts(78,64)
        heat=parts["HS1"].val().BoundingBox()
        self.assertAlmostEqual(heat.xlen,85.6,places=3)
        self.assertAlmostEqual(heat.ylen,38,places=3)
        self.assertAlmostEqual(heat.zmax,18,places=3)
        for ref,x,y in (("SC1",45.3945,87.074),("SC2",110.5945,87.074),("SC3",37.9945,56.074),("SC4",117.9945,56.074)):
            b=parts[ref].val().BoundingBox()
            self.assertAlmostEqual((b.xmin+b.xmax)/2,x,places=3)
            self.assertAlmostEqual((b.ymin+b.ymax)/2,-y,places=3)
        self.assertAlmostEqual(tim.val().BoundingBox().zlen,3.175,places=3)
    def test_module_maximum_envelope_and_holes(self):
        import cadquery as cq
        parts,_=v.supplier_parts(78,64); module=parts["M1"]
        b=module.val().BoundingBox()
        self.assertAlmostEqual(b.zmin,.5,places=3)
        self.assertAlmostEqual(b.zmax,6.7,places=3)
        for x in (45.3945,110.5945):
            probe=cq.Workplane("XY").circle(.9).extrude(8).translate((x,-87.074,0))
            self.assertLess(module.intersect(probe).val().Volume(),.001)
if __name__=="__main__": unittest.main()

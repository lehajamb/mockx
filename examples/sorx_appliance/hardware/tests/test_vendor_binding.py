"""Supplier geometry must remain an input of the verified revision."""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from verification_core import source_hashes, scratch_project
class VendorBindingTests(unittest.TestCase):
    def test_vendor_asset_change_changes_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); asset=root/"mech/vendor/example/model.step"
            asset.parent.mkdir(parents=True); asset.write_bytes(b"original")
            before=source_hashes(root); asset.write_bytes(b"changed")
            self.assertNotEqual(before,source_hashes(root))
    def test_scratch_contains_vendor_inputs_without_old_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"source"; dest=Path(tmp)/"scratch"
            for name in ("hw/sorx.pretty","tools","bom","mech/vendor/example"):
                (root/name).mkdir(parents=True)
            (root/"mech/vendor/example/model.step").write_bytes(b"source geometry")
            (root/"mech/fit-check.json").write_bytes(b"stale")
            scratch_project(root,dest)
            self.assertTrue((dest/"mech/vendor/example/model.step").is_file())
            self.assertFalse((dest/"mech/fit-check.json").exists())
if __name__=="__main__": unittest.main()

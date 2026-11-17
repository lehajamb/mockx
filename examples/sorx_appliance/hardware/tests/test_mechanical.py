"""Mechanical regressions; run with the pinned CadQuery interpreter."""
import importlib.util
import unittest
for dependency in ['cadquery']:
    if importlib.util.find_spec(dependency) is None:
        raise unittest.SkipTest("Dedicated hardware container dependency unavailable: "+dependency)
import sys
import hashlib
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import gen_mech

class MechanicalRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if hasattr(gen_mech, 'load_parts'):
            gen_mech.P = gen_mech.load_parts(gen_mech.PARTS_PATH)

    def test_j9_has_no_enclosure_interference(self):
        _, parts = gen_mech.board_assembly()
        base, lid = gen_mech.enclosure(gen_mech.openings())
        volume = base.union(lid).intersect(parts["J9"]).val().Volume()
        self.assertLessEqual(volume, 0.001)

    def test_reporting_preserves_individual_component_geometry(self):
        pcb, parts = gen_mech.board_assembly()
        original_count = len(parts["J9"].objects)
        with tempfile.TemporaryDirectory() as out:
            with patch.object(gen_mech, "MECH", out), patch.object(gen_mech, "board_assembly", return_value=(pcb, parts)), patch.object(gen_mech.cq.Assembly, "save"), patch.object(gen_mech.cq.Assembly, "add") as assembly_add, patch.object(gen_mech.cq.exporters, "export"), patch.object(gen_mech, "drawings"):
                gen_mech.main()
        self.assertEqual(len(parts["J9"].objects), original_count)
        assembly_names = {call.kwargs["name"] for call in assembly_add.call_args_list}
        self.assertTrue(set(parts).issubset(assembly_names), "Assembly export silently omits modeled fitted components")

    def test_geometry_failure_is_not_zero_interference(self):
        class BrokenShell:
            def intersect(self, other):
                raise RuntimeError("injected CAD failure")
        with self.assertRaisesRegex(RuntimeError, "injected CAD failure"):
            gen_mech.intersection_volume(BrokenShell(), object(), "J9")

    def test_bottom_component_height_is_not_clipped(self):
        part = {"ref":"U_TEST", "fp":"SOT-223-3_TabPin2", "side":"B", "box":[10,10,15,15]}
        with patch.dict(gen_mech.P, {"U_TEST":part}, clear=True):
            _, parts = gen_mech.board_assembly()
        bounds = parts["U_TEST"].val().BoundingBox()
        self.assertAlmostEqual(bounds.zlen, 1.8, places=5)

    def test_module_keepout_rejects_top_part_but_not_carrier_bottom_part(self):
        parts={"J1":{"x":78.0,"y":64.0,"side":"F","fp":"SODIMM","box":[38,49,118,91]},
               "U_TEST":{"ref":"U_TEST","side":"F","fp":"SOT-23","box":[70,70,73,73]}}
        with patch.dict(gen_mech.P, parts, clear=True):
            result=gen_mech.module_keepout_findings()
            self.assertFalse(result["pass"])
            self.assertEqual(result["overlaps"][0]["ref"],"U_TEST")
            gen_mech.P["U_TEST"]["side"]="B"
            self.assertTrue(gen_mech.module_keepout_findings()["pass"])

    def test_fan_has_vertical_tolerance_allowance(self):
        self.assertGreaterEqual(gen_mech.ZC - gen_mech.ZF - gen_mech.FAN["size"], 1.0)

class MechanicalProvenanceRegressions(unittest.TestCase):
    def test_stale_and_missing_placement_metadata_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            pcb = Path(folder) / 'board.kicad_pcb'
            parts = Path(folder) / 'parts.json'
            pcb.write_bytes(b'old PCB')
            parts.write_bytes(b'[]')
            self.assertFalse(gen_mech.source_binding_valid(parts, pcb, 0))
            metadata = {'source_pcb_sha256': hashlib.sha256(pcb.read_bytes()).hexdigest(),
                        'parts_sha256': hashlib.sha256(parts.read_bytes()).hexdigest(), 'footprints': 0}
            parts.with_suffix('.json.meta.json').write_text(json.dumps(metadata), encoding='utf-8')
            self.assertTrue(gen_mech.source_binding_valid(parts, pcb, 0))
            pcb.write_bytes(b'changed PCB')
            self.assertFalse(gen_mech.source_binding_valid(parts, pcb, 0))

    def test_duplicate_component_refs_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            parts = Path(folder) / 'parts.json'
            parts.write_text('[{"ref":"J9"},{"ref":"J9"}]', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Duplicate component'):
                gen_mech.load_parts(parts)

if __name__ == "__main__":
    unittest.main()

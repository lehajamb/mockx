"""Failure-path tests for hardware verification; no KiCad or hardware required."""
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from verification_core import (apply_waivers, digest, engineering_failures, mechanical_failures,
                               parse_drc, parse_erc, run_tool, source_hashes, source_identity,
                               validate_release_report)

DRC = """** Drc report for sorx.kicad_pcb **
** Found 1 DRC violations **
[holes_co_located]: Holes are co-located
    Local override; Severity: error
    @(36.9000 mm, 41.5000 mm): Via [GND] on F.Cu - B.Cu
    @(36.9000 mm, 41.5000 mm): Via [GND] on F.Cu - B.Cu
** Found 0 unconnected pads **
** Found 0 Footprint errors **
** End of Report **
"""

def good_fit():
    return {"schema_version": 2, "success": True, "geometry_errors": [], "source_binding_valid": True,
            "source_pcb_sha256": "pcb", "model_inputs_sha256": {"mech_parts.json": "parts"},
            "max_interference_mm3": 0.0, "all_ports_aligned": True,
            "clearance_checks": {"fan": {"actual_mm": 1.0, "required_mm": 0.5, "ok": True}},
            "blockers": []}


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "fresh.json"
        self.logs = self.root / "logs"

    def run_code(self, code, outputs=True, timeout=20):
        return run_tool("injected", [sys.executable, "-c", code], self.root, self.logs,
                        [self.out] if outputs else [], timeout)

    def test_failed_subprocess_never_passes(self):
        result = self.run_code("import sys; sys.exit(9)", outputs=False)
        self.assertFalse(result["success"])
        self.assertEqual(result["returncode"], 9)

    def test_stale_output_removed_before_failed_command(self):
        self.out.write_text('{"passed": true}')
        result = self.run_code("import sys; sys.exit(3)")
        self.assertFalse(result["success"])
        self.assertFalse(self.out.exists())
        self.assertTrue(result["missing_outputs"])

    def test_failed_command_new_output_not_accepted(self):
        result = self.run_code("from pathlib import Path; import sys; Path('fresh.json').write_text('{}'); sys.exit(2)")
        self.assertFalse(result["success"])
        self.assertTrue((self.logs / "fresh.json").exists())

    def test_missing_output_with_zero_exit_fails(self):
        self.assertFalse(self.run_code("pass")["success"])

    def test_fresh_output_zero_exit_passes_and_is_hashed(self):
        result = self.run_code("from pathlib import Path; Path('fresh.json').write_text('{}')")
        self.assertTrue(result["success"])
        self.assertEqual(result["outputs"]["fresh.json"], digest(self.out))

    def test_timeout_is_execution_failure(self):
        result = self.run_code("import time; time.sleep(2)", outputs=False, timeout=0.05)
        self.assertFalse(result["success"])
        self.assertIsNone(result["returncode"])


class ReportTests(unittest.TestCase):
    def test_drc_counts_and_objects_are_preserved(self):
        data = parse_drc(DRC)
        self.assertEqual(data["counts"]["DRC violations"], 1)
        self.assertIn("36.9000", data["findings"][0]["objects"])

    def test_drc_missing_sections_fail(self):
        with self.assertRaises(ValueError):
            parse_drc("all fine")

    def test_truncated_drc_report_fails(self):
        with self.assertRaises(ValueError):
            parse_drc(DRC.replace("** End of Report **", ""))

    def test_drc_unparsed_findings_fail(self):
        with self.assertRaises(ValueError):
            parse_drc(DRC.replace("Found 1 DRC", "Found 2 DRC"))

    def test_duplicate_vias_have_no_blanket_waiver(self):
        findings = parse_drc(DRC)["findings"]
        unwaived, waived = apply_waivers(findings, {"schema_version": 1, "waivers": []}, Path.cwd(), "pcb")
        self.assertEqual(len(unwaived), 1)
        self.assertFalse(waived)

    def test_category_only_waiver_rejected(self):
        with self.assertRaises(ValueError):
            apply_waivers([], {"schema_version": 1, "waivers": [{"category": "holes_co_located", "reason": "fine"}]},
                          Path.cwd(), "pcb")

    def test_duplicate_hole_cannot_be_waived_even_with_evidence(self):
        finding = parse_drc(DRC)["findings"][0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); evidence = root / "review.txt"; evidence.write_text("review")
            waiver = {"fingerprint": finding["fingerprint"], "pcb_sha256": "pcb", "reason": "duplicate",
                      "reviewer": "test", "evidence_path": "review.txt", "evidence_sha256": digest(evidence)}
            with self.assertRaises(ValueError):
                apply_waivers([finding], {"schema_version": 1, "waivers": [waiver]}, root, "pcb")

    def test_erc_requires_consistent_counts(self):
        self.assertEqual(parse_erc("ERC messages: 0 Errors 0 Warnings 0")["errors"], 0)
        with self.assertRaises(ValueError):
            parse_erc("ERC messages: 0 Errors 1 Warnings 0")


class MechanicalTests(unittest.TestCase):
    def test_complete_geometric_success_passes(self):
        self.assertEqual(mechanical_failures(good_fit(), "pcb", "parts"), [])

    def test_j9_collision_blocks_release(self):
        data = good_fit(); data["max_interference_mm3"] = 5.002
        self.assertTrue(mechanical_failures(data, "pcb", "parts"))

    def test_geometry_exceptions_never_equal_zero(self):
        data = good_fit(); data["geometry_errors"] = ["kernel failed"]
        self.assertTrue(mechanical_failures(data, "pcb", "parts"))

    def test_stale_board_or_placement_rejected(self):
        self.assertTrue(mechanical_failures(good_fit(), "newpcb", "parts"))
        self.assertTrue(mechanical_failures(good_fit(), "pcb", "newparts"))

    def test_unqualified_ports_clearance_and_heights_block(self):
        for key, value in [("all_ports_aligned", False), ("clearance_checks", {}), ("blockers", ["unknown height"])]:
            with self.subTest(key=key):
                data = good_fit(); data[key] = value
                self.assertTrue(mechanical_failures(data, "pcb", "parts"))

    def test_nan_infinity_and_missing_volume_rejected(self):
        for value in (None, math.nan, math.inf, -1):
            with self.subTest(value=value):
                data = good_fit(); data["max_interference_mm3"] = value
                self.assertTrue(mechanical_failures(data, "pcb", "parts"))

    def test_legacy_report_cannot_claim_fresh_approval(self):
        self.assertTrue(mechanical_failures({"max_interference_mm3": 0, "all_ports_aligned": True}, "pcb", "parts"))


class GateTests(unittest.TestCase):
    def test_missing_review_blocks_all_release(self):
        self.assertTrue(engineering_failures({}, {}, Path.cwd()))

    def test_pass_requires_current_sources_and_hashed_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = root / "review.txt"; p.write_text("review")
            hashes = {"hw/sorx.kicad_pcb": "pcb"}
            gate = {"status": "PASS", "reviewer": "engineer",
                    "evidence": [{"path": "review.txt", "sha256": digest(p)}]}
            review = {"schema_version": 1, "source_identity": source_identity(hashes),
                      "gates": {f"R{i:02}": dict(gate) for i in range(1, 10)}}
            self.assertFalse(engineering_failures(review, hashes, root))
            p.write_text("changed")
            self.assertTrue(engineering_failures(review, hashes, root))
            self.assertTrue(engineering_failures(review, {"pcb": "changed"}, root))

    def test_passing_summary_without_raw_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                validate_release_report({"release_passed": True, "source_hashes": source_hashes(root), "environment": {"container_image": "sha256:" + "0" * 64}}, root)

    def test_importing_verifier_and_export_has_no_tool_side_effects(self):
        with patch("subprocess.run", side_effect=AssertionError("unexpected subprocess")):
            for name in ("verify", "export"):
                spec = importlib.util.spec_from_file_location("guarded_" + name, TOOLS / (name + ".py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertTrue(callable(module.main))


    def test_export_failure_does_not_create_archive(self):
        spec = importlib.util.spec_from_file_location("export_failure_case", TOOLS / "export.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fake_pcbnew = types.SimpleNamespace(LoadBoard=lambda path: types.SimpleNamespace(GetCopperLayerCount=lambda: 4))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(sys.modules, {"pcbnew": fake_pcbnew}), patch.object(module, "kicad_environment", return_value={"KICAD7_FOOTPRINT_DIR": "/test/fixtures/footprints"}), patch.object(module, "run_tool", return_value={"success": False, "returncode": 9}):
                self.assertEqual(module.export(root, development=True), 1)
            self.assertFalse(list(root.rglob("*.zip")))
            report = json.loads(next(root.rglob("export-result.json")).read_text())
            self.assertFalse(report["fabrication_approval"])
            self.assertFalse(report["complete"])

    def test_release_cli_refuses_missing_verification(self):
        spec = importlib.util.spec_from_file_location("export_no_approval", TOOLS / "export.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "argv", ["export.py", "--root", tmp]), patch.object(module, "kicad_environment", return_value={"KICAD7_FOOTPRINT_DIR": "/test/fixtures/footprints"}):
            self.assertEqual(module.main(), 1)
            self.assertFalse(list(Path(tmp).rglob("*.zip")))


if __name__ == "__main__":
    unittest.main()

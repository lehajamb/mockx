import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HW / "tools"))
import routing_gate as gate
from verification_core import digest, parse_drc


def archived(case):
    proof = HW / "validation" / case
    return [parse_drc((proof / name).read_text()) for name in ("before.txt", "after.txt")]


def example():
    labels = dict(a="U1.1", b="J1.1", c="C64.2", d="U5.1", e="C1.2")
    before = {"labels": labels, "fixed_geometry": "fixture", "copper": {}, "duplicate_vias": [],
              "groups": {"SIGNAL": [["a"], ["b"]], "GND": [["c", "d"], ["e"]]}}
    after = copy.deepcopy(before)
    after["groups"]["SIGNAL"] = [["a", "b"]]
    after["copper"]["track1"] = {"locked": False, "net": "SIGNAL"}
    return before, after


def reports_for_example():
    def drc(*nets):
        return {"counts": {gate.SECTION: len(nets)}, "findings": [
            {"section": gate.SECTION, "objects": "Pad on [" + net + "]"} for net in nets]}
    return drc("SIGNAL", "GND"), drc("GND")


class RoutingGateTests(unittest.TestCase):
    def test_historical_accepted_power_bridge_actual_reports(self):
        reports = archived("power-bridge-proof-01")
        self.assertEqual([r["counts"][gate.SECTION] for r in reports], [33, 30])
        self.assertEqual(gate.drc_delta(*reports), ({}, []))

    def test_ordinary_proof_real_ground_regression_despite_fewer_opens(self):
        reports = archived("ordinary-completion-proof-01")
        self.assertEqual([r["counts"][gate.SECTION] for r in reports], [36, 31])
        self.assertEqual(gate.drc_delta(*reports)[0], {"GND": 1})
        raw = json.loads((HW / "validation/ordinary-completion-proof-01/ground-split-audit.json").read_text())
        self.assertEqual([(p["reference"], p["pad"]) for p in raw["newly_disconnected_pads"]], [("C64", "2")])

    def test_partition_comparison_detects_same_net_swap_at_equal_open_count(self):
        before, after = example()
        reports = reports_for_example()
        joins = [("U1.1", "J1.1")]
        self.assertEqual(gate.compare(before, after, reports, joins, []), ["SIGNAL"])
        after["groups"]["GND"] = [["d", "e"], ["c"]]
        with self.assertRaisesRegex(ValueError, "GND pad group split.*C64.2"):
            gate.compare(before, after, reports, joins, [])

    def test_missing_or_ambiguous_target_and_unapproved_detour(self):
        before, after = example()
        reports = reports_for_example()
        for pair in (("bad-pad", "J1.1"), ("C64.2", "U5.1"), ("U1.1", "C64.2")):
            with self.assertRaises(ValueError):
                gate.compare(before, after, reports, [pair], [])
        after["copper"]["unreviewed"] = {"net": "PCIE_CLK_P", "locked": False}
        with self.assertRaisesRegex(ValueError, "unexpected copper"):
            gate.compare(before, after, reports, [("a", "b")], [])
        after["copper"]["unreviewed"]["locked"] = True
        with self.assertRaisesRegex(ValueError, "locked copper"):
            gate.compare(before, after, reports, [("a", "b")], ["PCIE_CLK_P"])

    def test_inconsistent_pad_geometry_cannot_prove_a_join(self):
        before, after = example()
        # Wrong net but same number of unconnected groups as the physical checker.
        reports = reports_for_example()
        reports[1]["findings"][0]["objects"] = "Pad on [SIGNAL]"
        with self.assertRaisesRegex(ValueError, "candidate pad groups disagree with native DRC"):
            gate.compare(before, after, reports, [("a", "b")], [])

    def test_changed_native_rules_require_fresh_accepted_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp) / "hardware"
            (hw / "hw/sorx.pretty").mkdir(parents=True)
            (hw / "tools/env").mkdir(parents=True)
            (hw.parent / "docs").mkdir()
            board = hw / "hw/sorx.kicad_pcb"
            project = board.with_suffix(".kicad_pro")
            files = [board, project, hw / "hw/fp-lib-table", hw / "tools/env/image-id.txt"]
            for path in files:
                path.write_text("working native input")
            ledger = hw.parent / "docs/routing-ledger.json"
            ledger.write_text(json.dumps({"inputs": {p.relative_to(hw.parent).as_posix(): digest(p) for p in files}}))
            with patch.object(gate, "HW", hw):
                self.assertEqual(gate.check_native_binding(board.resolve()), [ledger])
                project.write_text("different rules")
                with self.assertRaisesRegex(ValueError, "accepted routing ledger"):
                    gate.check_native_binding(board.resolve())

    def test_missing_truncated_stale_and_failed_evidence(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            parse_drc("** Found 0 DRC violations **")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            base, candidate = [tmp / f"{name}.kicad_pcb" for name in ("parent", "candidate")]
            for item in (base, candidate):
                item.write_text("fake board - worker is mocked")
            opts = argparse.Namespace(parent=base, candidate=candidate, out=tmp / "new",
                                      parent_sha256="123", join=[("A", "B")], allow_net=[])
            with patch.dict(os.environ, SORX_IMAGE_ID=(HW / "tools/env/image-id.txt").read_text().strip()):
                with self.assertRaisesRegex(ValueError, "stale accepted parent"):
                    gate.run(opts)
                opts.parent_sha256 = digest(base)
                with self.assertRaises(OSError):
                    gate.run(opts)  # no project rule file
                for item in (base, candidate):
                    item.with_suffix(".kicad_pro").write_text("test rule set")
                with patch.object(gate.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "worker")):
                    with self.assertRaises(subprocess.CalledProcessError):
                        gate.run(opts)
                self.assertFalse((opts.out / "comparison.json").exists())
                # A second call cannot reuse outputs from an earlier attempt.
                with self.assertRaises(FileExistsError):
                    gate.run(opts)


if __name__ == "__main__":
    unittest.main()

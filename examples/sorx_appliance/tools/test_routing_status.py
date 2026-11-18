import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import routing_status as rs


class RoutingStatusTest(unittest.TestCase):
    def setUp(self):
        # Frozen report: real data, independent of the active board's next promotion.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for rel in (rs.PCB, "hardware/hw/sorx.kicad_pro", "hardware/hw/fp-lib-table",
                    "hardware/tools/env/image-id.txt"):
            p = self.root / rel
            p.parent.mkdir(exist_ok=True, parents=True)
            p.write_bytes(rel.encode())
        ledger = json.loads((rs.ROOT / rs.LEDGER).read_text())
        ledger["inputs"] = rs.input_hashes(self.root)
        ledger["report"] = "hardware/validation/native-promotion-01/drc-refilled.txt"
        ledger["decisions"] = [d for d in ledger["decisions"] if d["id"] in ("ordinary-21", "power-bridge-01")]
        for rel in [ledger["report"], *(d["evidence"] for d in ledger["decisions"])]:
            p = self.root / rel
            p.parent.mkdir(exist_ok=True, parents=True)
            p.write_bytes((rs.ROOT / rel).read_bytes())
        self.report = self.root / ledger["report"]
        ledger["report_sha256"] = rs.digest(self.report)
        dest = self.root / rs.LEDGER
        dest.parent.mkdir(parents=True)
        dest.write_text(json.dumps(ledger))

    def test_real_queue_and_decisions_are_bounded(self):
        lines = rs.summarize(self.root)
        self.assertLess(len(lines), 20)
        self.assertTrue(any("30 (16 GND, 14 other); other findings: 93" in line for line in lines))
        self.assertIn("ACCEPTED power-bridge-01", "\n".join(rs.summarize(self.root, "+1V8")))
        self.assertIn("C64.2 GND disconnected", "\n".join(rs.summarize(self.root, "GND")))

    def test_source_or_report_drift_prevents_display(self):
        rules = self.root / "hardware/hw/sorx.kicad_dru"
        rules.write_text("(version 1)")
        with self.assertRaisesRegex(ValueError, "Stale routing queue"):
            rs.summarize(self.root)
        rules.unlink()
        self.report.write_text("old result")
        with self.assertRaisesRegex(ValueError, "evidence binding"):
            rs.summarize(self.root)


if __name__ == "__main__":
    unittest.main()

"""Check numeric land dimensions independently of native DRC acceptance."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"tools"))
import gen_sma
from sx import find, first
@unittest.skipUnless(gen_sma.SOURCE.exists(), "Pinned KiCad footprint source unavailable")
class SmaLandPatternTests(unittest.TestCase):
    def test_exact_copper_land_dimensions_and_edge_offsets(self):
        pads = find(gen_sma.footprint(), "pad")
        self.assertEqual(len(pads), 5)
        for pad in pads:
            with self.subTest(pad=pad[1], layers=first(pad,"layers")[1:]):
                signal = pad[1] == "1"
                w,l = map(float,first(pad,"size")[1:])
                x,y = map(float,first(pad,"at")[1:3])
                self.assertAlmostEqual(w,1.5 if signal else 1.6)
                self.assertAlmostEqual(l,5.2 if signal else 4.9)
                self.assertAlmostEqual(2.54-x-l/2,.3 if signal else .6)
                self.assertAlmostEqual(2.54-x+l/2,5.5)
                self.assertAlmostEqual(abs(y),0 if signal else 4.25)
class SmaCandidateGateTests(unittest.TestCase):
    def test_new_required_open_fails(self):
        from apply_sma_land import candidate_passed
        self.assertFalse(candidate_passed({"unconnected pads":0},{"unconnected pads":1},[]))
    def test_native_errors_fail_and_equal_counts_pass(self):
        from apply_sma_land import candidate_passed
        self.assertFalse(candidate_passed({"unconnected pads":2},{"unconnected pads":2},["short"]))
        self.assertTrue(candidate_passed({"unconnected pads":2},{"unconnected pads":2},[]))
if __name__ == "__main__": unittest.main()

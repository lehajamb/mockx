"""Nodal extraction must preserve shared bottlenecks and unequal branch currents."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
try:
    import scipy
except ImportError as exc:
    raise unittest.SkipTest("Power-network tests require scipy in the pinned hardware container") from exc
from power_network import solve_resistors

class PowerNetworkTests(unittest.TestCase):
    def test_unequal_parallel_branches_do_not_split_current_equally(self):
        v,i=solve_resistors([('s','a',.1),('a','t',.1),('a','t',.3)],['s'],{'t':2.0})
        self.assertAlmostEqual(v['t'],.35)
        self.assertAlmostEqual(abs(i[0]),2.0)
        self.assertAlmostEqual(abs(i[1]),1.5)
        self.assertAlmostEqual(abs(i[2]),.5)
    def test_separate_loads_share_only_upstream_section(self):
        v,i=solve_resistors([('s','a',.1),('a','x',.2),('a','y',.3)],['s'],{'x':.25,'y':.75})
        self.assertAlmostEqual(v['x'],.15)
        self.assertAlmostEqual(v['y'],.325)
        self.assertAlmostEqual(abs(i[0]),1)
    def test_open_required_load_fails(self):
        with self.assertRaises(ValueError):
            solve_resistors([('s','a',.1)],['s'],{'missing':1})

if __name__=='__main__':unittest.main()

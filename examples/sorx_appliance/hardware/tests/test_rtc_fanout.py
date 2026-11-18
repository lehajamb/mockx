import importlib.util,sys,unittest
from pathlib import Path
for name in ("pcbnew","shapely","numpy","PIL"):
 if importlib.util.find_spec(name) is None:raise unittest.SkipTest("Dedicated hardware environment required: "+name)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import ground_fanout as g

class RtcFanoutTests(unittest.TestCase):
 def test_rtc_exception_requires_explicit_review_and_exact_width(self):
  self.assertFalse(g.fanout_policy("VBAT_RTC",.15,False)[0])
  self.assertTrue(g.fanout_policy("VBAT_RTC",.15,True)[0])
  self.assertFalse(g.fanout_policy("VBAT_RTC",.25,True)[0])
  self.assertFalse(g.fanout_policy("+1V8",.15,True)[0])

 def test_added_length_over_twenty_mm_is_rejected(self):
  old=[dict(net="VBAT_RTC",a=(0,0),b=(1,0))]
  new=[dict(net="VBAT_RTC",new_runs=[("B",[(0,0),(22,0)])],new_vias=[])]
  with self.assertRaises(ValueError):g.rtc_detour_budget(old,new)

 def test_length_budget_records_engineering_current_allocation(self):
  old=[dict(net="VBAT_RTC",a=(0,0),b=(1,0))]
  new=[dict(net="VBAT_RTC",new_runs=[("B",[(0,0),(5,0)])],new_vias=[])]
  r=g.rtc_detour_budget(old,new)
  self.assertEqual(r["added_planar_length_mm"],4)
  self.assertEqual(r["engineering_current_allocation_a"],.001)
  self.assertFalse(r["manufacturer_maximum_current_claimed"])

if __name__=="__main__":unittest.main()

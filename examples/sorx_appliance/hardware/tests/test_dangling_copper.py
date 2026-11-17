import importlib.util,sys,unittest
from pathlib import Path
if importlib.util.find_spec("shapely") is None:raise unittest.SkipTest("Dedicated hardware environment required: shapely")
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from shapely.geometry import Point,LineString
from trim_dangling_copper import plan_trim,pad_groups

class DanglingCopperTests(unittest.TestCase):
 def fixture(self):
  pads=[dict(id="p"+str(x),kind="pad",shape=Point(x,0).buffer(.2),layers=[0]) for x in (0,2)]
  def track(name,a,z,locked=False):return dict(id=name,kind="track",shape=LineString([a,z]).buffer(.075),layers=[0],ends=[a,z],locked=locked)
  return pads+[track("required",(0,0),(2,0)),track("stub",(2,0),(3,0))]

 def test_trim_only_dead_tail_keeps_pad_groups(self):
  nodes=self.fixture();removed=plan_trim(nodes)
  self.assertEqual(removed,["stub"])
  self.assertEqual(pad_groups(nodes),pad_groups([n for n in nodes if n["id"] not in removed]))

 def test_required_open_net_is_never_trimmed(self):
  self.assertEqual(plan_trim(self.fixture(),has_required_open=True),[])

 def test_locked_stub_is_preserved(self):
  nodes=self.fixture();nodes[-1]["locked"]=True
  self.assertEqual(plan_trim(nodes),[])

 def test_pad_at_stub_end_makes_route_required(self):
  nodes=self.fixture();nodes.append(dict(id="p3",kind="pad",shape=Point(3,0).buffer(.2),layers=[0]))
  self.assertEqual(plan_trim(nodes),[])

if __name__=="__main__":unittest.main()

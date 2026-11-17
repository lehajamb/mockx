import importlib.util,sys,unittest
from pathlib import Path
for name in ("pcbnew","shapely"):
 if importlib.util.find_spec(name) is None:raise unittest.SkipTest("Dedicated hardware environment required: "+name)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import pcbnew
from shapely.geometry import Point
from thermal_ground_vias import forbidden_sites

class ThermalViaTests(unittest.TestCase):
 def fixture(self):
  b=pcbnew.BOARD();f=pcbnew.FOOTPRINT(b);f.SetReference("U13");b.Add(f)
  def pad(number,layer,x,size):
   p=pcbnew.PAD(f);p.SetNumber(number);p.SetShape(pcbnew.PAD_SHAPE_RECT);p.SetAttribute(pcbnew.PAD_ATTRIB_SMD);p.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(size),pcbnew.FromMM(size)));p.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x),pcbnew.FromMM(10)));p.SetLayerSet(pcbnew.LSET(layer));f.Add(p);return p
  target=pad("49",pcbnew.F_Cu,10,4)
  pad("",pcbnew.F_Paste,10,.8)
  pad("29",pcbnew.F_Cu,11.5,.3)
  return b,target

 def test_own_ep_paste_window_is_not_foreign_copper(self):
  b,p=self.fixture()
  self.assertFalse(forbidden_sites(b,p).intersects(Point(10,10)))

 def test_other_copper_pads_still_block(self):
  b,p=self.fixture()
  self.assertTrue(forbidden_sites(b,p).intersects(Point(11.5,10)))

 def test_opposite_side_foreign_track_still_blocks(self):
  b,p=self.fixture();net=pcbnew.NETINFO_ITEM(b,"SIGNAL");b.Add(net)
  t=pcbnew.PCB_TRACK(b);t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(9),pcbnew.FromMM(10)));t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(11),pcbnew.FromMM(10)));t.SetWidth(pcbnew.FromMM(.15));t.SetLayer(pcbnew.B_Cu);t.SetNet(net);b.Add(t)
  self.assertTrue(forbidden_sites(b,p).intersects(Point(10,10)))

if __name__=="__main__":unittest.main()

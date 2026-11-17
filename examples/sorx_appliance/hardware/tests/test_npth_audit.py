"""Native geometry regression for the reproduced backside pad/NPTH defect."""
import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
try:import pcbnew
except ImportError:pcbnew=None
from audit_npth import audit

@unittest.skipUnless(pcbnew,'KiCad pcbnew is required')
class NpthAuditTests(unittest.TestCase):
 def board(self,pad_x=0):
  b=pcbnew.BOARD();b.SetCopperLayerCount(2)
  hole=pcbnew.FOOTPRINT(b);hole.SetReference('H7');b.Add(hole)
  p=pcbnew.PAD(hole);p.SetAttribute(pcbnew.PAD_ATTRIB_NPTH);p.SetShape(pcbnew.PAD_SHAPE_CIRCLE);p.SetDrillSize(pcbnew.VECTOR2I(3000000,3000000));p.SetSize(pcbnew.VECTOR2I(3000000,3000000));p.SetLayerSet(pcbnew.LSET.AllCuMask());hole.Add(p)
  f=pcbnew.FOOTPRINT(b);f.SetReference('U12');f.SetLayer(pcbnew.B_Cu);b.Add(f)
  p=pcbnew.PAD(f);p.SetNumber('5');p.SetAttribute(pcbnew.PAD_ATTRIB_SMD);p.SetShape(pcbnew.PAD_SHAPE_RECT);p.SetSize(pcbnew.VECTOR2I(600000,600000));p.SetLayerSet(pcbnew.LSET(pcbnew.B_Cu));f.Add(p);f.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(pad_x),0))
  return b
 def result(self,b):
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/'test.kicad_pcb';pcbnew.SaveBoard(str(path),b);return audit(path)
 def test_backside_copper_over_front_mounting_hole_is_a_defect(self):
  result=self.result(self.board());self.assertEqual(result['physical_overlap_count'],1)
  self.assertEqual(result['other_component_findings'][0]['component_pad']['layers'],['B.Cu'])
 def test_relocated_pad_has_no_hole_clearance_finding(self):
  result=self.result(self.board(3));self.assertEqual(result['other_component_findings'],[])
 def test_near_miss_is_retained_as_a_clearance_finding(self):
  result=self.result(self.board(1.9));self.assertEqual(result['physical_overlap_count'],0);self.assertEqual(len(result['other_component_findings']),1)

if __name__=='__main__':unittest.main()

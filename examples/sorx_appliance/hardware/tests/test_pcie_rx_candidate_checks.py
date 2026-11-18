import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from pcie_candidate_checks import assess_pcie_rx_candidate
CLEAN="** Drc report for test **\n** Found 0 DRC violations **\n** Found 0 unconnected pads **\n** Found 0 Footprint errors **\n** End of Report **\n"
def fixture():
 return {'errors':[],'pairs':[{'name':'PCIE_RX','errors':[],'p':{'off_path_track_length_mm':0,'via_count':1,'layer_sequence':['B.Cu','F.Cu']},'n':{'off_path_track_length_mm':0,'via_count':1,'layer_sequence':['B.Cu','F.Cu']},'planar_length_gate_pass':True,'via_symmetry_pass':True,'via_budget_pass':True,'layer_sequence_symmetry_pass':True}]}
class RXChecks(unittest.TestCase):
 def test_malformed_pair_collection_returns_failure(self):
  for report in [None,{'errors':[],'pairs':None},{'errors':[],'pairs':['bad']}]:self.assertFalse(assess_pcie_rx_candidate(report,CLEAN,CLEAN)['targeted_geometric_checks_pass'])
 def test_complete_report_is_intermediate_only(self):
  r=assess_pcie_rx_candidate(fixture(),CLEAN,CLEAN);self.assertTrue(r['targeted_geometric_checks_pass']);self.assertFalse(r['fabrication_ready'])
 def test_invalid_geometry_and_topology_rejected(self):
  for field,value in [('via_count',2),('layer_sequence',['F.Cu']),('off_path_track_length_mm',float('nan'))]:
   with self.subTest(field=field):
    r=fixture();r['pairs'][0]['n'][field]=value;self.assertFalse(assess_pcie_rx_candidate(r,CLEAN,CLEAN)['targeted_geometric_checks_pass'])
 def test_new_unrelated_required_open_rejected(self):
  after=CLEAN.replace('** Found 0 unconnected pads **','** Found 1 unconnected pads **\n[unconnected_items]: Missing connection\n Severity: error\n Pad [GND] of C1 to Pad [GND] of C2')
  self.assertFalse(assess_pcie_rx_candidate(fixture(),after,CLEAN)['targeted_geometric_checks_pass'])
 def test_missing_or_truncated_source_native_report_rejected(self):
  for before in ['',CLEAN.replace('** End of Report **','')]:self.assertFalse(assess_pcie_rx_candidate(fixture(),CLEAN,before)['targeted_geometric_checks_pass'])
if __name__=='__main__':unittest.main()

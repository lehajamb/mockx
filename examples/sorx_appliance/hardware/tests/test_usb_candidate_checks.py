"""Prevent partial/invalid routing reports from promoting a candidate."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from usb_candidate_checks import assess_usb_candidate


def fixture():
    return {'errors':[],'pairs':[{'name':name,'errors':[],'p':{'off_path_track_length_mm':0},
        'n':{'off_path_track_length_mm':0},'planar_length_gate_pass':True,'via_symmetry_pass':True,
        'via_budget_pass':True,'layer_sequence_symmetry_pass':True} for name in ['USB2_SSTX','USB2_SSRX']]}


CLEAN_DRC="** Drc report for test **\n** Found 0 DRC violations **\n** Found 0 unconnected pads **\n** Found 0 Footprint errors **\n** End of Report **\n"


class CandidateChecks(unittest.TestCase):
    def test_valid_geometry_is_explicitly_not_fabrication_approval(self):
        result=assess_usb_candidate(fixture(),CLEAN_DRC)
        self.assertTrue(result['targeted_geometric_checks_pass'])
        self.assertFalse(result['fabrication_ready'])

    def test_missing_pair_and_overlapping_copper_fail(self):
        for report in [dict(errors=[],pairs=[]),fixture()]:
            if report['pairs']:report['pairs'][0]['errors']=['Overlapping track centerlines']
            self.assertFalse(assess_usb_candidate(report,CLEAN_DRC)['targeted_geometric_checks_pass'])

    def test_physical_drc_error_cannot_be_hidden_by_good_lengths(self):
        drc=CLEAN_DRC.replace("** Found 0 DRC violations **", "** Found 1 DRC violations **\n[clearance]: failed\n Severity: error\n Track [USB2_SSRX_N] near U19")
        self.assertFalse(assess_usb_candidate(fixture(),drc)['targeted_geometric_checks_pass'])

    def test_empty_truncated_and_count_mismatched_native_reports_fail(self):
        for text in ['', '** End of Report **', CLEAN_DRC.replace('** End of Report **',''), CLEAN_DRC.replace('Found 0 DRC violations','Found 1 DRC violations')]:
            with self.subTest(text=text):
                self.assertFalse(assess_usb_candidate(fixture(),text)['targeted_geometric_checks_pass'])

    def test_off_path_measurements_require_finite_nonnegative_real_numbers(self):
        for value in [float('nan'),float('inf'),-1,True,'0',None]:
            with self.subTest(value=value):
                report=fixture();report['pairs'][0]['p']['off_path_track_length_mm']=value
                self.assertFalse(assess_usb_candidate(report,CLEAN_DRC)['targeted_geometric_checks_pass'])

    def test_required_flags_are_explicit_booleans(self):
        for field in ['planar_length_gate_pass','via_symmetry_pass','via_budget_pass','layer_sequence_symmetry_pass']:
            for value in [1,'true',None]:
                with self.subTest(field=field,value=value):
                    report=fixture();report['pairs'][0][field]=value
                    self.assertFalse(assess_usb_candidate(report,CLEAN_DRC)['targeted_geometric_checks_pass'])

    def test_unremoved_branch_and_extra_via_fail(self):
        for field,value in [('off_path_track_length_mm',0.2),('via_budget_pass',False)]:
            report=fixture()
            if field=='off_path_track_length_mm':report['pairs'][0]['p'][field]=value
            else:report['pairs'][0][field]=value
            self.assertFalse(assess_usb_candidate(report,CLEAN_DRC)['targeted_geometric_checks_pass'])

if __name__=='__main__':unittest.main()

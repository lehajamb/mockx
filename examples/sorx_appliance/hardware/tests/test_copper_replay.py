import sys,unittest,json,tempfile,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from replay_copper_delta import check_conflicts,delta,apply_omissions,copper_differences

class CopperReplayTests(unittest.TestCase):
 def test_postfill_net_reassignment_is_detected(self):
  self.assertEqual(copper_differences({"v":{"net":"GND"}},{"v":{"net":"PCIE_CLK_P"}}),["v"])

 def test_omission_requires_exact_ground_via_and_evidence(self):
  with tempfile.TemporaryDirectory() as tmp:
   evidence=Path(tmp)/"report.json";evidence.write_text("native conflict")
   item={"uuid":"via","reason":"explicit collision","evidence_path":"report.json","evidence_sha256":hashlib.sha256(evidence.read_bytes()).hexdigest()}
   manifest={"schema_version":1,"proof_sha256":"proof","omissions":[item]}
   added={"via":{"kind":"via","net":"GND"}}
   self.assertEqual(apply_omissions(added,manifest,"proof",tmp)[0],{})
   with self.assertRaises(ValueError):apply_omissions({"via":{"kind":"via","net":"SIGNAL"}},manifest,"proof",tmp)
   with self.assertRaises(ValueError):apply_omissions(added,manifest,"wrong-proof",tmp)
   evidence.write_text("changed")
   with self.assertRaises(ValueError):apply_omissions(added,manifest,"proof",tmp)

 def test_independent_changes_are_preserved(self):
  base={"keep":{"net":"GND"},"replace":{"x":1}}
  changed={"keep":{"net":"GND"},"new":{"x":2}}
  removed,added=delta(base,changed)
  self.assertEqual(removed,{"replace":{"x":1}});self.assertEqual(added,{"new":{"x":2}})
  check_conflicts(dict(base,independent={"x":3}),removed,added)

 def test_same_uuid_changed_geometry_is_conflict(self):
  with self.assertRaisesRegex(ValueError,"Removed or changed"):
   check_conflicts({"old":{"x":2}},{"old":{"x":1}},{})

 def test_missing_removed_copper_is_conflict(self):
  with self.assertRaises(ValueError):check_conflicts({}, {"old":{"x":1}}, {})

 def test_existing_added_uuid_is_conflict(self):
  with self.assertRaisesRegex(ValueError,"already exists"):
   check_conflicts({"new":{"x":3}}, {}, {"new":{"x":2}})

if __name__=="__main__":unittest.main()

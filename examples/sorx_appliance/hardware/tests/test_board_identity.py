"""PCB/model footprint identity must include the library namespace."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from verification_worker import check_board

class BoardIdentityTests(unittest.TestCase):
    def check(self, library):
        identity=types.SimpleNamespace(GetLibItemName=lambda:"SameName",GetLibNickname=lambda:library)
        fp=types.SimpleNamespace(GetReference=lambda:"U1",GetFPID=lambda:identity,GetValue=lambda:"device",Pads=lambda:[])
        board=types.SimpleNamespace(GetFootprints=lambda:[fp],GetTracks=lambda:[],Zones=lambda:[])
        native=types.SimpleNamespace(LoadBoard=lambda path:board,PCB_VIA=type("PCB_VIA",(),{}),WriteDRCReport=lambda *a:None,EDA_UNITS_MILLIMETRES=0,ZONE_FILLER=lambda b:types.SimpleNamespace(Fill=lambda zones:None))
        part=types.SimpleNamespace(ref="U1",fp="expected:SameName",value="device")
        design=types.SimpleNamespace(DESIGN=types.SimpleNamespace(parts={"U1":part}))
        sch=types.SimpleNamespace(resolve=lambda p:{})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules,{"pcbnew":native,"design":design,"gen_sch":sch}):
            root=Path(tmp)
            check_board(root/"input.kicad_pcb",root/"drc.txt",root/"result.json")
            return json.loads((root/"result.json").read_text())["model_mismatches"]

    def test_same_item_in_wrong_library_is_mismatch(self):
        issues=self.check("wrong")
        self.assertEqual(len(issues),1)
        self.assertIn("wrong:SameName",issues[0])
        self.assertIn("expected:SameName",issues[0])

    def test_exact_library_and_item_match(self):
        self.assertEqual(self.check("expected"),[])

if __name__=="__main__":
    unittest.main()

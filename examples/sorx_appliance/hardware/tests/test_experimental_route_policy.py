"""The experimental router shares the supported router's protected-net policy."""
import importlib.util
import sys
import unittest
from pathlib import Path
if importlib.util.find_spec("pcbnew") is None or importlib.util.find_spec("shapely") is None:
    raise unittest.SkipTest("Dedicated hardware container dependencies unavailable")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from autoroute_codex import eligible
class ExperimentalRoutingPolicyTests(unittest.TestCase):
    def test_protected_nets_never_enter_global_router(self):
        for net in ("GND", "RS_C1P", "RS_C2N", "OE_REG_CTRL", "BUCK_BOOT", "VBAT_RTC", "RS_VP", "RS_VN", "PD_1V2", "PD_2V7", "MG_TXC", "SD_CLK", "USB2_SSTX_P", "+5V"):
            with self.subTest(net=net):
                self.assertFalse(eligible(net))
    def test_ordinary_control_is_eligible(self):
        self.assertTrue(eligible("FRAM_CS#"))
if __name__ == "__main__": unittest.main()

import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import proj

class PowerClassificationTests(unittest.TestCase):
    def test_battery_and_charge_pump_supplies_are_not_signals(self):
        for net in ("VBAT_RTC", "RS_VP", "RS_VN"):
            with self.subTest(net=net):
                self.assertEqual(proj.classify(net), "PWR")

    def test_pd_internal_regulator_capacitors_are_local_power_nets(self):
        for net in ("PD_1V2", "PD_2V7"):
            with self.subTest(net=net):
                self.assertEqual(proj.classify(net), "PWR")

    def test_auxiliary_pd_rail_has_power_clearance_and_width(self):
        self.assertEqual(proj.classify("+4V5_PD"), "PWR")

    def test_recovery_fixture_supply_is_not_a_generic_signal(self):
        self.assertEqual(proj.classify("USB_REC_VBUS"), "PWR")

if __name__ == "__main__":
    unittest.main()

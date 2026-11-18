import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from route_power import policy,endpoint_supported


def ends(net,description="Track",layer="F"):
    return [((0,0),layer,net,description),((1,1),"F",net,"Track")]


class PowerRoutingPolicyTests(unittest.TestCase):
    def test_input_trunk_and_auxiliary_branch_have_distinct_widths(self):
        self.assertGreater(policy(ends("VBUS_IN"))[0],policy(ends("VBUS_IN","Pad1 of C136"))[0])
        self.assertTrue(policy(ends("VBUS_IN"))[2])

    def test_internal_power_layer_is_not_reinterpreted_as_surface(self):
        self.assertFalse(endpoint_supported(ends("+5V",layer="I")))

    def test_only_identified_host_sense_branch_gets_small_width(self):
        self.assertIsNone(policy(ends("USB1_VBUS")))
        self.assertLess(policy(ends("USB1_VBUS","Pad1 of JP1"))[1],.01)

    def test_high_speed_net_is_not_eligible(self):
        self.assertIsNone(policy(ends("PCIE_TX_P")))


if __name__=="__main__":unittest.main()

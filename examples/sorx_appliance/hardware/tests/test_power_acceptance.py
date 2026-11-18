import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from power_acceptance import accept_power_change

def report(counts):
 return {'findings':[{'section':'unconnected pads','objects':'Pad ['+net+'] and Track ['+net+']'}for net,count in counts.items()for _ in range(count)]}
class PowerAcceptanceTests(unittest.TestCase):
 def test_reduced_total_cannot_hide_new_ground_island(self):
  accepted,_=accept_power_change(report({'PWR':2}),report({'GND':1}),[],['PWR'])
  self.assertFalse(accepted)
 def test_required_power_net_must_be_fully_connected(self):
  accepted,_=accept_power_change(report({'PWR':2,'OTHER':2}),report({'PWR':1}),[],['PWR'])
  self.assertFalse(accepted)
 def test_complete_power_and_preserved_other_nets_pass(self):
  accepted,_=accept_power_change(report({'PWR':2,'GND':1}),report({'GND':1}),[],['PWR'])
  self.assertTrue(accepted)
if __name__=='__main__':unittest.main()

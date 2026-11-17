import math,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from design import DESIGN
from sx import parse,find,first
class ServiceHeaderTests(unittest.TestCase):
 def test_exact_ordering_parts_and_separate_shunts(self):
  for ref,mpn in [('JP1','61300311121'),('JP2','61300211121'),('J14','61300211121')]:
   self.assertEqual(DESIGN.parts[ref].mpn,mpn);self.assertEqual(DESIGN.parts[ref].fp,'sorx:Wurth_'+mpn)
  for ref in ['SH1','SH2']:
   self.assertEqual(DESIGN.parts[ref].mpn,'60900213421');self.assertFalse(DESIGN.parts[ref].fp);self.assertTrue(DESIGN.parts[ref].inbom)
 def test_finished_drill_and_original_pad_centers(self):
  for mpn,count in [('61300311121',3),('61300211121',2)]:
   fp=parse((ROOT/'hw/sorx.pretty'/('Wurth_'+mpn+'.kicad_mod')).read_text());pads=sorted(find(fp,'pad'),key=lambda p:int(p[1]));self.assertEqual(len(pads),count)
   for i,pad in enumerate(pads):
    self.assertEqual(tuple(map(float,first(pad,'at')[1:])),(0.0,2.54*i));self.assertEqual(tuple(map(float,first(pad,'size')[1:])),(1.7,1.7))
    drill=float(first(pad,'drill')[1]);self.assertEqual(drill,1.2);self.assertGreater(drill-.05,math.sqrt(2)*(.64+.15));self.assertLessEqual(drill+.05,1.10+.15)
if __name__=='__main__':unittest.main()

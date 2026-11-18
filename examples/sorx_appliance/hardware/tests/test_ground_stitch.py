"""Focused geometry regressions for ground stitching and power detours."""
import sys
import unittest
import importlib.util
for _dependency in ['shapely']:
    if importlib.util.find_spec(_dependency) is None:
        raise unittest.SkipTest("Dedicated hardware environment required; unavailable dependency: " + _dependency)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from shapely.geometry import Point, LineString, box
import ground_stitch as gs

class GroundGeometryTests(unittest.TestCase):
    def test_stitch_uses_copper_island_beyond_obstructed_pad(self):
        copper = box(0, 0, 3, 1)
        forbidden = box(-1, -1, 1, 2)
        chosen = gs.choose_site(copper, forbidden, box(-5, -5, 5, 5))
        self.assertIsNotNone(chosen)
        self.assertGreater(chosen[0][0], 1)

    def test_detour_avoids_new_via_without_shrinking_power_trace(self):
        obstacle = Point(0, 0).buffer(.3)
        self.assertEqual(LineString([(-5,0),(5,0)]).distance(obstacle), 0)
        path = gs.detour_polyline((-5,0),(5,0),(0,0),.5,Point().buffer(0),box(-6,-3,6,3))
        self.assertIsNotNone(path)
        self.assertGreaterEqual(LineString(path).buffer(.25).distance(obstacle), gs.COPPER_CLEARANCE)
        self.assertEqual(path[0], (-5,0))
        self.assertEqual(path[-1], (5,0))

    def test_detour_refuses_blocked_copper(self):
        path = gs.detour_polyline((-5,0),(5,0),(0,0),.5,box(-2,-3,2,3),box(-6,-3,6,3))
        self.assertIsNone(path)

    def test_detour_refuses_via_at_existing_track_endpoint(self):
        self.assertIsNone(gs.detour_polyline((0,0),(5,0),(0,0),.5,Point().buffer(0),box(-6,-3,6,3)))

if __name__ == '__main__': unittest.main()

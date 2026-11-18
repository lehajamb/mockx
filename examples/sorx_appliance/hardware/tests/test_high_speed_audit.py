"""Focused routing-audit regressions for branches, explicit devices and layer isolation."""
import importlib.util
import unittest
for dependency in ['shapely', 'numpy']:
    if importlib.util.find_spec(dependency) is None:
        raise unittest.SkipTest("Dedicated hardware container dependency unavailable: "+dependency)
import sys
import json
import numpy as np
import unittest
from pathlib import Path
from shapely.geometry import LineString, Point, box
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from high_speed_audit import RouteGraph, reference_audit, report_json


def track(name,a,b,net='D',layer='F.Cu'):
    return {'id':name,'net':net,'layer':layer,'line':LineString([a,b]),'width_mm':0.15}


def pad(name,xy,net='D',layer='F.Cu'):
    return {'id':name,'net':net,'xy':xy,'layers':[layer],'shape':Point(xy).buffer(0.1)}


class PathRegressions(unittest.TestCase):
    def test_geometry_scalar_report_roundtrips_boolean(self):
        report={'success':np.bool_(False),'length_mm':np.float64(1.25)}
        parsed=json.loads(report_json(report))
        self.assertIs(parsed['success'],False)
        self.assertEqual(parsed['length_mm'],1.25)

    def test_esd_stub_does_not_inflate_endpoint_length(self):
        traces=[track('main',(0,0),(10,0)),track('esd-stub',(5,0),(5,5))]
        graph=RouteGraph(traces,[pad('SOC:1',(0,0)),pad('J:1',(10,0)),pad('ESD:1',(5,5))],[])
        route=graph.route('SOC:1','J:1')
        self.assertAlmostEqual(route['planar_length_mm'],10.0)
        self.assertAlmostEqual(sum(t['line'].length for t in traces),15.0)
        self.assertGreater(route['off_path_track_length_mm'],4.8)

    def test_series_capacitor_requires_explicit_bridge(self):
        traces=[track('tx',(0,0),(4,0),'TX'),track('rx',(6,0),(10,0),'RX')]
        pads=[pad('U:1',(0,0),'TX'),pad('C:1',(4,0),'TX'),pad('C:2',(6,0),'RX'),pad('J:1',(10,0),'RX')]
        with self.assertRaisesRegex(ValueError,'No copper'):
            RouteGraph(traces,pads,[]).route('U:1','J:1')
        route=RouteGraph(traces,pads,[],[{'a':'C:1','b':'C:2','object':'C','pad_center_span_mm':2.0}]).route('U:1','J:1')
        self.assertAlmostEqual(route['planar_length_mm'],8.0)
        self.assertEqual(route['series_components'][0]['object'],'C')

    def test_crossing_layers_do_not_create_a_connection(self):
        traces=[track('top',(0,0),(10,0)),track('bottom',(5,-5),(5,5),layer='B.Cu')]
        pads=[pad('U:1',(0,0)),pad('J:1',(5,5),layer='B.Cu')]
        with self.assertRaisesRegex(ValueError,'No copper'):
            RouteGraph(traces,pads,[]).route('U:1','J:1')
        via={'id':'v1','net':'D','xy':(5,0),'layers':['F.Cu','B.Cu']}
        route=RouteGraph(traces,pads,[via]).route('U:1','J:1')
        self.assertAlmostEqual(route['planar_length_mm'],10.0)
        self.assertEqual(route['via_count'],1)
        self.assertIsNone(route['via_barrel_length_mm'])

    def test_reference_slot_and_foreign_copper_are_reported(self):
        route={'edges':[{'kind':'track','object':'signal','start':(0,0),'end':(10,0),'width_mm':0.15,'layer':'B.Cu'}],'via_ids':[]}
        ground=box(-1,-1,11,1).difference(box(4.8,-1,5.2,1))
        foreign=track('power',(5,-1),(5,1),net='+3V3',layer='In2.Cu')
        result=reference_audit(route,{'layers':['F.Cu','In1.Cu','In2.Cu','B.Cu'],'ground':{'In2.Cu':ground},'tracks':[foreign],'vias':[]})
        self.assertFalse(result['continuous_projected_ground'])
        self.assertEqual(result['foreign_reference_copper'][0]['reference_net'],'+3V3')
        self.assertGreater(result['missing_reference'][0]['uncovered_centerline_mm'],0.4)

    def test_six_layer_internal_signal_requires_continuous_in4_ground(self):
        route={'edges':[{'kind':'track','object':'signal','start':(0,0),'end':(10,0),'width_mm':0.15,'layer':'In3.Cu'}],'via_ids':[]}
        geometry={'layers':['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu'],'ground':{'In4.Cu':box(-1,-1,11,1)},'tracks':[],'vias':[]}
        self.assertTrue(reference_audit(route,geometry)['continuous_projected_ground'])
        geometry['ground']['In4.Cu']=box(-1,-1,11,1).difference(box(4.8,-1,5.2,1))
        self.assertFalse(reference_audit(route,geometry)['continuous_projected_ground'])

    def test_track_ending_inside_via_annulus_is_connected(self):
        traces=[track('top',(0,0),(4.8,0)),track('bottom',(5,0),(5,5),layer='B.Cu')]
        pads=[pad('U:1',(0,0)),pad('J:1',(5,5),layer='B.Cu')]
        via={'id':'v1','net':'D','xy':(5,0),'layers':['F.Cu','B.Cu'],'diameter_mm':0.6,'drill_mm':0.3}
        route=RouteGraph(traces,pads,[via]).route('U:1','J:1')
        self.assertEqual(route['via_count'],1)
        self.assertAlmostEqual(route['planar_length_mm'],10.0)
        self.assertTrue(any(e['kind']=='via_copper' for e in route['edges']))

    def test_duplicate_copper_is_an_error(self):
        graph=RouteGraph([track('a',(0,0),(10,0)),track('b',(0,0),(10,0))],[],[])
        self.assertTrue(graph.errors)

if __name__=='__main__':
    unittest.main()

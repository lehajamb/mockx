import sys
import unittest
import importlib.util
for _dependency in ['shapely', 'pcbnew', 'numpy', 'PIL']:
    if importlib.util.find_spec(_dependency) is None:
        raise unittest.SkipTest("Dedicated hardware environment required; unavailable dependency: " + _dependency)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import numpy as np
from shapely.geometry import box, Point, GeometryCollection
from route_completion import Grid

class RoutingGeometryTests(unittest.TestCase):
    def test_obstacle_polygon_holes_remain_available(self):
        grid = Grid.__new__(Grid)
        grid.x0 = grid.y0 = 0
        grid.nx = grid.ny = 201
        cells = np.zeros((201,201), dtype=np.uint16)
        grid.stamp(cells, box(1,1,9,9).difference(box(3,3,7,7)), 0)
        self.assertEqual(cells[100,100], 0)
        self.assertEqual(cells[40,40], 1)
        self.assertEqual(cells[0,0], 0)

    def test_clipped_obstacle_tangencies_remain_blocked(self):
        grid = Grid.__new__(Grid)
        grid.x0 = grid.y0 = 0
        grid.nx = grid.ny = 201
        cells = np.zeros((201,201), dtype=np.uint16)
        grid.stamp(cells, GeometryCollection([box(1,1,2,2),Point(4,4)]), 0)
        self.assertEqual(cells[80,80], 1)
        self.assertEqual(cells[100,100], 0)


    def test_inner_power_endpoint_is_not_misread_as_bottom_layer(self):
        from route_completion import endpoint_layers
        self.assertEqual(endpoint_layers("Track [+1V8] on In2.Cu"), "I")
        self.assertEqual(endpoint_layers("Track [GND] on In3.Cu"), "C")
        self.assertEqual(endpoint_layers("Track [NET] on Unknown.Cu"), "")

    def test_generic_router_refuses_power_and_differential_pairs(self):
        from route_completion import route_policy
        for net in ("PD_1V2", "PD_2V7", "VBAT_RTC", "RS_VP", "RS_VN", "RS_C1P", "RS_C1N", "RS_C2P", "RS_C2N", "B1_BOOT", "OE_REG_CTRL", "+1V8", "+2V5_NEW", "VSYS", "VBUS_IN", "OE_MDI1_P", "PCIE_TX_N", "USB2_SS_RX_P", "MG_TXD0", "MG_RXC", "SD_CLK_R", "SD_D3"):
            self.assertFalse(route_policy(net)[0], net)
        self.assertTrue(route_policy("I2C_SDA")[0])
        self.assertFalse(route_policy("I2C_SDA", {"I2C_SCL"})[0])

    def test_ground_inner_routing_layer_is_recognized(self):
        import pcbnew
        from route_completion import track_routing_layer
        self.assertEqual(track_routing_layer(pcbnew.In3_Cu), "C")
        self.assertEqual(track_routing_layer(pcbnew.In2_Cu), "I")

    def test_power_tracks_are_never_generic_ripup_candidates(self):
        from types import SimpleNamespace
        grid = Grid.__new__(Grid)
        grid.protected = set()
        track = SimpleNamespace(GetNetname=lambda: '+1V8', IsLocked=lambda: False)
        self.assertFalse(grid.rippable(track))


    def test_checkpoint_requires_source_project_rules(self):
        import tempfile
        from route_completion import main
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.kicad_pcb"
            source.write_text("placeholder", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source project"):
                main(str(source), str(Path(folder) / "output.kicad_pcb"), 1)

    def test_checkpoint_refuses_existing_output(self):
        import tempfile
        from route_completion import main
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.kicad_pcb"
            target = Path(folder) / "output.kicad_pcb"
            source.write_text("source", encoding="utf-8")
            target.write_text("preserved", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "new output checkpoint"):
                main(str(source), str(target), 1)
            self.assertEqual(target.read_text(), "preserved")


    def small_board(self):
        import pcbnew
        board = pcbnew.BOARD()
        edge = pcbnew.PCB_SHAPE(board)
        edge.SetShape(pcbnew.SHAPE_T_RECT)
        edge.SetStart(pcbnew.VECTOR2I(0,0))
        edge.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(20),pcbnew.FromMM(20)))
        edge.SetLayer(pcbnew.Edge_Cuts)
        board.Add(edge)
        return board

    def test_npth_clearance_uses_physical_hole_rule(self):
        import math, pcbnew
        from route_completion import RES, W_TRK
        board = self.small_board()
        fp = pcbnew.FOOTPRINT(board); fp.SetReference("H1"); board.Add(fp)
        pad = pcbnew.PAD(fp)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetLayerSet(pcbnew.LSET.AllCuMask())
        pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(3.05),pcbnew.FromMM(3.05)))
        pad.SetDrillSize(pcbnew.VECTOR2I(pcbnew.FromMM(3.05),pcbnew.FromMM(3.05)))
        pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10.0145),pcbnew.FromMM(10.024)))
        fp.Add(pad)
        grid = Grid(board); grid.load(board)
        distances = [math.dist(grid.xy(x,y),(10.0145,10.024))-1.525-W_TRK/2
                     for y in range(150,250) for x in range(150,250) if grid.T["F"][y,x] == 0]
        self.assertGreaterEqual(min(distances), .2)

    def test_existing_same_net_via_is_reused_without_new_drill(self):
        import pcbnew
        board = self.small_board()
        net = pcbnew.NETINFO_ITEM(board,"CONTROL"); board.Add(net)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10),pcbnew.FromMM(10)))
        via.SetWidth(pcbnew.FromMM(.6)); via.SetDrill(pcbnew.FromMM(.3))
        via.SetViaType(pcbnew.VIATYPE_THROUGH); via.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu)
        via.SetNet(net); board.Add(via)
        grid = Grid(board); grid.load(board)
        result = grid.route("CONTROL",(10,10),"F",(10,10),"B")
        self.assertIsNotNone(result)
        self.assertEqual(result[1], [])

if __name__ == '__main__': unittest.main()

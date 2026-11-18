"""Regression requirements from manufacturer documents; no KiCad runtime needed."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from sx import parse, first, find
spec = importlib.util.spec_from_file_location("sorx_electrical_model", ROOT / "tools/design.py")
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)


class ElectricalRequirements(unittest.TestCase):
    def test_fram_ordering_code_matches_voltage_and_package(self):
        fram = model.DESIGN.parts["U1"]
        self.assertEqual(fram.mpn, "CY15V104QN-50SXI")
        self.assertEqual(fram.fp, "Package_SO:SOIC-8_5.23x5.23mm_P1.27mm")
        self.assertEqual(fram.pins["7"], "FRAM_DNU")
        self.assertEqual(fram.sym, "SORX:CY15V104QN")
        self.assertEqual(model.DESIGN.parts["R11"].value, "0")
        self.assertEqual(fram.pins["8"], "+1V8")

    def test_ksz9131_bias_resistor_is_6040_ohms(self):
        resistors = [p for p in model.DESIGN.parts.values() if set(p.pins.values()) == {"MG_ISET", "GND"}]
        self.assertEqual(len(resistors), 1)
        self.assertEqual(resistors[0].value, "6.04k")

    def test_verdin_uart_handshake_pin_numbers(self):
        self.assertEqual({n:model.VERDIN_PINS[n] for n in (133,135,141,143)},
                         {133:"UART_1_RTS",135:"UART_1_CTS",141:"UART_2_RTS",143:"UART_2_CTS"})

    def test_pd_controller_has_protected_auxiliary_supply(self):
        self.assertEqual(model.DESIGN.parts["U2"].pins["24"], "+4V5_PD")
        self.assertEqual(model.DESIGN.parts["U2"].pins["22"], "GND")
        aux = model.DESIGN.parts["U38"]
        self.assertEqual(aux.mpn, "LT3010EMS8E#PBF")
        self.assertEqual(aux.sym, "SORX:LT3010_ADJ")
        self.assertEqual(aux.pins, {"1":"+4V5_PD", "2":"PD_AUX_ADJ", "4":"GND",
                                   "5":"VBUS_IN", "8":"VBUS_IN", "9":"GND"})
        self.assertEqual(aux.nc, ("3", "6", "7"))

    def test_pd_enable_actually_controls_efuse(self):
        gate = model.DESIGN.parts["Q5"]
        self.assertEqual(gate.mpn,"PMV65XP,215")
        self.assertEqual(gate.pins, {"1":"PD_EN_SNK#", "2":"+4V5_PD", "3":"EF_SHDN#"})
        self.assertEqual(model.DESIGN.parts["R15"].pins,{"1":"+4V5_PD","2":"PD_EN_SNK#"})
        pull = model.DESIGN.parts["R116"]
        self.assertEqual(pull.pins, {"1":"EF_SHDN#", "2":"GND"})
        ohms = float(pull.value.removesuffix("k"))*1000
        # TI SHDN source current 10uA, PMV65XP IDSS 100uA at150C,
        # plus100nA gate leakage as a conservative extra allowance.
        self.assertLess(110.1e-6 * ohms * 1.01, .8)
        # With the auxiliary rail absent, its PMOS cannot source the load.
        self.assertLess(10.1e-6 * ohms * 1.01, .8)

    def test_auxiliary_regulated_bounds_and_minimum_load(self):
        def resistance(ref):
            return float(model.DESIGN.parts[ref].value.removesuffix("k"))*1000
        top,bottom=resistance("R117"),resistance("R118")
        lower=1.237*(1+top*.99/(bottom*1.01))
        upper=1.313*(1+top*1.01/(bottom*.99))+100e-9*top*1.01
        self.assertGreater(lower,4.1)  # STUSB4500 operating minimum
        self.assertLess(upper,5.0)  # TPS26631 SHDN recommended maximum
        self.assertGreaterEqual(lower/(resistance("R115")*1.01),.001)
        self.assertEqual(model.DESIGN.parts["R117"].pins,{"1":"+4V5_PD","2":"PD_AUX_ADJ"})
        self.assertEqual(model.DESIGN.parts["R118"].pins,{"1":"PD_AUX_ADJ","2":"GND"})

    def test_pd_sense_has_precision_clamp_and_rated_series_resistor(self):
        clamps = [p for p in model.DESIGN.parts.values() if p.mpn == "TL431BIPK"]
        self.assertEqual(len(clamps), 1)
        self.assertEqual(clamps[0].pins, {"1":"PD_CLAMP_REF", "2":"GND", "3":"PD_VS"})
        series = [p for p in model.DESIGN.parts.values() if p.ref.startswith("R")
                  and set(p.pins.values()) == {"VBUS_IN", "PD_VS"}]
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].value, "1k")
        self.assertEqual(series[0].fp, "Resistor_SMD:R_1206_3216Metric")

    def test_unquoted_bom_parts_remain_explicitly_unknown(self):
        import gen_bom
        self.assertEqual(gen_bom.est(model.DESIGN.parts["U38"]), (None, None, None))

    def test_bom_keeps_off_board_heatsink_and_screws(self):
        import gen_bom
        for ref in ("HS1", "SC1", "SC2", "SC3", "SC4"):
            self.assertTrue(gen_bom.include_part(model.DESIGN.parts[ref]))

    def test_power_budget_matches_three_independent_three_amp_bucks(self):
        import subprocess
        proc = subprocess.run([sys.executable, str(ROOT / "tools/power_budget.py")], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("USB 3 hub", proc.stdout)
        self.assertNotIn("6 A part", proc.stdout)
        self.assertIn("+5V_USB", proc.stdout)
        self.assertIn("independent", proc.stdout)

    def test_recovery_inhibits_host_power_and_isolates_sense(self):
        gates=[p for p in model.DESIGN.parts.values() if p.sym=="74xGxx:74LVC1G08"
               and p.pins.get("4")=="USB1_EN_SAFE"]
        self.assertEqual(len(gates),1)
        self.assertEqual(gates[0].pins["1"], "USB1_EN")
        self.assertEqual(gates[0].pins["2"], "RECOVERY#")
        self.assertEqual(model.DESIGN.parts["JP1"].pins,
                         {"1":"USB1_VBUS", "2":"USB1_DET_SOURCE", "3":"USB_REC_VBUS"})
        self.assertEqual(model.DESIGN.parts["JP2"].pins, {"1":"USB1_ID_STRAP", "2":"GND"})
        self.assertEqual(model.DESIGN.parts["J14"].pins, {"1":"USB_REC_VBUS", "2":"GND"})

    def test_recovery_pulldown_meets_high_drive_and_unpowered_leakage(self):
        # TI full-temperature bounds: U40 VOH at 100uA; U20 II=1uA,
        # U40 Ioff=10uA. U20 VIL=0.65V for VCC=3.0..3.6V.
        value = model.DESIGN.parts["R114"].value
        ohms = float(value.removesuffix("k")) * 1000
        self.assertLessEqual(1.95 / (ohms * .99) + 1e-6, 100e-6)
        self.assertLess((10e-6 + 1e-6) * ohms * 1.01, .65)

    def test_imon_resistor_keeps_full_monitor_range_below_1v8(self):
        # TPS2663 RevG max gain through its specified 0.6..6A range;
        # Toradex recommends ADC inputs remain within 0..1.8V.
        ohms = float(model.DESIGN.parts["R21"].value.removesuffix("k")) * 1000
        self.assertLessEqual(6 * 30.14e-6 * ohms * 1.01, 1.8)

    def test_usb3_esd_uses_matching_opposite_pads_for_flow_through(self):
        esd = model.DESIGN.parts["U19"]
        self.assertEqual(esd.sym, "SORX:TPD4E05U06_FLOWTHROUGH")
        for first_pad, opposite_pad, net in [("1","10","USB2_SSTX_P"),
                                             ("2","9","USB2_SSTX_N"),
                                             ("4","7","USB2_SSRX_P"),
                                             ("5","6","USB2_SSRX_N")]:
            self.assertEqual(esd.pins[first_pad],net)
            self.assertEqual(esd.pins[opposite_pad],net)
            self.assertNotIn(opposite_pad,esd.nc)
        self.assertEqual(esd.pins["3"],"GND")
        self.assertEqual(esd.pins["8"],"GND")

    def test_te_socket_locating_holes(self):
        fp = parse((ROOT / "hw/sorx.pretty/SODIMM-260_DDR4_TE_2309409-2.kicad_mod").read_text())
        holes = sorted((float(first(p,"at")[1]),float(first(p,"at")[2]),float(first(p,"drill")[1]))
                       for p in find(fp,"pad") if p[2] == "np_thru_hole")
        self.assertEqual(len(holes), 2)
        self.assertAlmostEqual(holes[1][0]-holes[0][0], 68.8, places=4)
        self.assertAlmostEqual(holes[0][2], 1.10, places=4)
        self.assertAlmostEqual(holes[1][2], 1.60, places=4)

    def test_te_socket_hold_down_land_pattern(self):
        fp = parse((ROOT / "hw/sorx.pretty/SODIMM-260_DDR4_TE_2309409-2.kicad_mod").read_text())
        pads = {str(p[1]):p for p in find(fp,"pad")}
        a,b = pads["261"],pads["262"]
        self.assertAlmostEqual(abs(float(first(a,"at")[1])-float(first(b,"at")[1])),67.60,places=4)
        for p in (a,b):
            self.assertAlmostEqual(float(first(p,"at")[2]),4.074,places=4)
            self.assertEqual(tuple(map(float,first(p,"size")[1:])), (3.5,4.6))


if __name__ == "__main__":
    unittest.main()

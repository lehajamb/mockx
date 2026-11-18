"""KiCad project file (net classes, design rules) for the SORX carrier."""
import json
def nc(name, clr, w, via=(0.6, 0.3), dpw=0.2, dpg=0.2):
    return {"name": name, "clearance": clr, "track_width": w, "via_diameter": via[0], "via_drill": via[1], "diff_pair_width": dpw, "diff_pair_gap": dpg,
            "diff_pair_via_gap": 0.25, "microvia_diameter": 0.3, "microvia_drill": 0.1, "bus_width": 12, "line_style": 0,
            "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)", "wire_width": 6}
CLASSES = [nc("Default", 0.15, 0.15), nc("DP100", 0.18, 0.127, dpw=0.127, dpg=0.18), nc("DP90", 0.18, 0.155, dpw=0.155, dpg=0.18),
           nc("PWR", 0.15, 0.25), nc("PWR_HI", 0.15, 0.35)]
PATTERNS = [("DP100", "*_MDI?_?"), ("DP90", "USB2_SS*"), ("DP90", "USB?_D_?"), ("DP90", "DBG_D_?"), ("DP90", "FT_D?"), ("DP90", "PCIE_TX_?"), ("DP90", "PCIE_RX_?"),
            ("DP90", "PCIE_CLK_?"), ("DP90", "OE_PET_?"),
            ("PWR_HI", "VBUS_IN"), ("PWR_HI", "VIN_PROT"), ("PWR_HI", "VSYS"), ("PWR_HI", "+5V"), ("PWR_HI", "+5V_USB"), ("PWR_HI", "B*_SW"),
            ("PWR", "+4V5_PD"), ("PWR", "USB_REC_VBUS"), ("PWR", "+3V3"), ("PWR", "+1V8"), ("PWR", "+1V2_PHY"), ("PWR", "+3V3_SD"), ("PWR", "+3V3_DBG"), ("PWR", "VBUS_DBG*"), ("PWR", "USB?_VBUS"),
            ("PWR", "PD_1V2"), ("PWR", "PD_2V7"), ("PWR", "VBAT_RTC"), ("PWR", "RS_VP"), ("PWR", "RS_VN"), ("PWR", "FAN_5V"), ("PWR", "OE_0V9"), ("PWR", "OE_1V5"), ("PWR", "MG_1V2_PLL")]
RULES = {"allow_blind_buried_vias": False, "allow_microvias": False, "max_error": 0.005, "min_clearance": 0.15, "min_connection": 0.0, "min_copper_edge_clearance": 0.25,
         "solder_mask_to_copper_clearance": 0.0, "min_hole_clearance": 0.2, "min_hole_to_hole": 0.25, "min_microvia_diameter": 0.2, "min_microvia_drill": 0.1,
         "min_resolved_spokes": 1, "min_silk_clearance": 0.0, "min_text_height": 0.6, "min_text_thickness": 0.08, "min_through_hole_diameter": 0.3,
         "min_track_width": 0.127, "min_via_annular_width": 0.13, "min_via_diameter": 0.6, "solder_mask_clearance": 0.0, "solder_mask_min_width": 0.0,
         "use_height_for_length_calcs": True}
def classify(net):
    import fnmatch
    for c, p in PATTERNS:
        if fnmatch.fnmatchcase(net, p): return c
    return "Default"
def write(path, name, severities=None):
    d = {"board": {"3dviewports": [], "design_settings": {"defaults": {}, "diff_pair_dimensions": [{"gap": 0.18, "via_gap": 0.25, "width": 0.127}, {"gap": 0.18, "via_gap": 0.25, "width": 0.155}],
                                                          "rule_severities": severities or {}, "rules": RULES,
                                                          "track_widths": [0.0, 0.127, 0.15, 0.155, 0.25, 0.35, 0.5, 0.8], "via_dimensions": [{"diameter": 0.0, "drill": 0.0}, {"diameter": 0.6, "drill": 0.3}, {"diameter": 0.8, "drill": 0.4}]},
                   "layer_presets": [], "viewports": []},
         "boards": [], "cvpcb": {"equivalence_files": []}, "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
         "meta": {"filename": name + ".kicad_pro", "version": 1},
         "net_settings": {"classes": CLASSES, "meta": {"version": 3}, "net_colors": None, "netclass_assignments": None,
                          "netclass_patterns": [{"netclass": c, "pattern": p} for c, p in PATTERNS]},
         "pcbnew": {"last_paths": {}, "page_layout_descr_file": ""}, "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []}, "sheets": [], "text_variables": {}}
    json.dump(d, open(path, 'w'), indent=2)

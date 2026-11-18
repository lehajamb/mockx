# Debug power repair and selected via filling

**NOT READY FOR FABRICATION.** The debug USB power input now has an electrically connected layout candidate. Its three specifically identified power-pad vias require a filled, planarized and copper-capped manufacturing process approved by the board fabricator and assembler. They must not be delivered as open holes or treated as ordinary underside-tented vias.

The native source contained three separate VBUS_DBG_RAW islands: the J10 A4/B9 contact pair, the J10 A9/B4 contact pair, and FB2 input. The routing inspection found the connector's two NPTH pegs beneath the power exits and USB data routing above them. The first outside-pad trial correctly failed hole and copper clearance; it is retained as rejected evidence. No USB data track, connector peg or required pad was removed to close the power net.

The corrected design uses 0.60 mm copper / 0.30 mm nominal drill through-vias at these exact power-pad centers:

| Power land | Native PCB X, Y mm | Required treatment |
|---|---|---|
| FB2.1 | 115.6525, 106.4200 | Filled and copper capped, planar solderable top |
| J10.A4 / B9 | 115.6000, 110.6400 | Filled and copper capped, planar solderable top |
| J10.A9 / B4 | 120.4000, 110.6400 | Filled and copper capped, planar solderable top |

A 0.25 mm B.Cu bus joins the three lands. Its 100 mA engineering allocation covers the debug UART island, not a general USB power output. Current does not depend on an assumed equal split between the USB-C contact pairs. The barrel copper, not the resin, carries current. Supplier minimum plating, final copper thickness and actual debug load remain qualification items.

Filled/capped through-vias are an established via-in-pad process: [Eurocircuits' process specification](https://www.eurocircuits.com/technical-guidelines/pcb-design-guidelines/via-filling/) identifies Type VII protection and explicitly supports via-in-pad. Its published end-size range encompasses 0.30 mm and its board-thickness range encompasses this candidate. This supports process feasibility, not an approved quote or manufacturing release for this board. The supplier requires unambiguous identification of the holes to fill. [Its assembly guidance](https://www.eurocircuits.com/technical-guidelines/pcb-assembly-guidelines/via-in-pad/) also calls attention to solder escape and tombstoning. The assembler must approve cap planarity, soldermask/paste treatment and the ferrite/connector joints.

`tools/export_filled_vias.py FINAL_BOARD.kicad_pcb OUTPUT.json` identifies the exact via and pad UUIDs, net, coordinates and board hash. It fails if any selected pad, mapping, size or single-via condition is wrong. Its native-coordinate manifest supplements the supplier's drill/CAM review; it is not an NC drill file. U2/U13 exposed-pad thermal vias have separate process requirements. This is not a blanket permission to place vias in signal pads.

The accepted isolated debug proof changed 35 required opens to 33, with 95 other findings unchanged and no new finding. The subsequent accepted power-bridge proof closes the two reset/pullup 1.8 V islands and the Q2/R12 input-sense branch, reaching 30 opens and 93 other findings. Both proofs pass a strengthened per-net gate: no net gains an open, and the specifically repaired nets have zero required opens. The remaining U13.4 supply open is explicitly retained for coordinated PHY fanout work.

Evidence: `validation/debug-power-proof-01/` (rejected outside-pad attempt), `validation/debug-power-proof-02/` (accepted geometry and stronger-gate review), `validation/power-bridge-proof-01/`, and `validation/power-acceptance/` (two substantive failing-before regression cases and three passing tests). These counts are checkpoint results, not a fabrication release.

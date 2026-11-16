# Stackup, impedance and fabrication notes

## Stackup (1.6 mm, 4 layers)

| Layer | Use | Material | Thickness |
|---|---|---|---:|
| F.Cu | Components, signals, pairs, GND pour | Copper, 1 oz finished | 0.035 mm |
| Prepreg | — | FR-4, Er about 4.1 (1080/3313 class) | 0.100 mm |
| In1.Cu | Solid GND plane | Copper, 1 oz | 0.035 mm |
| Core | — | FR-4, Er about 4.5 | 1.265 mm |
| In2.Cu | Solid GND plane | Copper, 1 oz | 0.035 mm |
| Prepreg | — | FR-4, Er about 4.1 | 0.100 mm |
| B.Cu | I210 block, signals, pairs, GND pour | Copper, 1 oz finished | 0.035 mm |

Both inner layers are unbroken ground planes, so every outer-layer trace has an adjacent reference plane 0.1 mm away. Power is routed on the outer layers.

## Controlled impedance

Computed by `tools/impedance.py` (IPC-2141 closed form; edge-coupled microstrip over the 0.1 mm prepreg).

| Class | Use | Width mm | Gap mm | Z0 single ohm | Zdiff ohm | Target |
|---|---|---:|---:|---:|---:|---|
| DP100 | Ethernet MDI | 0.127 | 0.18 | 54.7 | 100.1 | 100 ohm +/-10% |
| DP90 | USB 2.0, USB 3.0, PCIe | 0.155 | 0.18 | 49.1 | 89.8 | 90 ohm +/-10% (PCIe 85 ohm +/-15%) |
| Default | single-ended signals | 0.150 | - | 50.0 | - | 50 ohm nominal |

Order the board as impedance controlled and let the fabricator adjust widths to their own material with a field solver and a test coupon. The closed form is good to a few percent; it is not a substitute for the fab's model.

## Design rules used

| Rule | Value |
|---|---:|
| Minimum track / clearance | 0.127 mm / 0.15 mm |
| Pair and power class clearance | 0.18 mm (must stay below the 0.20 mm pad gap of the SoM socket and USB-C pads) |
| Via | 0.6 mm pad, 0.3 mm drill, through only |
| Copper to board edge | 0.25 mm |
| Hole to hole | 0.25 mm |
| Finish | ENIG (0.5 mm pitch socket and QFNs) |
| Solder mask | Green LPI both sides; silkscreen white, top only in rev A |

## Fabrication package

- `hw/fab/gerbers/` — RS-274X, one file per layer, plus board outline.
- `hw/fab/drill/` — Excellon, plated and non-plated separate, with a PDF drill map.
- `hw/fab/assembly/` — pick-and-place CSV (both sides), top and bottom assembly drawings.
- `hw/sorx-fab-revA.zip` — the three folders above in one archive.

## Assembly notes

- Two-sided SMT. Bottom side carries only the I210 block (U10, U11, U12, Y1 and their passives), all under 1.2 mm.
- Through-hole parts: J4, J6, J7 (RJ45 magjacks), J8, J9 (USB-A, pin-in-paste capable), J11, J13, SW1, SW2.
- J5 (SMA) is an edge-launch part soldered on both faces. J2 (AUX header), R20, R85 and the PPS termination are not fitted; see the DNP column of the BOM.
- J1 is a 0.5 mm pitch socket: use a 0.1 mm stencil or a step-down in that area.

## Revision 2026-11-16: layer 3 carries the supply rails

Layer 2 (In1) stays a solid GND plane. Layer 3 (In2) is no longer a solid GND plane: it carries the supply rails as routed tracks (0.25 to 0.8 mm) with GND fill around them. Signals stay on layers 1 and 4. Trace geometry and impedance targets are unchanged. Consequence: pairs on layer 4 cross supply routes in their reference layer; review these crossings before release (gate G3).

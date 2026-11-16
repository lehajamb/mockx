# Design review notes and release gates (rev A)

Rev A is a complete, rule-checked design package produced without hardware and without a human layout review. Treat the first build as a prototype spin. The items below are what a reviewer must close before releasing boards to fabrication, in priority order.

## Release gates (must close before ordering PCBs)

| # | Item | Why it is open | How to close |
|---|---|---|---|
| G1 | **SoM socket land pattern (J1).** | KiCad has no TE 2309409-2 footprint and the TE drawing could not be read as text. The pattern is taken from the TE 2309413 (same DDR4 SODIMM family) footprint in the open-hardware Antmicro Jetson Nano baseboard; its 68.80 mm peg spacing matches the dimension in the Toradex Verdin land-pattern figure. | Overlay `hw/sorx.pretty/SODIMM-260_DDR4_TE_2309409-2.kicad_mod` on TE customer drawing C-2309409 and on the Toradex Verdin carrier land pattern (pads, 2 pegs, 2 hold-downs). |
| G2 | **Module standoffs not fitted.** | Their positions come from the Toradex land-pattern drawing, which was not available as numbers. Toradex states standoffs are optional; the socket latches retain the module. | Add the four standoff holes from the Toradex footprint if vibration or the Toradex heatsink is required. |
| G3 | **Layout is autorouted.** | Freerouting routed signals; pairs are loosely coupled and not skew-tuned (see `verification-report.md` for per-pair length and skew). No SI/PI simulation. | Layout review; hand-tune USB 3.0 and PCIe pairs to < 0.15 mm intra-pair skew; confirm return-path vias next to every pair layer change. |
| G4 | **Power distribution uses widened tracks, not pours.** | The autorouter can only leave 0.5 mm pitch pads with narrow tracks. A post-process widens each power segment as far as clearance allows; short neck-downs remain (see the width table in the verification report). | Replace +5V, +5V_USB, VSYS and +3V3 trunks with copper pours; check current density at the neck-downs. |
| G5 | **Component values taken from application-circuit knowledge, not re-read from datasheets in this pass.** | See the table below. | Check each against the current datasheet. |

## Values and connections to confirm against datasheets (G5)

| Ref | Part | Assumption made |
|---|---|---|
| R16-R18 | TPS26631 UVLO/OVP ladder | 1.2 V thresholds: on at 12.1 V, off at 22.6 V |
| R19 | TPS26631 ILIM | I_lim = 18 / R(kohm): 8.2 k gives 2.2 A |
| R21, R22 | TPS26631 IMON | Gain assumed about 28 uA/A, so 24.9 k gives 1.5 V at 2.2 A into the SoM ADC (1.8 V max) |
| R20 (DNP) | TPS26631 MODE | Left open = default fault response; confirm which |
| Q1, Q2 | Reverse-polarity FETs | Q1 source to IN_SYS, drain to IN, gate to B_GATE; Q2 gate to DRV, drain to B_GATE, source to IN_SYS (from the TI pin description) |
| U2 | STUSB4500 | VSYS tied to GND (unused), DISCH unconnected, VBUS_VS_DISCH through 1 k, factory NVM = 20 V/1 A and 15 V/1.5 A (must be reprogrammed to 2 A) |
| D2 | SMAJ22A | Clamp voltage (35.5 V) is above the STUSB4500 28 V absolute maximum during a surge; consider TVS2200 |
| U4-U6 | LMR33630 | V_FB = 1.0 V: 100 k / 24.9 k = 5.02 V, 100 k / 43.2 k = 3.31 V; 10 uH at 400 kHz |
| U10 | I210-AT | 39 nF between CTOP and CBOT, 4.99 k RSET, internal switched-capacitor regulator feeds 1.5 V and 0.9 V; strap and JTAG pulls per Intel reference design from memory; LED0/LED1 default functions |
| U13 | KSZ9131RNX | Uses the KiCad KSZ9031RNX symbol (pin compatible); straps give RGMII, all speeds, PHY address 7, single-LED mode; 1.2 V core from an external LDO (LDO_O unused) |
| J4, J6, J7 | Pulse JK0654219NL | Pin numbers from the KiCad library symbol; LED colour and polarity per pin not confirmed |
| U32 | MAX6369 | SET2:SET1:SET0 = 011 disabled, 111 = 60 s, 110 = 10 s (table confirmed by web search) |
| U17, U21 | TPS2065C | Same pinout as the TPS2051C library symbol; enable threshold needs the 3.3 V buffer |
| U1 | CY15B104QN | 1.8 V operation and SOIC-8 150 mil body |

## Known limitations accepted for rev A

- Not sub-10 us. See `som-comparison.md`.
- USB 2 is USB 2.0 only and doubles as the recovery port (A-to-A cable). USB 1 is the only SuperSpeed port. This avoids a USB 3 hub and two extra crystals; the checkpoint diagram showed two USB 3.0 ports.
- The debug USB-C port carries the console only. Recovery moved to USB 2 for the same reason.
- No ESD devices on the microSD lines or the RS-232 console beyond the transceiver's own 15 kV rating.
- The enclosure heights of connectors and buttons are nominal catalogue values, not checked against vendor 3D models.
- Boot from microSD depends on the boot-loader order; the SoM's primary boot device is eMMC.
- ERC was run with KiCad 7.0.11 through GUI automation (KiCad 7 has no command-line ERC).
- Prices outside the seven parts with a dated distributor check are estimates.

## Open items at handoff (2026-11-16)

- Layout is incomplete: see `docs/verification-report.md` (Result) and `docs/reports/open-connections.txt`. 10 signal/supply connections and 41 GND pad groups are unconnected. Do not order boards from `hw/sorx-fab-revA.zip`.
- The 41 GND groups are most likely fine-pitch GND pins (module socket J1, PHYs) whose escape is fenced in by neighbouring tracks. Not confirmed pad by pad.
- U13 pin 4 (+1V2_PHY) is fenced in by the locked MDI pairs; fix needs pair routing re-run with the fan-out vias in `tools/route.py` (untested code).
- Pairs are not length-matched (table in the verification report).
- `tools/patch_route.py` rip-up mode does not converge; it is switched off with PATCH_NORIP=1.

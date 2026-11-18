# Independent review of the supplied SORX design

**Status: NOT READY FOR FABRICATION.** Review baseline: the supplied
`sorx-hw-2026-11-16-final.zip`, dated 2026-11-16. The word `final` in its filename
does not indicate release approval. The supplied fabrication ZIP must not be
used to order boards in this state.

This document records the earlier independent inspection of the native PCB,
stored netlist, BOM, scripts, raw reports, mechanical data and manufacturing
outputs, including selected manufacturer-document checks. It is separate from
the imported [upstream handoff](../hardware/HANDOFF.md) and
[verification report](../hardware/docs/verification-report.md), which remain
historical evidence. The handoff's instructions and priorities do not authorize
deferring release blockers.

No new KiCad ERC/DRC, CAD-kernel interference calculation, signal-integrity
simulation or physical hardware test was performed during that independent
review. Archived check results are identified below. Independent calculations
and file comparisons do not certify that the electrical model is correct.

Use the stable finding IDs below in the before/after comparison. Their closure
requirements are in [release-criteria.md](release-criteria.md).

## Evidence that passed consistency checks

| Check | Baseline evidence and limit |
|---|---|
| External handoff and verification report | Byte-identical to their copies in the hardware archive |
| Stored schematic netlist versus declared net map | 262 of 262 nets agree; no extra multi-node nets; 179 single-node no-connect nets |
| PCB versus declared pad nets | 1,500 pads on 348 footprints checked; zero assignment mismatches |
| Final PCB inventory | 262 named nets, 3,317 track segments, 1,180 vias; filled GND zones on all four copper layers |
| Mechanical placement versus final PCB | All 348 component records agree on reference, position, rotation and board side |
| Pick-and-place versus final PCB | All 320 exported entries agree on position, rotation and side; omitted items are four mounting holes, 20 testpoints and four DNP parts |
| Nested fabrication ZIP versus loose outputs | All 24 files are byte-identical after normalizing archive paths |
| Archived ERC | Reports zero errors and zero warnings; not a newly executed check |

The design model, BOM and socket footprint were unchanged from the preceding
hardware archive. A shared model makes consistency valuable, but a wrong
assumption in that model can propagate into the schematic, PCB and BOM together.

## R01 — Required electrical connectivity is incomplete

The [raw final DRC report](../hardware/docs/reports/drc-report.txt) contains
**51 unconnected items: 10 signal/supply connections and 41 GND findings**.
The [open-connections list](../hardware/docs/reports/open-connections.txt)
identifies the signal/supply failures. They include:

- `MG_TXD0`: no track segments between J1 pin 221 and U13 pin 19. The management
  Ethernet transmit interface is incomplete.
- `USB1_VBUS_DET`: no track segments between J1 pin 159 and R83 pin 2.
- U13 pin 4 on `+1V2_PHY`: the required supply pad remains disconnected. The
  nearest same-net front-layer track centerline is 2.5 mm from the pad center.
- `VBUS_DBG_RAW`: the debug connector-to-filter power connection is incomplete.
- `USB1_OC#`, `PWR_FLT#`, `OE_0V9` and `MG_LED2`: additional reported opens.

Do not describe the 41 GND findings as 41 established ungrounded IC pins.
Thirty-eight entries name only zone endpoints; three name pad pairs
(`C3/U12`, `Y1/R20`, `C1/U11`). The handoff's explanation that they are mostly
fine-pitch pin escapes is an unverified hypothesis. Inspect the actual connected
copper components and remove unnecessary isolated copper or connect required
ground groups as appropriate.

## R02 — FRAM package and supply selection need correction

[The model](../hardware/tools/design.py), lines 168–170, selects
`CY15B104QN-50SXI` on `SOIC-8_3.9x4.9mm_P1.27mm`, and the final PCB contains that
narrow U1 footprint. The selected ordering code uses the **208 mil SOIC
package**, not the 150 mil body assumed in the upstream review notes.

The B variant also has a 1.8 V minimum supply. A nominal 1.8 V rail therefore
has no negative tolerance allowance. The V variant specifies 1.71–1.89 V, but
changing the part is an engineering decision requiring a complete ordering-code,
footprint and electrical check, not an automatic substitution.

Evidence: [Infineon CY15B104QN/CY15V104QN datasheet](https://www.infineon.com/assets/row/public/documents/10/49/infineon-cy15b104qn-cy15v104qn-4-mb-excelon-lp-ferroelectric-ram-f-ram-serial-spi-512k-8-industrial-datasheet-en.pdf).

## R03 — Exact SoM socket land pattern remains unverified

The [custom socket footprint](../hardware/hw/sorx.pretty/SODIMM-260_DDR4_TE_2309409-2.kicad_mod)
was derived from a related TE 2309413 pattern. Broad dimensions agree with the
TE 2309409-2 product information: 260 positions, 0.5 mm pitch, 8.2 mm row
spacing and 5.2 mm stack height. This is a partial check only.

Locating holes, hold-down pads, numbering/orientation and seating geometry have
not been fully compared with the exact manufacturer drawing. No additional
socket geometry defect was demonstrated by the review; **the gate is open
because validation is incomplete**. Confirm module retention and the chosen
heatsink's mounting requirements together with R06.

Evidence: [TE 2309409-2 product and drawing links](https://www.te.com/en/product-2309409-2.html),
[Toradex Verdin carrier-board guide](https://docs.toradex.com/108140-verdin-carrier-board-design-guide.pdf).

## R04 — High-speed routing needs engineering correction

Independent sums of final-board track lengths reproduce these differences:

| Pair | P length (mm) | N length (mm) | Difference (mm) |
|---|---:|---:|---:|
| `OE_MDI1` | 12.586 | 16.950 | 4.365 |
| `OE_MDI3` | 10.220 | 20.977 | 10.757 |
| `PCIE_RX` | 20.736 | 18.303 | 2.433 |
| `PCIE_CLK` | 32.269 | 30.242 | 2.028 |

The two `OE_MDI` examples were traced as direct, unbranched, bottom-layer
IC-to-jack paths with no vias. Their large mismatch cannot be explained by
counting unrelated ESD stubs. Intel specifies matching within **1.25 mm** for
I210 MDI pairs and emphasizes symmetry. The remaining pair table in the
supplied report includes net-length sums that may include branches; use real
source-to-destination paths for final qualification.

The actual board also contains bottom-layer differential traces crossing
In2 supply tracks in their adjacent reference layer. Geometric inspection found
21 such centerline crossings among the P/N traces examined; this is not a full
field-solver analysis. For example, `PCIE_TX_P` crosses +3V3 and +5V routes.
These are reference-path discontinuities, **not electrical shorts between
layers**. A correct remedy must address continuity, pair coupling and layer
transitions as well as length.

The project has no custom pair-skew rule file enforcing the stated targets.
Clean clearance DRC alone cannot establish controlled impedance or reliable
PCIe, USB and Ethernet operation.

Evidence: [Intel I210 datasheet, sections 12.5.6.8 and 12.5.6.11](https://cdrdv2-public.intel.com/333016/333016%20-%20I210_Datasheet_v_3_7.pdf),
[Toradex layout guide](https://docs.toradex.com/102492-layout-design-guide.pdf),
[current stackup notes](../hardware/docs/stackup-and-fab-notes.md).

## R05 — Power protection, distribution and test procedure need review

The model connects the SMAJ22A TVS and STUSB4500 supply directly to raw VBUS,
ahead of the eFuse. The TVS specifies a **35.5 V maximum clamp**, while the
STUSB4500 VDD absolute maximum is **28 V**. Those ratings do not establish
adequate controller protection under the specified surge condition. This is
not a claim that ordinary 20 V operation necessarily fails.

The [bring-up checklist](../hardware/docs/bring-up-checklist.md), line 16,
instructs applying −20 V to raw VBUS and expecting no current. The fitted
unidirectional TVS will be forward-biased under reverse polarity. Correct the
test and its expected behavior before using it on hardware.

The PD controller's `PD_EN_SNK#` signal reaches a pull-up and testpoint, not the
downstream power-path controls. Programming a “power only above 5 V” setting
does not gate this board through that output. The eFuse UVLO separately blocks
5 V; it is voltage-based protection rather than PD-contract-qualified load
control. Review startup, negotiated current, reset and fault behavior explicitly.

Power tracks were widened wherever clearance allowed. Width summaries are not
proof of allowable current, voltage drop or regulator-loop quality. Review
trunks, neck-downs, return paths, decoupling placement, tolerances and thermal
requirements against the actual loads. Selected nominal buck-divider and
current-monitor calculations agreed with manufacturer data; that does not close
the complete power review.

Evidence: [STUSB4500 datasheet](https://www.st.com/resource/en/datasheet/stusb4500.pdf),
[Littelfuse SMAJ datasheet](https://www.littelfuse.com/~/media/electronics/datasheets/tvs_diodes/littelfuse_tvs_diode_smaj_datasheet.pdf.pdf),
[TI TPS2663 datasheet](https://www.ti.com/lit/ds/symlink/tps2663.pdf),
[TI LMR33630 datasheet](https://www.ti.com/lit/ds/symlink/lmr33630.pdf).

## R06 — Mechanical validation fails and has coverage gaps

The saved [fit-check JSON](../hardware/mech/fit-check.json) identifies **J9,
the USB 2/recovery connector, with 5.002 mm³ enclosure interference**. This is
an archived CAD result, not an independently recomputed volume. The port
alignment booleans can all pass while another portion of the connector envelope
intersects the enclosure.

The placement synchronization problem from the preceding archive is fixed:
348 of 348 mechanical component records match the final PCB. Four PCB mounting
holes also match the enclosure boss coordinates. Remaining limitations include:

- [The mechanical generator](../hardware/tools/gen_mech.py), lines 230–231,
  converts an intersection exception into zero interference.
- Its collision check covers selected parts against the enclosure, not all
  parts or component-to-component contact, cable insertion and assembly access.
- Bottom-side heights are capped at 1.2 mm. The reported 4.8 mm floor clearance
  is derived from that assumption rather than vendor-verified maximum heights.
- The fan envelope is 40 mm high in a 40 mm cavity, with no nominal vertical
  tolerance allowance.
- Module standoffs are absent and the modeled heatsink is a generic envelope;
  compatibility with a specific purchasable mounting arrangement is unresolved.

The [thermal estimate](../hardware/docs/thermal-estimate.md) is explicitly
unmeasured. Its arithmetic gives approximately 9.9 K bulk air rise and 96.6 °C
SoC temperature at 35 °C ambient, using assumed airflow and thermal
resistances. It does not verify sustained operation at that ambient temperature.

## R07 — Verification can incorrectly pass a later revision

The supplied report correctly fails the current board, but the workflow has
defects that must be fixed before relying on a future green result:

- [The raw DRC](../hardware/docs/reports/drc-report.txt), lines 682–685, reports
  two GND vias at **(36.9, 41.5)**. The waiver calls them duplicate library
  mounting pads. Two distinct via objects and two identical
  `X36.9Y-41.5` commands in the
  [PTH drill file](../hardware/hw/fab/drill/sorx-PTH.drl) confirm the explanation
  is incorrect.
- [The verification script](../hardware/tools/verify.py), lines 128–133,
  excludes mechanical interference, port alignment and the schematic-netlist
  subprocess result from its pass decision. It normally exits successfully
  even when the generated report says “Not passed.”
- [The export script](../hardware/tools/export.py) prints subprocess failures
  and continues packaging available files, with no verification prerequisite.
- Several waiver categories are accepted broadly. Reviewed exceptions must
  identify the affected objects and evidence, rather than suppress a category.
- [The schematic generator](../hardware/tools/gen_sch.py) automatically adds
  power flags and broadly types socket signals as bidirectional. ERC therefore
  provides limited evidence about actual power sources and signal directions.
- The negative-ERC-test claim is a hard-coded report sentence; its raw result
  is not archived. Reproduce it if retaining that claim.

The 255 other DRC findings comprise 240 warnings and 15 errors before waivers,
in addition to the 51 unconnected errors. “Zero unwaived” depends on valid
exceptions; it is not equivalent to zero raw DRC findings.

## R08 — Release outputs and documentation need reconciliation

The exported files are internally consistent, but they describe an incomplete
board. Regeneration must follow corrections and must identify one exact source
revision and tool environment.

The imported README still contains “Not started” and pre-schematic status
alongside a later status update. Stackup notes describe both inner layers as
unbroken GND before an appended correction says In2 carries supplies. The
upstream design-review opening says “complete, rule-checked” despite later
explicitly listing open connections. Replace these contradictory statements in
the active design documentation when correcting the design; preserve the
original baseline unchanged.

Recovery instructions also need correction and validation. Toradex describes
recovery asserted at cold power-on, with no bootable SD card. Do not claim that
the USB ID pull-down alone prevents ROM recovery: the official errata states
that recovery forces client mode independently of that pin.

Evidence: [Verdin iMX8M Plus module datasheet](https://docs1.toradex.com/110977-verdin_imx8m_plus_v1.1_datasheet.pdf?v=2),
[official errata](https://docs.toradex.com/109492-verdin-imx8m-plus-errata.pdf).

## R09 — Sourcing and programming deliverables are incomplete

Only **7 of 39 active/electromechanical BOM lines** are marked checked in the
supplied [stock check](../hardware/bom/stock-check.csv). This is archived listing
evidence, not a new availability check. Other prices and availability remain
estimates or unknowns; alternates may require different footprints.

The archive does not contain a complete STUSB4500 configuration/programming and
read-back workflow or an I210 NVM/MAC provisioning workflow. Document the exact
assets, legitimate acquisition requirements, versions and verification steps.
Do not invent proprietary images, availability or test results.

Completing this review's design gates produces a qualified fabrication
candidate. It does not establish that an unbuilt board boots, meets latency
targets or passes electrical, thermal and compliance measurements.

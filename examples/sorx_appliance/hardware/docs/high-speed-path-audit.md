# Endpoint routing audit and constraints

**NOT READY FOR FABRICATION.** This audit detects geometric defects and records review evidence. It does not simulate channels, qualify impedance or certify hardware operation. `high_speed_audit.py` returns nonzero while required geometry or qualification gates remain open.

## Reproducible scope

[high_speed_audit.py](../tools/high_speed_audit.py) measures 21 named differential channels from explicit component-pad endpoints: the three four-pair MDI ports, PCIe TX/RX/clock, USB 2.0 and SuperSpeed, and both debug USB-C orientations. The RX PCIe channel includes the two routed nets on either side of C74/C75. Debug USB includes its 27-ohm series resistors. A changed or missing series component produces a failure instead of silently terminating a measurement at the capacitor.

The graph nodes copper centerlines at same-layer intersections and connects actual pad polygons and explicit vias. It preserves track UUIDs, exact endpoint reference/pad numbers, source PCB hash, selected edges, per-layer sequence and via identities. Same-XY crossings on different layers cannot connect without a via. Series-device internal delay and via-barrel length are marked unknown instead of invented. The reported planar length includes copper travel to pad centers; electrical delay through components, connector pins, SoM traces and packages is outside that measurement.

Branches and alternate copper outside the chosen endpoint path are recorded separately. The old `pairs.py` net-total measurement remains available as a diagnostic, but cannot prove path matching when ESD branches or loops are present. Unsupported arcs, overlapping centerlines, missing endpoints, incomplete paths and malformed reference geometry are explicit errors. Geometry touching only by trace width without an exact centerline/pad intersection may conservatively report an incomplete path; such cases require actual copper review rather than assumed connectivity.

The source PCB for this baseline audit is byte-identical to the preserved ZIP: SHA-256 `4b0b5f18692eecd80ba4ca10c9bc060dbb6584ba1aa6668b223d5afe200c771a`. See [raw baseline report](../validation/high-speed/baseline-path-audit.json), [audit command/exit status](../validation/high-speed/baseline-audit-run.json) and [six regression tests](../validation/high-speed/tests.log). The tests cover branch exclusion, explicit series-capacitor continuity, layer isolation, reference slots/foreign copper, duplicate copper and geometry-scalar serialization.

## Applied geometric targets and source qualification

The [Toradex Layout Design Guide](https://docs.toradex.com/102492-layout-design-guide.pdf), tables 5, 7, 8 and 9, supplies carrier targets: PCIe/SuperSpeed intra-pair difference below 0.15 mm; MDI below 0.25 mm; USB 2.0 below 1.1 mm. Limits are approximations to the guide's time-skew budgets, so stackup-dependent delay must also be checked. PCIe device-down length is below 300 mm, USB below 200 mm and MDI approximately 100 mm maximum. Two vias per MDI conductor and USB SuperSpeed connector conductor are the selected budgets; PCIe RX device-down permits four, TX two. The clock via budget remains unspecified rather than fabricated. The guide's contradictory USB3 pair-spacing inequality is not silently adopted.

The [Intel I210 datasheet](https://cdrdv2-public.intel.com/333016/333016%20-%20I210_Datasheet_v_3_7.pdf), section 12.5.6.8, separately specifies MDI matching within 1.25 mm and emphasizes symmetry. The report preserves both that device-specific check and the stricter carrier target. Intel section 12.1.4 directs PCIe routing questions to Intel; complete I210-specific acceptance is still required. Microchip KSZ9131-specific acceptance must be reconciled with the provisional carrier target rather than declaring the generic target to be its datasheet requirement.

Impedance targets, reference geometry and spacing must be reconciled with the selected module, devices and fabricator's actual stackup. A `DP90` or `DP100` netclass name does not measure impedance. In particular, PCIe reference-clock requirements and module/device terminations need an interface-specific decision, not automatic inheritance from data lanes.

## Baseline findings

| Channel | Endpoint planar mismatch mm | Net-total diagnostic mm | Carrier target mm | Vias P / N |
|---|---:|---:|---:|---:|
| MD_MDI0 | 0.193 | 0.256 | 0.250 | 1 / 1 |
| MD_MDI1 | 0.694 | 0.635 | 0.250 | 1 / 1 |
| MD_MDI2 | 0.840 | 0.779 | 0.250 | 1 / 1 |
| MD_MDI3 | 1.031 | 1.024 | 0.250 | 1 / 1 |
| OE_MDI0 | 0.491 | 0.491 | 0.250 | 0 / 1 |
| OE_MDI1 | 4.365 | 4.365 | 0.250 | 0 / 0 |
| OE_MDI2 | 0.043 | 0.043 | 0.250 | 0 / 0 |
| OE_MDI3 | 10.757 | 10.757 | 0.250 | 0 / 0 |
| MG_MDI0 | 0.696 | 0.635 | 0.250 | 0 / 0 |
| MG_MDI1 | 1.149 | 1.088 | 0.250 | 0 / 1 |
| MG_MDI2 | 0.635 | 0.635 | 0.250 | 0 / 0 |
| MG_MDI3 | 4.388 | 4.388 | 0.250 | 0 / 0 |
| PCIE_TX | 1.055 | 1.055 | 0.150 | 1 / 1 |
| PCIE_RX | 0.906 | 0.930 | 0.150 | 1 / 1 |
| PCIE_CLK | 2.028 | 2.028 | 0.150 | 1 / 1 |
| USB2_SSTX | 0.834 | 0.177 | 0.150 | 2 / 2 |
| USB2_SSRX | 0.423 | 0.261 | 0.150 | 2 / 2 |
| USB2_D | 3.061 | 6.586 | 1.100 | 2 / 2 |
| USB1_D | 1.162 | 1.164 | 1.100 | 2 / 2 |
| USB_DEBUG_A | 4.829 | 14.095 | 1.100 | 0 / 0 |
| USB_DEBUG_B | 6.786 | 14.095 | 1.100 | 0 / 0 |

The two previously reviewed unbranched OE_MDI paths reproduce the original results. Other interfaces illustrate why endpoint measurement is necessary: USB2_SSTX's net-total mismatch looks like 0.177 mm while its actual copper endpoint mismatch is 0.834 mm. Its off-path copper is approximately 29.02/27.92 mm per conductor. USB2_D has approximately 18.45/15.28 mm off-path copper. These are substantial branch/topology findings, not quantities that should be added to the main signal path to make its lengths match.

## Reference continuity and coupling

Outer-layer reference mapping is explicit: F.Cu -> In1.Cu; a four-layer board maps B.Cu -> In2.Cu, while the proposed six-layer board maps B.Cu -> In4.Cu. For the explicit six-layer sequence F/In1/In2/In3/In4/B, In3.Cu is screened against In4.Cu GND. Other internal signal assignments remain unsupported. This projected-ground test does not qualify the field distribution between In2 power and In4 ground; the material stackup and impedance/delay extraction remain required. The audit uses saved filled GND polygons, including holes, and checks the projected trace width. It separately records crossings over foreign tracks in the reference layer. Refill zones from the same PCB revision before treating results as current.

The baseline OE_MDI1 conductors cross an In2.Cu +3V3 track near X=31.1-31.6, Y=32.3-33.7 mm. The raw report identifies both signal UUIDs and the offending reference-track UUID `2211e0d3-dab0-46bd-8e8c-d9e6109b41a3`. PCIe RX_N also crosses In2 +3V3 near X=35.2-35.9, Y=49.8-50.0 mm. The six-layer proposal provides a dedicated bottom reference layer, but its actual filled geometry and connections must be rechecked; adding a layer name alone is insufficient.

Signal-via antipads and connector transitions naturally produce some reference voids. They are reported rather than suppressed by a global radius waiver. A reviewer must distinguish intended transitions from long return-path interruptions and document a nearby connected return via. The audit's 1.0 mm return-via proximity target is an explicit engineering screening target, not a claimed manufacturer limit.

Coupling is sampled at no more than 0.25 mm intervals against same-layer parallel partner segments (15-degree angle tolerance). A provisional 0.30 mm edge-gap screening target exposes long separated routes; it is not an impedance-derived acceptable spacing. All sampled unpaired spans, endpoints and UUIDs remain in the report for escape/bend review. Symmetric layer sequences and via counts are checked separately. No sampled geometry result establishes actual coupled impedance, insertion loss, eye opening or common-mode conversion.

## Correction priorities and useful locations

1. Route ESD protection as part of a short continuous port path. The present USB SuperSpeed ESD branches reach approximately X=93, Y=45-47 while the USB-A interface is around Y=100. Review placement near the external connector and route through its pads, preserving protection and all required signals. Remove obsolete branch copper only after the replacement path is verified.
2. Re-route OE_MDI3 as a pair; its negative conductor has a long detour while positive contains a 5.458 mm vertical run around X=25.849, Y=28.407-33.865. Adding 10.76 mm to the shorter trace without correcting symmetry would retain the core defect.
3. OE_MDI1's positive run is approximately X=31.596, Y=25.962-34.292 (8.330 mm). The negative path's connector escape detours. Prefer symmetric escape geometry before local tuning; any tuning in this area must honor adjacent pairs and the reference-plane problem.
4. Re-check PCIe matching over the complete device-to-module path, including each side of AC capacitors. Keep capacitor geometry and via transitions symmetric. USB2/3 pairing and branch cleanup must precede final length tuning.
5. Perform object-specific reference and escape reviews after zone refill, then extract delay and impedance using the agreed material/copper stackup. RGMII and SDIO timing groups are outside this differential-only helper and remain separate release gates.

The suggested coordinates are diagnostic locations in the baseline, not DRC-cleared routing proposals. PCB placement changes invalidate their use as tuning sites.

## Commands

In the pinned dedicated Codex image, from `hardware/`:

```sh
/usr/bin/python3 -m unittest discover -s tests -p test_high_speed_audit.py -v
/usr/bin/python3 tools/high_speed_audit.py hw/sorx.kicad_pcb --output validation/high-speed/current-path-audit.json
```

Image: `sha256:23dbab2af8391fba7496a546d6729c37a5e8bdfb8e941ab903589252e8bec1e2`. The CLI atomically replaces the requested report, records source/tool hashes, detects a PCB change during execution and returns 1 for unresolved prerequisites. Preserve the baseline report and generate a distinct current report for comparison.

## Additional audit regressions

Eight targeted tests now cover branch exclusion, explicit series devices, layer isolation, duplicate copper, reference interruptions, six-layer In3/In4 reference projection, NumPy report scalars, and physical via-annulus joins. The last case was reproduced as a false open before correction. Tracks ending inside a same-net plated land now connect through explicit `via_copper` edges. Their radial pad-center lengths are a geometric bookkeeping estimate, not a model of current flowing through the drill center or a field-solved delay. The raw report separates this estimate from track and pad lengths; barrel/stub delay remains unqualified.

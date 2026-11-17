# Main power network review

**NOT READY FOR FABRICATION.** This review quantifies conductor loss and implements a feasible copper improvement. It does not establish regulator tolerance, connector contact drop, ground return drop, startup behavior or thermal compliance.

`tools/power_network.py` builds a resistor graph from every track and via on each reviewed net and solves nodal voltages and branch currents. Pad copper is treated as equipotential; coincident centerline segments are not counted as separate parallel wires. The five SoM supply pins are evaluated both as an ideal common module node and with the entire load assigned to each pin in turn. No equal division between pins or vias is assumed. Series/package and connector contact resistances, pad spreading and GND-plane resistance are excluded. Broad adjacent copper contact without intersecting centerlines is not a field-solved connection; the extracted graph is an engineering diagnostic, not an exact continuum model.

The material scenario uses 25 µm finished copper, 20 µm barrel plating, 85 °C copper, 20 °C resistivity of 1.724e-8 ohm-m and coefficient 0.00393/°C. These are explicit design assumptions to be guaranteed or replaced by the fabricator; 85 °C is not a predicted trace temperature. The six-layer conductor-center distances follow the proposed 1.6 mm stack. Conservative material choices do not make the result an upper bound on total rail drop because omitted contacts and return copper add resistance.

The planning load is 2.59 A on +5V (12.5 W SoM plus 0.45 W external 1.8 V allowance), and 1.893 A input at 15 V from the converter-efficiency budget. They are allocation cases, not measured loads or manufacturer maxima.

| Trace/barrel network on checkpoint 13c | Drop at planning transient | Copper loss |
|---|---:|---:|
| J3 VBUS contacts to Q1 source group | 64.37 mV | 0.122 W |
| Q1 drain group to eFuse IN group | 8.52 mV | 0.016 W |
| L1 output to ideal common J1 supply group | 234.69 mV | 0.608 W |
| L1 output, worst single-pin allocation J1.251 | 245.60 mV | 0.636 W |

The +5V result is driven by shared sections, not an assumed single shortest path. Sixteen trace sections carry essentially the entire 2.59 A. The 10.1791 mm segment `2ac454e6-db81-47d0-8122-74d9130ebdd9`, from (26.8104, 41.8237) to (26.8104, 52.0028), is only 0.35 mm wide and alone contributes 65.21 mV in this scenario. The full object/current list is retained in `validation/power-network-01/network.json`.

`tools/widen_main_power.py` widens actual current-carrying segments up to the nearest foreign copper, holes and board edge, using explicit clearance margins. Its isolated proof widened 21 segments without adding a native DRC finding. This reduced the common-node +5V estimate to 200.99 mV, which still warranted a larger repair. VBUS input became 62.57 mV and protected input 3.71 mV. The narrow sections constrained by nearby copper are retained in the report rather than hidden.

`tools/route_main_bypass.py` adds a complete 1.5 mm wide front-layer parallel +5V path from L1.2 to J1.259. It forbids every automatic layer change and introduces no new vias. The first isolated proof is 52.862 mm long and passes the same native DRC comparison with zero new findings. Even assigning the entire 2.59 A to this new path gives a centerline conductor estimate of 30.51 milliohm, 79.02 mV and 0.205 W under the stated material scenario. Existing parallel copper reduces conductor resistance; it is not used to justify equal current division. The full-width path estimate remains distinct from contact, pad-spreading and return-path contributions. The material and temperature assumptions still need supplier/physical qualification.

The initial narrow-network finding is reproduced before repair. Targeted solver tests cover a shared resistor feeding unequal parallel branches, separate loads sharing an upstream section, and an open required load. Both widening and bypass helpers reject new native findings or increased required opens. The scratch proof is not the final integrated board; its geometry is replayed and checked on the coordinated candidate before adoption.

Commands in the pinned tool image, with `KICAD7_FOOTPRINT_DIR=/usr/share/kicad/footprints`:

```text
python3 -m unittest discover -s tests -p test_power_network.py
python3 tools/power_network.py INPUT.kicad_pcb NEW_NETWORK.json
python3 tools/widen_main_power.py INPUT.kicad_pcb NEW_NETWORK.json NEW_WIDENED.kicad_pcb NEW_WIDEN_EVIDENCE
python3 tools/route_main_bypass.py NEW_WIDENED.kicad_pcb NEW_BYPASS.kicad_pcb NEW_BYPASS_EVIDENCE
```

All outputs must be new paths. Input hash mismatches reject stale network evidence. Do not interpret ordinary clearance DRC or these DC calculations as an IPC-2152 thermal pass, a transient simulation, or an approved fabrication release.

## Accepted common-board integration

The coordinated source was `runs/route-combined-15b-ground.kicad_pcb`, SHA-256 `0e825f295aac2fe7da0723bf85a7b8d2c6de2a09d00255e1a4cd65d6a5e986c6`. Network analysis, widening, the full-width bypass, PD decoupling and exact service headers all passed sequential native before/after checks. Their accepted output is `runs/route-electrical-16d-headers.kicad_pcb`, SHA-256 `4a9d59f1d3292ec9f2004aeffd04bc6b1c7f66bcfa97734081f5ddb669ad555f`. Required opens changed 36 to 35. Before custom-library normalization, other findings changed 107 to 106; the header comparison resolves the six inherited missing-custom-library warnings consistently on both sides and reports 100 to 100. No new finding was accepted at any stage.

The exact SMA correction was then replayed to `runs/electrical-final17/route-electrical-17-sma.kicad_pcb`, SHA-256 `825e00ab492409f0c4665e1710710c23160ca38cbcf83e9748ebc51d6f933f0d`. With the same resolved library environment, it reduced other findings 100 to 95 and retained 35 required opens. Evidence is in `validation/electrical-integration-16/` and `validation/electrical-integration-17-sma/`. An independent comparison against the coordinated source found zero changed pad nets, zero changed net identities on retained copper, and zero unexpected copper removals. Native model parity reports 371 footprints, 1,588 pads, zero model mismatches and zero duplicate via sites. These are checkpoint results, not fabrication approval or the outcome of later routing work.

The electrical commands use `/hardware` as the container mount for this clone's `examples/sorx_appliance/hardware` directory. Other project launchers use `/work/hardware` for the same independent files. Retained source paths and absolute `sorx` library-table entries reflect the command that produced each report. To reproduce them, use the recorded mount or the verifier's read-only `/hardware/hw` alias to the same independent clone. Neither path refers to Claude's checkout. A `SaveBoard` call can change the native board's project lookup path: the header verifier now runs DRC while its board still belongs to the isolated project with a resolved library table, then saves the output. Missing-library warnings were fixed through the environment, not waived.

On the accepted common board, the new full-width path is 52.134 mm. Its whole-load conductor estimate is **77.93 mV at 2.59 A**, with the same 25 µm / 85 °C assumptions; the corresponding loss is 0.202 W. The earlier 79.02 mV figure refers only to the isolated scratch proof.

The six complete scratch projects used for header native-DRC comparisons are preserved byte-for-byte in [electrical-native-projects.zip](../validation/electrical-native-projects.zip). Its [manifest](../validation/electrical-native-projects.json) records each original relative path, size and SHA-256. Raw reports remain beside their execution records. Extract these historical inputs into isolated scratch space, never over the current design.

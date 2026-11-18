# PCIe TX and reference-clock copper correction

**NOT READY FOR FABRICATION.** This accepted intermediate checkpoint closes the mounting-area TX/clock opens and improves paired topology. It does not qualify impedance, timing, the remaining interfaces, or the complete board.

## Fixed comparison

Input `runs/route-electrical-13c-trimmed.kicad_pcb`, SHA-256 `8b9d37de517287fd49d7c12f221765020cdd29ea738bb878ef77581833a4aefa`.
Accepted output `runs/route-electrical-14-pcie.kicad_pcb`, SHA-256 `be0e7b2efc6d55d5b02c0e0923cbbab927f18c36e189ecd4d92fb11a7ceba647`.
Raw before report: `validation/high-speed/pcie-01/`. Raw after, source-to-destination geometry, exact removed records, native comparison and hashes: `validation/high-speed/pcie-11/`.

| Measure | Before | Accepted result |
|---|---:|---:|
| TX/clock required native opens | 3 | 0 |
| Whole-board native opens | 54 | 50 |
| Other native findings | 113 | 107 |
| TX endpoint planar lengths P/N | N incomplete | 28.897652 / 28.930078 mm |
| TX planar mismatch | N incomplete | 0.032426 mm |
| Clock endpoint planar lengths P/N | N incomplete | 45.510127 / 45.558120 mm |
| Clock planar mismatch | N incomplete | 0.047993 mm |
| Off-path TX/clock track length | Broken routes | 0 for all four conductors |
| Through-via transitions | Broken routes | 2 per conductor |

No new non-open native finding is introduced and no net's open count increases. Three differential opens and one GND open disappear after the obsolete copper is removed and planes refilled. Native object-pair choices can change when islands merge; the complete raw reports and per-net counts are retained. Both sides use `KICAD7_FOOTPRINT_DIR=/usr/share/kicad/footprints`; the 199 environment-only footprint-library warnings are absent on both sides.

## Physical change and remaining reset work

The old TX and reference-clock copper is replaced, retaining the exact logical endpoints: TX J1.240/238 to U10.24/23, clock J1.228/226 to U10.26/25. The existing I210 bottom-layer escape prefixes are retained. Each conductor then uses one transition to In3, a paired shared-center corridor, and one transition to the module's front pads. This avoids the actual H7 mounting-hole obstruction.

The internal paired trunks are 0.155 mm wide with 0.400 mm center spacing (0.245 mm gap). The route search reserves the complete two-conductor envelope before creating the two rails. Short existing escape regions carry measured 45-degree matching detours: +0.20 mm on TX_P and +0.70 mm on clock_P. The shorter-path tuning changes neither logical polarity nor the two-transition topology. The strict gate verifies the actual endpoint graph, rather than net-total copper sums.

The existing PCIE_RST#_1V8 via near (48.8318,61.5146) obstructed the module fanout. Its four old tracks and two vias are removed, with exact serialized records retained. This net already had one required open between its only two pads, U12.2 and J1.244; it still has exactly that one required open. Downstream ordinary routing must restore it. No unrelated signal or power copper is removed. The native source file is never mutated.

## What remains unqualified

The 0.150 mm planar screening target passes. Matched planar length is not proof of delay matching: radial via-pad paths are explicitly geometric estimates, barrel and package delay are not extracted, and remaining uncoupled escapes and projected reference interruptions require review. The two internal traces face the proposed In4 ground at 0.250 mm and In2 power above; their 0.155 mm width is not a qualified PCIe impedance prescription. Fabricator/material-specific field calculation must establish width, spacing, loss and delay, followed by new geometry/reference checks if the prescription changes.

The separate PCIe RX path was not altered; its approximately 0.920 mm planar mismatch remains above the adopted screening target. OE MDI, other interface constraints and whole-board opens also remain release blockers. Neither these reports nor the KiCad clearance result constitute signal-integrity simulation or a hardware measurement.

## Reproduce and validate

Use the pinned Codex hardware image `sha256:23dbab2af8391fba7496a546d6729c37a5e8bdfb8e941ab903589252e8bec1e2`, KiCad 7.0.11, with the footprint-library environment above. From `hardware/`, choose fresh output/evidence paths:

```sh
/usr/bin/python3 tools/correct_pcie.py runs/route-electrical-13c-trimmed.kicad_pcb runs/new-pcie.kicad_pcb --evidence validation/high-speed/new-pcie
/usr/bin/python3 -m unittest discover -s tests -p test_pcie_candidate_checks.py -v
/usr/bin/python3 -m unittest discover -s tests -p test_high_speed_audit.py -v
```

The worker runs in a fresh process after exact serialized copper filtering, avoiding the observed KiCad7 Remove/SWIG defect. It refuses output overwrites and changed I210 escape anchors. Failed workers, missing endpoint pairs, graph errors, excess branch copper, false/asymmetric via flags, planar gate failures, malformed native reports and target errors produce failure. The pure gate requires finite nonnegative off-path numbers, literal true flags and a complete three-section native DRC report. Seven gate regressions pass; the existing USB gate's seven tests also pass in the pinned image. Gate-only hardening was rechecked against the retained raw geometry and native reports without claiming a new circuit-generation run.

Rejected exploratory pair corridors are never promoted. Trial09 first proved complete paired topology but missed planar matching (TX 0.232426 mm, clock 0.747993 mm); trial11 adds the two validated matching detours. The wider-spacing trial10 also missed matching and remains rejected. All source/output hashes identify actual runs, not manufactured readiness.

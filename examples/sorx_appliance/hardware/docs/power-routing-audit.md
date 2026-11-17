# Power-routing review - Codex B

**NOT READY FOR FABRICATION.** Native connectivity and clearance are necessary,
but do not establish rail-current capacity, ripple, loop inductance or temperature.

`tools/route_power.py` creates new candidates at explicit per-connection widths.
The first accepted pass is `runs/route-electrical-07-power.kicad_pcb`, starting
from `route-06-final-model.kicad_pcb`. Its native DRC changed 138 to 114 required
opens and 339 to 334 other findings, with zero new non-open findings. No existing
track/via was removed. Exact commands/outputs and added UUIDs are under
`validation/power-routing-01/`. These checkpoint counts are historical within
this independent implementation, not the final integrated board result.

A copper-shape island audit identified the remaining supply loads before a
second helper was allowed to use narrower branches. It includes actual pad,
track and via overlap on every copper layer, without treating an In2 endpoint
as though it were on B.Cu. The load sets are saved in `power-islands.json`.

| Remaining island | Actual loads | Routing allocation |
|---|---|---|
| VBUS_IN | Q2 source reference and R12 sense/discharge | 0.25 mm, 36 mA; excludes Q1/main input current |
| +5V | C3 and C4, each 4.7 µF local decoupling | 0.50 mm; transient loop qualification still required |
| +3V3 | U27 RS232, U32 watchdog, U33/U34 translators, R108 and four 100 nF capacitors | 0.25 mm, 100 mA branch allocation |
| +1V8 | Pullups and U12/C76 reset-buffer island | 0.15 mm, 5 mA per branch allocation |
| +4V5_PD | TP21 only | 0.15 mm, 1 mA measurement allocation; use a high-impedance probe |
| +1V2_PHY | U13 pin 4 only | 0.15 mm short pin escape, ≤5 mm; 300 mA planning envelope |
| VBUS_DBG_RAW | J10 fine-pitch VBUS contacts and debug UART/filter | 0.15 mm, 100 mA allocation |

These current allocations are design envelopes to qualify, not newly invented
manufacturer maximum-load specifications. All larger original rails remain
subject to the total-load/thermal review. The first pass's 0.25 mm auxiliary
regulator traces are limited to its 50 mA output envelope. Its main VBUS and 5 V
attempts used 2.0 mm and 1.5 mm respectively and refused unreviewed transitions.

`tools/route_power_branches.py` requires the audited power-copper inventory to
be unchanged before running. It adds an In2 transition only at a sampled point
on the actual DRC-reported existing track, after checking foreign copper,
drills, edge clearance and via keepouts. It records the original track UUID,
new via coordinates, diameter and drill. It never silently relabels an In2
endpoint. The local decoupler transition uses 0.8/0.4 mm via copper/drill; smaller
control branches use 0.6/0.3 mm. Native DRC must still accept the entire result.
No via-in-pad or blanket clearance exemption is introduced.

For transparent DC estimates, copper resistance may be calculated as
`R_ohm = 1.724e-5 * length_mm / (width_mm * copper_thickness_mm)` at 20 °C.
With assumed 35 µm copper, a 10 mm long, 0.15 mm wide trace is about 32.8 milliohm, or 3.28 mV
at 100 mA. A 1.6 mm long, 0.3 mm finished-hole via with assumed 25 µm plating is
about 1.17 milliohm, neglecting pad/spreading resistance. Both thicknesses are
fabrication assumptions, not verified delivered values. Use temperature-adjusted
resistivity, finished plating minimum and all shared segments for the final
IR-drop calculation. This arithmetic is not an IPC 2152 thermal qualification.

Main-current transitions need a separately reviewed via array. Do not assign
full 3 A converter/input current to one control-branch via because DRC passes.
Parallel barrels need comparable connection impedance; two vias do not imply
exactly half the current in each. Final fabrication stackup, minimum plating,
maximum ambient, pulse duty cycle and measured/simulated rail drops must close
this requirement. Converter input/SW/output loops and decoupling inductance
also need review beyond DC resistance.

A source-to-destination check of all four J3 VBUS pads to Q1 source and L1 to
J1 supply pads found no newly added auxiliary copper on those main paths.
The original paths include long 0.35 mm segments. Their existence alone does not
prove the complete parallel power network is inadequate; it also does not
establish a current/thermal pass. Preserve this distinction in the release table.

Commands, from `hardware/` in the pinned tool image:

```text
python3 tools/route_power.py runs/route-06-final-model.kicad_pcb runs/route-electrical-07-power.kicad_pcb validation/power-routing-01
python3 tools/route_power_branches.py INPUT.kicad_pcb NEW_OUTPUT.kicad_pcb NEW_EVIDENCE_DIRECTORY
```

Use new output/evidence paths for each attempt. Rejected candidates must never
be installed as the active board merely because they close more opens.


## Placement correction and the third power pass

The actual drill audit in `tools/audit_npth.py` checks every copper pad on both
faces against every round or slotted NPTH drill. On checkpoint 09 it inspected
27 drills and 1,503 copper pads at a 0.25 mm review threshold. It found one
foreign-component conflict: H7's 3.0 mm hole physically overlapped backside
U12 pin 5 by 0.3923 mm2. The failed route was correctly blocked by that hole.
No router-clearance exception was introduced. Own-footprint annular mounting
lands are recorded separately for supplier review, not silently ignored.

The accepted local placements are U12 at (30, 55.5), 0 degrees on B.Cu; C76
at (30.75, 58.5), 90 degrees on B.Cu; and TP21 at (17.5, 28), 0 degrees on
F.Cu. TP21's original pad overlapped a PD_ATTACH# test-point pad. The first
replacement candidate was rejected for a courtyard overlap; the final position
clears native DRC. After relocation the full-board pad/NPTH audit has zero
foreign-component findings. Native geometry regression tests cover backside
overlap, a non-overlapping clearance violation, and a clear relocated pad.

The USB topology correction removed three identified +1V8 segments. Before
repair, the island audit was repeated on checkpoint 11b. The +5V and +3V3
rails remained connected. The disconnected +1V8 loads were R82; pullups
R3/R4/R107 with C132; PD pullups R13/R14/R23; and U12/C76. Thus the 5 mA,
0.15 mm branch envelope still applies; no narrow main-current trunk was
introduced. The new U12-to-rail branch is 2.618 mm and the C76-to-rail branch is 0.924 mm;
these are not a demonstrated complete decoupling loop. Local loop inductance
and ground return still require review. Static reset routes
use 0.15 mm copper; these are reset logic, not PCIe differential signals.

Checkpoint 12 reduced 62 opens to 56 without adding a non-open finding relative
to its immediate input. However, the overall relocation was not accepted until
the dangling reset stub introduced by detachment was removed. The final trim
removed 14 exact copper objects: pad-free islands and dangling reset dead ends.
An independent copper-shape audit proved all actual pad connectivity groups
unchanged by that trim. Required reset connections remain explicit opens.

The accepted combined result is `runs/route-electrical-13c-trimmed.kicad_pcb`,
SHA-256 `8b9d37de517287fd49d7c12f221765020cdd29ea738bb878ef77581833a4aefa`.
Compared with the accepted USB checkpoint 10, native opens changed 61 to 54
and other findings 321 to 306, with zero new non-open findings. These counts
include inherited unresolved library warnings; they are not fabrication approval.
The source hashes, exact command lines, auxiliary script snapshots, rejected
intermediate explanations and accepted comparison are retained in
`validation/electrical-placement-trim-03/execution-manifest.json`.

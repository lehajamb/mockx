# Ground and signal routing workflow

**NOT READY FOR FABRICATION.** These tools create routing candidates. A completed
routing invocation is not release approval, and stored copper fills do not prove
ground connectivity.

## Ground stitching

`tools/ground_stitch.py INPUT.kicad_pcb OUTPUT.kicad_pcb --report-dir NEW_DIRECTORY`
works on full connected GND copper geometry, including filled zones. It places
0.6 mm / 0.3 mm through vias outside pads, preserves existing routes, refills
zones, and retains native KiCad before/after DRC.

`--trace-escapes` can add GND stems from pads to safe existing or new vias while
preserving all other routing. It applies physical drill exclusions and native
DRC just like direct stitching.

Optional `--detour-supply` can move unlocked In2 power segments around a new
ground via. The same power-net endpoints and track width are retained. It never
shrinks the via process, removes functionality, or reroutes differential pairs.
Proposed detours undergo geometric collision checks before insertion. Every
candidate still requires native DRC and electrical review; these heuristics are
not a power-integrity qualification.

The report records exact additions, supply detours, unresolved pads, source PCB,
project and tool hashes, tool versions and input-change detection. The caller
must set `SORX_IMAGE_ID` to the actual immutable container image identity.
An existing destination or report directory is refused. Exit 0 means this
ground task completed; 1 means ground groups remain; 2 means new non-connectivity
DRC findings or source changes invalidate the candidate. Unhandled tool errors
remain nonzero. Every report explicitly sets fabrication approval false.

The starting `runs/route-00-six-layer.kicad_pcb` had already had one duplicate
via removed by the six-layer preparation step. Its native DRC had 51 open items
and 254 other findings. The conservative island-only experiment
[`ground-stitch-01`](../validation/ground-stitch-01/ground-stitch.json) closed
three opens without new other findings. The supply-detour experiments
[`ground-stitch-02`](../validation/ground-stitch-02/ground-stitch.json) and
[`ground-stitch-05`](../validation/ground-stitch-05/ground-stitch.json) each
closed five opens, leaving 46, with 254 other findings unchanged and no new
non-connectivity fingerprints. They are alternative experimental checkpoints,
not cumulative changes. C123's apparent free surface via space was obstructed
by buried +1V8 copper; an explicit power detour was necessary.

Experiment 03 was stopped after a rasterizer defect was identified; experiment
04 failed on a clipped geometry tangency. Neither produced a validated final
candidate. The retained failing and passing regression reports document both
corrections. Experiment 05 predates the final strict-first candidate selection
and final provenance fields; rerun the current tool on the final synchronized
source for release evidence. Many socket ground pins still require deliberate
fanout/placement work. No passing-ground claim is made.

## Generic signal completion

`tools/route_completion.py INPUT OUTPUT [ROUNDS]` uses F.Cu, B.Cu and In3.Cu for
generic 0.15 mm signal routing. `PATCH_NETS` is an optional comma-separated set
of exact permitted net names. Power and DP90/DP100 nets are refused even when
selected. Power needs per-net current/width analysis, and differential links
need interface-specific paired routing. MG_TX/MG_RX and SD clock/command/data
timing nets are also protected, although the historical project classified them
as Default. Physical drill exclusions apply independently of net ownership;
existing plated transitions are reused without adding duplicate drill hits.

Rip-up is disabled by default. `PATCH_ALLOW_RIP=1` permits only eligible signal
rip-up; power, differential, locked and protected tracks remain excluded.
In2 endpoints are reported as unsupported, never silently interpreted as B.Cu.
Remaining non-GND opens produce exit 1 from the top-level routing invocation.
The routing result must be checked with fresh native DRC and the complete
[verification workflow](verification-workflow.md). A successful signal-only
pass does not imply that GND or the release gates pass.

## Regression checks

Run the dedicated image's system Python (with KiCad, NumPy, Pillow and Shapely):

```sh
python3 -m unittest discover -s tests -p test_ground_stitch.py -v
python3 -m unittest discover -s tests -p test_route_completion.py -v
python3 -m unittest discover -s tests -p test_orphan_copper.py -v
```

The focused suite has seventeen passing cases. Missing dedicated dependencies cause
an explicit module skip in the host application suite; the mandatory container
run executes every case. Internal import errors and assertions are not skipped.
The verification-core regression suite runs independently.

## Controlled socket-ground fanout evidence

`tools/ground_fanout.py` creates one explicit front-ground escape and reroutes
only ordinary 0.15 mm signal segments blocking its stem or via. Endpoints and
widths remain fixed. It refuses protected nets, locked traces and all existing
vias, reconnects every removed segment, and rejects new native DRC findings or
remaining opens on the affected signals. Source changes invalidate acceptance.
This candidate-level acceptance is not fabrication approval.

On the earlier synchronized electrical model, signal routing closed 130 to 94
native opens without new non-connectivity findings. A controlled J1.194 escape
rerouted PPS_GPIO and FRAM_MISO, closed four ground connections (94 to 90), and
left both signals connected. Eight direct ground stitches then reduced 90 to
82. Other DRC findings stayed at 326 through the two ground stages. Reports are
in `validation/placement-signal-02/`, `placement-ground-fanout-02/` and
`placement-ground-direct-01/`. The source model subsequently changed; these are
historical method-validation results, not final-model release evidence.

An earlier signal trial introduced NPTH-clearance and duplicate-drill errors;
it was rejected. Native-board regression tests reproduced both defects before
the obstacle model and existing-via reuse were corrected. The rejected trial
remains in `validation/placement-signal-01/` for comparison. A failed J1.10
fanout similarly stopped because a newly added I2C via occupied its planned
site; no replacement board was saved. Refresh geometry surveys after routing.

## Padless ordinary-signal copper

`tools/audit_orphan_copper.py INPUT --report-dir NEW_DIRECTORY` is read-only.
It groups actual same-net copper across layers and plated transitions and
reports islands containing no component pad. An explicit `--candidate NEW_PCB`
can remove only these unused ordinary-signal islands. Locked items, power,
GND, differential, timing and zoned nets are excluded. Before/after pad
connectivity groups must match exactly, and native DRC must introduce no new
non-connectivity finding. Every removed UUID is retained in the report.

A reduction in open-item count from this cleanup is reported as unused copper
removed, not a newly connected required circuit. Three native-board regression
cases check protected/locked exclusions, preservation of pad-connected back
copper through a via, and unchanged pad connectivity after removal.

## Named supplies and local analog loops

The generic router protects `VBAT_RTC`, `RS_VP` and `RS_VN` as power nets.
The TRS3232 flying-capacitor loops (`RS_C1P/N`, `RS_C2P/N`), bootstrap
loops (`*_BOOT`) and `OE_REG_CTRL` are also excluded from ordinary routing
and rip-up. These require review of local current loops and placement, not
a default signal width. The battery and charge-pump classification regression
failed for all three supplies before the fix; native routing tests then pass.
This updates the declarative project policy; final active project settings
must be reconciled from `proj.PATTERNS` without changing historical evidence.

`PD_1V2` and `PD_2V7` are also power-classified local STUSB4500 regulator
decoupling connections (C14/C15). They must remain short local loops and
are excluded from the generic signal router; the ordinary-net classifier
previously admitted them. Their placement and thermal-via interaction require
a coordinated electrical layout correction.

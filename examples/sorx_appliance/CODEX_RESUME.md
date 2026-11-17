# Resume the independent Codex hardware implementation

**PAUSED AT THE USER'S REQUEST — NOT READY FOR FABRICATION.**

This is a saved development checkpoint, not a completed fabrication candidate.
Do not start further work until a new task requests resumption. No changes were
discarded, no boards/components ordered, and no branch pushed or merged.

## Start here

- Clone: `C:/Users/lehaj/dev/mockx_rebirth_july_2026/mockx-sorx-codex`.
- Branch: `codex/sorx-hardware-independent`.
- Starting bootstrap: `c2e1b3c4b3b6b2cf1099db273bd4c1c08a7c7a8b`.
- Source-import benchmark: `df8247829ee12cb5b1c03e0d8fa1c29383a95f74`.
- Native PCB promotion commit: `ad3d633`.
- The final pause commit also contains unfinished tools, experiments and this
  handoff. Its commit message explicitly identifies it as a WIP checkpoint.
- Keep the original Claude checkout and containers untouched. This clone has
  separate Git objects and no object alternates/hardlinks; its GitHub remote
  is preserved. Do not push or merge without a later instruction.

Read repository `AGENTS.md`, this example's `CLAUDE.md`,
[release criteria](docs/release-criteria.md), the
[independent implementation record](docs/independent-implementation.md), and:

- [Electrical resume](hardware/docs/electrical-resume.md).
- [Routing and verification resume](hardware/docs/verification-routing-resume.md).
- [High-speed resume](hardware/docs/high-speed-resume.md).

The original handoff and original “final” filenames remain historical evidence.
Preserve `baseline/` and `reference_images/` unchanged.

## Authoritative active board

`hardware/hw/sorx.kicad_pcb` now contains the accepted independent implementation.
It is byte-identical to
`hardware/runs/electrical-final17/power-bridge-proof01.kicad_pcb`.

SHA-256: `5188991db0633f32b6552605679a701606510d5833916cdc843b0ec5413cead1`.

Fresh native checks are in
[validation/native-promotion-01](hardware/validation/native-promotion-01/).
Both saved and freshly filled DRC report the same counts:

| Measure | Imported baseline | Current accepted board |
|---|---:|---:|
| Footprints |348|371|
| Pads |1,500|1,588|
| Modeled nets |262|269|
| Track segments |3,317|9,362|
| Vias |1,180|1,258|
| Required open DRC items |51|30|
| Other DRC findings with library environment normalized |56|93|
| PCB/model mismatches |0|0|
| Duplicate via sites |1|0|

The historical baseline 255 other findings included 199 environment-only library
warnings. Their removal is not a copper improvement. The increased current
finding count is retained honestly; this is not a percentage-complete estimate.
There are no blanket DRC waivers.

Current opens include 16 GND items; two each on PD_ATTACH# and MG_LED2; and one
each on USB1_OC#, USB1_VBUS_DET, FRAM_MOSI, MG_TXD0, PD_ALERT#,
PCIE_RST#_1V8, PD_EN_SNK#, +1V2_PHY, PCIE_RST#_3V3 and OE_JTAG_PU.
[Exact objects and fingerprints](hardware/validation/pause-checkpoint/outstanding-findings.json)
are retained. The 93 other findings include 27 dangling tracks,3 dangling vias,
40 silkscreen findings,12 hole-clearance findings,5 courtyard overlaps,
4 solder-mask bridges and2 copper-edge findings. These require object-level
review; counts alone cannot justify deletion or waivers.

## Completed engineering

- Reproduced baseline ERC, DRC, connectivity, model/parity and mechanical results
  in a pinned isolated environment.
- Made required verification/export subprocess failures propagate; bound
  evidence to input hashes; added failure tests and mechanical error handling.
- Corrected the FRAM package/pin model, several supply/protection/recovery
  circuits, KSZ bias resistor, current sensing, exact service headers/shunts,
  and corresponding schematic/BOM/assembly requirements.
- Adopted a provisional six-layer PCB and corrected native footprint/placement
  geometry, retention holes, copper routing, ground connections, USB3 paths,
  PCIe TX/clock, main-power conduction and PD decoupling.
- Reproduced the saved J9 interference and traced it to a CAD aliasing bug;
  corrected the checker instead of moving a connector to hide the result.
- Bound supplier socket/heatsink/screw geometry to exact hashes and corrected
  placement. Fresh integrated17 CAD geometry passes, while thermal, tolerance
  and assembly access qualifications remain open.
- Corrected the PPS SMA lands against manufacturer dimensions. The current
  1.60mm stack conflicts with the connector's1.57mm maximum PCB specification;
  this is explicitly unresolved.

Relevant milestone commits include `4e3d306` (verification), `954042f`
(mechanics), `8e8c82d` (electrical model), `b06be0f` (supplier geometry),
`f3069c6` (service headers), `d73b707` (PCIe TX/clock), `9fe9c7a` (SMA),
`1737485` (ground integration), `fcf3a76` (power/decoupling),
`4c5aea7` (debug power/supply islands), and `ad3d633` (native promotion).

## Successful isolated proofs that are NOT in the active PCB

1. PCIe RX: `runs/rx-trial-01.kicad_pcb`, SHA
   `4e08c2cedee7f731aa5e918245257377e85e3336fe90106e1885ad83e14501d8`.
   Skew0.919690→0.019690mm; its older35-open/95-other source has no new findings.
   Reapply the exact small change to the active board and rerun strict checks.
2. Label cleanup: `runs/labels-proof-03/output.kicad_pcb`, SHA
   `346ca7b4832edc0b5f6c79fc0e9524057c36845d2594c4bfd4e4d024f93426e8`.
   Eight local label moves and “REV B - WIP” title;95→74 other findings on its
   older35-open source, no new findings. USB2/REC remains unresolved. The
   earlier equivalent label-only proof has exact copper/pad identity evidence.
   Reapply the helper and rerun identity/native/visual checks; do not overwrite
   the current PCB with this older candidate.

Ordinary-signal proof21, C64 ground repair, RTC/inward fanouts, RGMII01–09 and
MDI01–02 are rejected or incomplete, not accepted work. The resume notes record
why. Never promote them based on total-open count alone. Some helpers have
passing fixture tests without any accepted native trial; their status is WIP.

## Next engineering work, once resumed

1. Inspect the current files and hashes. Keep the accepted board as the starting
   point; use fresh uniquely named scratch outputs and per-net regression gates.
2. Finish ordinary/GND connections without opening another net. The ordinary
   proof introduced a C64.2 ground problem; its proposed pad via conflicts with
   foreign-layer copper. Required ground pins must remain connected explicitly.
3. Resolve U13.4 +1V2_PHY and the absent MG_TXD0 route with coordinated local
   fanout changes. The recorded TXD0 hypothesis is only a survey, with 0.160mm
   trace and0.185mm via-clearance assumptions. Restore every displaced LED or
   supply connection and validate timing, topology and reference continuity.
4. Reapply accepted PCIe RX, complete MDI/RGMII review and correct the project
   class definitions. The current project still has original PCIe DP90 defaults.
   Intel's85ohm I210 recommendation and older Toradex90ohm guidance must be
   reconciled; REFCLK remains a separate review. Proposed class names in the
   high-speed resume were NOT implemented. No impedance is qualified yet.
5. Complete remaining electrical design work: negative PD_VS protection,
   positive clamp dynamic/SOA analysis, source/cable startup bounds, FRAM
   guaranteed rail range, ADC backfeed, and programming assets/procedures.
6. Run accepted-only dangling copper cleanup and label cleanup. Keep source
   identity and required-connectivity proofs; do not weaken checks.
7. Freeze sources, regenerate a fresh netlist and run full verification, then
   regenerate mechanics from that exact PCB. Finish object-specific connector,
   thermal, height, tolerance, mounting, access and sourcing review.
8. Add the existing filled/capped-via exporter to final packaging; it is NOT yet
   integrated into `export.py`. Include specific thermal/ground process needs.
9. Update source-bound R01–R09 gate records. Generate coherent development
   exports with a NOT READY status if gates still fail; fabrication approval
   requires all relevant evidence. Final manufacturing/mechanical export from
   the active checkpoint has NOT been done. Existing Gerbers are historical.

`validation/release-gates.json` remains OPEN; no gate was newly marked passed
at this pause. `validation/latest.json` points to an explicitly stale historical
full-verifier smoke run. Its expected failures do not describe the corrected
current board. The current native DRC/model evidence is separate and fresh.
Docker/QEMU application integration and physical hardware bring-up remain deferred.

## Tools and validation at the pause

Dedicated immutable image:
`sha256:23dbab2af8391fba7496a546d6729c37a5e8bdfb8e941ab903589252e8bec1e2`.
Tag `sorx-hw-codex:baseline-1`. KiCad7.0.11, KiAuto2.3.4, Python3.11.2,
CadQuery2.4 (`/opt/cq/bin/python`), Shapely2.0.6, Freerouting1.9.
Read [environment instructions](hardware/tools/env/README.md).
The launcher's `/hardware/hw` alias is read-only and points only to this clone.
Do not assume host KiCad/CAD imports or the historical `/home/claude` paths.

From the clone root, a representative native check is:

```powershell
& ./examples/sorx_appliance/hardware/tools/env/run.ps1 -ToolArgs @(
  'python3', '/work/hardware/tools/verification_worker.py', 'board',
  '/work/hardware/hw/sorx.kicad_pcb', '/work/hardware/runs/new-board-check.json',
  '--drc', '/work/hardware/runs/new-drc.txt'
)
```

Pause-time results:

- MockX full suite: **1,123 passed,16 skipped**, exit0. Raw log:
  `hardware/validation/mockx-regression-pause.log`.
- Pinned KiCad Python hardware suite:137 tests run,135 passed,2 CAD-module
  skips, exit0 (`hardware-unittest-pause.log`).
- Required CadQuery modules separately:9 mechanical and 6 vendor-geometry tests
  passed. These are regression tests, not physical qualification.
- 92 tool/test Python files parse successfully.
- Bootstrap provenance passes: 3 source files, 161 imported files,
  18 documented exclusions and 16 reference images.
- Native model/parity and duplicate-via checks pass; required-open and other DRC
  gates fail as documented. No final full-verifier success is claimed.

The Conda environment was not modified. To rerun the host suite, initialize
`C:/Users/lehaj/anaconda3/shell/condabin/conda-hook.ps1`, activate `mockx`, set
`PYTHONPATH` to this clone's `src`, and run `python -m pytest -q`.

## Saved evidence and isolation observations

All original local scratch files remain in `hardware/runs/` (Git-ignored).
[The inventory](hardware/validation/pause-checkpoint/local-scratch-inventory.json)
records their paths, sizes and hashes. They are local to this clone and are not
all carried by Git. Selected unpromoted candidates and large validation source
files are additionally preserved in
[the portable archive](hardware/validation/pause-checkpoint/native-experiments.zip)
with a [member/hash manifest](hardware/validation/pause-checkpoint/native-experiments.json).
Original loose files are retained; do not extract over the active project.
The earlier electrical project archive is preserved separately.

The final read-only sample of the original Claude checkout matches 25 of 26
initial file hashes. Its `hardware/tools/design.py` now differs from the initial
snapshot; author/cause is not established by this observation. No original file
was changed or restored by this implementation. Preserve it as found. Evidence:
[original-checkout observation](hardware/validation/pause-checkpoint/original-checkout-preservation.json).

All agents stopped engineering at the user's request. Dedicated test containers
are ephemeral; the final inventory is saved with the pause evidence. No Claude
container was stopped or changed.

## Prompt for the next Codex task

> Resume the independent SORX hardware implementation in
> `C:/Users/lehaj/dev/mockx_rebirth_july_2026/mockx-sorx-codex`, branch
> `codex/sorx-hardware-independent`. Read
> `examples/sorx_appliance/CODEX_RESUME.md` and its linked agent resume notes
> first. Continue from the accepted native PCB, preserve all baseline evidence
> and rejected trials, and keep Claude's checkout untouched. Hardware completion
> remains the priority. Do not push, merge, order hardware, weaken verification
> or call the design fabrication-ready while release gates remain open.

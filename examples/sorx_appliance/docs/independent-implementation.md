# Independent Codex engineering record

**PAUSED AT THE USER'S REQUEST — NOT READY FOR FABRICATION.**

The accepted native PCB is committed at `ad3d633` with 30 required opens,93
other DRC findings, no model mismatches and no duplicate via sites.
Read [the resume handoff](../CODEX_RESUME.md) before continuing.

## Comparison boundaries

- Standalone clone: `C:/Users/lehaj/dev/mockx_rebirth_july_2026/mockx-sorx-codex`.
- Branch: `codex/sorx-hardware-independent`.
- Starting bootstrap revision: `c2e1b3c4b3b6b2cf1099db273bd4c1c08a7c7a8b`.
- Immutable source import: `df8247829ee12cb5b1c03e0d8fa1c29383a95f74`.
- No push, merge, component purchase or board order is part of this work.
- This implementation is informed by prior review. It is not a blind comparison.
- [Isolation manifest](independent-workspace.json) records the initial clone,
  image identity and the observed original checkout file hashes.

The original archives and reference images are the comparison evidence. The
active PCB is a design source; coherent manufacturing exports are generated
from it. Historical routing checkpoints and initial placement/autorouting
scripts are not substitutes for the corrected board.

## Independently reproduced starting condition

[Raw baseline run](../hardware/validation/baseline-codex/run.json) records
commands, exit statuses, tools and hashes. Stored and freshly filled DRC both
reported 51 unconnected items and 255 other findings. ERC had zero errors and
warnings, and all 262 modeled nets matched the fresh schematic netlist.
A later same-source rerun with `KICAD7_FOOTPRINT_DIR` set resolves all 199
headless library warnings: the comparable baseline is **51 opens and 56
other findings**. The historical 255 total remains preserved; resolving the
tool environment is not a copper correction. See
[normalized baseline](../hardware/validation/baseline-library-corrected/result.json).

The board contained 348 footprints, 1,500 pads, 3,317 track segments and 1,180
vias. A fresh original mechanical run reproduced the reported 5.002 mm³ J9
interference.

That mechanical number has since been traced to a CAD object-aliasing defect:
an unused `add` loop mutated J9's object with other component solids. The
intersecting solids were fictitious mounting-hole bodies. Correcting the model
is supported by a failing-before/passing-after regression; it is not evidence
of a physical connector relocation or a complete mechanical qualification.
See [mechanical evidence](../hardware/docs/mechanical-validation.md).

## Implemented model and verification changes

- Exact wide-SOIC FRAM package and DNU supply connection; module 1.8 V tolerance
  still requires evidence.
- Manufacturer-derived socket signal pads, pegs and hold-down geometry,
  with four specified module/heatsink standoffs.
- Auxiliary 4.5 V PD regulator and sense clamp, corrected USB recovery service
  path and source-power interlock, KSZ9131 bias resistor, current-monitor
  scaling, and associated symbols/BOM/bring-up changes.
- Required commands fail closed; stale outputs cannot become fresh evidence.
  Netlist/model/PCB parity, duplicate drills, mechanical source binding and
  explicit R01–R09 release gates are checked. Export failures create no
  apparently valid release package.
- Path-based high-speed analysis counts actual endpoints, series capacitors,
  branches, layer changes and reference interruptions. Net-total length
  differences are retained only as historical diagnostics.

Read [electrical corrections](../hardware/docs/electrical-corrections.md),
[power/recovery review](../hardware/docs/power-and-recovery-audit.md),
[programming requirements](../hardware/docs/programming-and-recovery.md),
[verification workflow](../hardware/docs/verification-workflow.md) and
[high-speed evidence](../hardware/docs/high-speed-path-audit.md).

## Six-layer PCB decision

The candidate uses F.Cu signals / In1.Cu GND / In2.Cu power / In3.Cu internal
signals / In4.Cu GND / B.Cu signals. The original In2 power routing is preserved
where electrically valid. Dedicated In4 GND provides a continuous adjacent
reference for bottom-layer interfaces; ordinary signals can escape on In3. USB3 RX is also provisionally routed on In3; its per-layer impedance geometry remains unqualified.

The proposed 1.6 mm stack uses six 0.035 mm copper layers and dielectric
thicknesses 0.10 / 0.25 / 0.69 / 0.25 / 0.10 mm. These are design assumptions,
not a fabrication agreement. Exact laminate construction, copper thickness,
impedance coupons, interface widths/gaps and manufacturing tolerances remain
R04 qualification work. Six layers increase board cost and require fresh
manufacturing outputs. The appliance capabilities are preserved. J5 introduces an additional explicit thickness gate: Amphenol specifies a maximum 1.57 mm PCB, so the provisional 1.60 mm stack must be reconciled before fabrication; see the [connector review](../hardware/docs/connector-land-review.md).

Corrected socket hold-downs and added retention holes invalidate some nearby
copper. The synchronization script records every detached segment/via by UUID
and leaves the resulting opens visible for routing. No required pad is changed
to no-connect to hide these opens. Fixed standoff positions displaced several
nearby passives; their new placement must be included in final electrical and
mechanical checks.

## Work still in progress

Copper completion, high-speed topology/routing corrections, final mechanical
regeneration, object-specific DRC review, coherent candidate packaging and
final gate status are ongoing. Some gates also need manufacturer or physical
evidence: rail tolerances, PD/clamp transients, exact component/heatsink/access
models, fabricator impedance agreement, current sourcing and programming
assets. No software emulator establishes these physical properties.

The Docker/QEMU application lab remains deferred. The preserved
[future lab outline](future-lab.md) describes that separate work.

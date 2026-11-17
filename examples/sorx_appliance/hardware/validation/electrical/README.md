# Electrical validation evidence

This folder records independently executed model checks, not physical hardware tests.
The release status remains **NOT READY FOR FABRICATION**.

- `baseline-*` and `intermediate-*` retain reproduced failures before their fixes.
- `final-regressions.txt`: 16 electrical regression tests passing in the pinned container.
- `model-resolution.json`: KiCad7.0.11/Python3.11.2; all371 footprints and269 nets resolve.
- `final-evidence.json`: exact commands, source hashes, image identity and exit statuses.
- `power-pin-mapping.json`: resolved critical electrical pin numbers and nets.
- `static-limits.json`: explicit DC arithmetic only; no surge simulation.
- `manufacturer-sources.json`: inspected primary sources and hashes only for actual downloads.
- `generation/manifest.json`: schematic/BOM/netlist generation commands, hashes, fresh ERC/parity and unchanged PCB hash.

Run `python3 -m unittest discover -s tests -p test_electrical_model.py -v`
from `hardware/` inside the recorded image. The generators are
`python3 tools/gen_sch.py`, `python3 tools/gen_bom.py`, and
`python3 tools/power_budget.py` (save stdout to `docs/power-budget.md`).
Use `tools/verify.py` for fresh integrated ERC/netlist/PCB/mechanical checking;
its release gate intentionally remains failed while engineering blockers remain.

The container image is an isolated reuse of the inspected immutable toolchain,
not an import of Claude's unfinished design. No original checkout, baseline
archive or reference image was modified by these commands.

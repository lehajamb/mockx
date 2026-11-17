# Independent hardware verification

**NOT READY FOR FABRICATION until the current run and all R01–R09 gates pass.**

The original verifier could ignore subprocess and mechanical failures, return
success for an incomplete board, and accept entire DRC categories. The original
exporter continued after failed commands and packaged whatever files remained.
The failing baseline checks are retained in
[verification-regression-before.txt](../validation/verification-regression-before.txt).

## Commands

Run inside the dedicated hardware tool container from `hardware/tools/`:

```sh
python3 verify.py
python3 export.py --development
# Only after a passing verification and completed engineering review:
python3 export.py
```

A verification run copies only active input files into a fresh temporary
project. It never regenerates the active PCB or schematics. It runs:

- KiAuto ERC with raw output and actual subprocess status.
- A fresh schematic netlist export compared with the live design model,
  rather than a previously generated `netmap.json`.
- Board pad/net/value/footprint parity with the live model, including missing
  required pads, unexpected footprints and coincident via/drill sites.
- KiCad DRC with stored fills and again after an in-memory zone refill.
- Fresh mechanical placement extraction and mechanical generation/checking.

Every command's output is captured. A failed command, missing output, malformed
report or source edit during execution blocks release. Expected design failures
are distinct from execution errors in `verification.json`. Exit status is 0
only for a fabrication candidate, 1 for design/engineering-gate failures, and 2
for execution errors. A successful Python import does not start checks or modify
files.

Results go into a new `validation/<run-id>/` directory. The manifest records
input hashes and byte-exact source snapshots, a deterministic source identity, Git revision, container image
identity, exact commands, statuses, logs and artifact hashes.
`validation/latest.json` points to the report and records its SHA-256.
`docs/verification-report.md` is a human-readable summary. Mechanical outputs
are retained with the run to allow exports from the same checked inputs.

Set `SORX_IMAGE_ID` to the full actual container image ID when launching.
Release verification requires it, and release export must use the same image.
The environment launcher also records its locked tool environment. Source
hashes are the authoritative reference when the working tree has uncommitted
changes.

## Waivers and engineering gates

`validation/drc-waivers.json` starts with no waivers. A waiver identifies an
exact raw finding fingerprint and the exact PCB SHA-256, a reviewer and reason,
and a local evidence file plus its SHA-256. Category-only waivers, stale or
unmatched entries and changed evidence are rejected. Required unconnected
items, shorts and coincident holes cannot be waived.

Example entry shape:

```json
{
  "fingerprint": "<fingerprint from current raw DRC finding>",
  "pcb_sha256": "<current native PCB SHA-256>",
  "reviewer": "<reviewer>",
  "reason": "<object-specific engineering justification>",
  "evidence_path": "docs/<specific-review>.md",
  "evidence_sha256": "<review file SHA-256>"
}
```

All nine gates in `validation/release-gates.json` start OPEN. A completed review
has `schema_version: 1`, the current `source_identity` from verification, and
each gate's `status: PASS`, `reviewer`, `reason` and nonempty `evidence` list.
Evidence entries use `{"path": "docs/file.md", "sha256": "..."}`, relative to
the hardware root. Missing, stale or changed evidence blocks release. Do not
mark a gate PASS simply to enable the exporter; use the appliance's independent
release criteria and retain unresolved manufacturer/fabricator dependencies.

Mechanical schema version 2 must report success, geometry errors, actual PCB
and placement hashes, finite interference, port alignment, numeric clearance
checks and qualification blockers. A geometry exception, unqualified height or
mounting assumption is not equivalent to zero interference.

## Export behavior

Export writes a unique directory under `validation/exports/`. It never deletes
the active fabrication directory or overwrites the imported fabrication ZIP.
Each external command must succeed and produce required outputs. A failure
retains an explicitly incomplete result directory and returns nonzero without
creating an archive.

`--development` creates an archive named `sorx-DEVELOPMENT-NOT-FOR-FAB.zip`
with a prominent status file and `fabrication_approval: false`. It does not
claim circuit correctness. Source-bound mechanical outputs are included when
a verification run exists for the same source inputs; otherwise their absence
is explicit. Release mode refuses to run without current passing verification,
unchanged raw evidence, current source hashes and closed engineering gates.

The package includes newly generated Gerbers, drills, placement, assembly and
copper drawings, outline/board STEP, schematic/netlist, source inputs and hashes.
Physical boot, thermal, EMC and latency acceptance remains a later hardware
phase even after a fabrication candidate is generated.

## Regression tests

From the repository root:

```powershell
python -m unittest discover -s examples/sorx_appliance/hardware/tests -p test_verification.py -v
```

To safely reproduce the three original defects from the preserved ZIP (without
running its generators):

```powershell
python examples/sorx_appliance/hardware/tests/reproduce_baseline_verification_defects.py
```

That demonstration exits 0 when the expected original defects are reproduced;
it never approves fabrication. The corrected regression suite currently has
27 passing cases.

These tests cover failing subprocesses, stale and missing outputs, timeouts,
malformed reports, exact waiver matching, mechanical failures, source/evidence
binding, safe imports and refusal to approve incomplete exports. They require
only the Python standard library; KiCad and CadQuery integration is checked
separately in the locked tool environment.

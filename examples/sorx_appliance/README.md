# SORX appliance hardware workspace

**NOT READY FOR FABRICATION.** This checkout contains the independent Codex
hardware implementation on `codex/sorx-hardware-independent`. It starts from
bootstrap commit `c2e1b3c`, with immutable source-import benchmark `df82478`.
The original archives and supplied images remain unchanged.

The independent implementation is paused at the user's request. The corrected
native PCB is saved with 30 required opens; release qualification is unfinished.
Read [the Codex resume handoff](CODEX_RESUME.md) for the exact accepted state,
unpromoted experiments, validation results and remaining work.
Read the [independent implementation record](docs/independent-implementation.md),
[release criteria](docs/release-criteria.md) and
[comparison guide](docs/comparison.md). Existing historical fabrication files
are not approved manufacturing outputs.

## Contents and authority

| Path | Purpose |
|---|---|
| [hardware](hardware/) | Editable project, preserving upstream `hw/`, `mech/`, `bom/`, `docs/`, `tools/` relationships and routing checkpoints |
| [baseline](baseline/) | Immutable original ZIPs, external handoff and SHA-256 import inventory |
| [reference_images](reference_images/) | Immutable supplied images, including explicitly historical routing attempts |
| [image catalog](docs/image-catalog.md) | Stage labels, digests and duplicate relationships |
| [baseline measurements](docs/baseline-measurements.json) | Saved and independently inspected starting measurements, with evidence limits |
| [validation](docs/bootstrap-validation.md) | Bootstrap checks and hardware checks not executed |
| [future lab](docs/future-lab.md) | Later Docker venues, replay and optional ARM emulation; not implemented here |

The original [hardware handoff](hardware/HANDOFF.md) and archived ZIP contents
remain historical evidence. Active hardware reports and engineering documentation
are being regenerated; their source-bound validation records govern current status. In particular, their “Later” list contains release blockers
that must be resolved before fabrication. The independent review records the
current assessment without rewriting that evidence.

The active design and original archives are intentionally both included. The
archives preserve every original file, including the excluded bytecode/UI-state
entries. See [baseline provenance](baseline/README.md) for the exact import policy.

## Independent development environment

This standalone clone has separate Git objects, branch and working files from
Claude's checkout. Do not run its generators or use its output directories.
The dedicated [container launcher](hardware/tools/env/README.md) uses an exact
immutable image ID, unique Codex container names and read-only baseline/image
mounts. The image's tool layers were reused with attribution; no unfinished
Claude circuit changes were imported.

The original [Claude continuation prompt](CLAUDE_HANDOFF.md) remains available
for comparison. Work in this clone's `hardware/`, preserve `baseline/` and
`reference_images/`, and retain `df82478` as the fixed source benchmark.

## Check the workspace

From the repository root:

```powershell
python examples/sorx_appliance/tools/validate_bootstrap.py --pristine
```

This checks archive/member hashes, safe paths, exclusions, imported bytes, image
provenance and Python syntax without running the hardware scripts. After design
changes, omit `--pristine`: immutable evidence is still verified and changed
original hardware files are listed. Use Git as described in the comparison guide
to include newly added files and the complete engineering diff.

**A passing bootstrap check is not a passing hardware verification.** KiCad,
electrical review, sourcing, mechanical validation and physical bring-up remain
separate gates. No Docker appliance, new order client or QEMU image is delivered
by this hardware implementation.

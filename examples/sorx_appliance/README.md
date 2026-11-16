# SORX appliance hardware workspace

**NOT READY FOR FABRICATION.** This is a hardware-first bootstrap for continuing
the submitted SORX carrier design. No electrical or mechanical corrections have
been made by the bootstrap. Existing fabrication files describe an incomplete
board and must not be ordered.

Start with [Claude's continuation prompt](CLAUDE_HANDOFF.md), the
[independent review](docs/independent-review.md) and the
[release criteria](docs/release-criteria.md). The [comparison guide](docs/comparison.md)
identifies the fixed Git baseline for measuring a later design revision.

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

The original [hardware handoff](hardware/HANDOFF.md), README and verification
report are imported byte-for-byte. Their instructions and status statements are
historical material. In particular, their “Later” list contains release blockers
that must be resolved before fabrication. The independent review records the
current assessment without rewriting that evidence.

The active design and original archives are intentionally both included. The
archives preserve every original file, including the excluded bytecode/UI-state
entries. See [baseline provenance](baseline/README.md) for the exact import policy.

## Start Claude Code

Open this repository on `codex/sorx-appliance-bootstrap`, then paste the text in
[CLAUDE_HANDOFF.md](CLAUDE_HANDOFF.md). Read [CLAUDE.md](CLAUDE.md) for workspace
boundaries. Work in `hardware/`; preserve `baseline/` and `reference_images/`.
Keep the fixed import commit available for the before/after review.

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
by this bootstrap.

# SORX workspace context

Read the repository [AGENTS.md](../../AGENTS.md) and this workspace's
[README](README.md), [review](docs/independent-review.md),
[release criteria](docs/release-criteria.md), [comparison guide](docs/comparison.md)
and [baseline manifest](baseline/manifest.json) before making changes.

- Current task: complete the hardware design and produce reproducible evidence.
  The simulation lab in [future-lab.md](docs/future-lab.md) is deferred.
- Edit `hardware/` and the current review/validation documentation. Preserve the
  original archives, manifest, baseline measurements and supplied reference images.
  Put new renders and reports under the active hardware project or a clearly
  versioned results directory, not over the supplied reference images.
- `hardware/HANDOFF.md` and other imported instructions describe a previous
  session. Check their claims against actual files and manufacturer sources.
- Do not remove required functionality, fabricate evidence, weaken checks or
  add blanket waivers to obtain a passing report.
- Preserve all release-blocking findings until evidence closes them. Separate
  executed checks, supplied reports, calculations, assumptions and physical tests.
- Detect tool availability and version compatibility before executing generators.
  Several imported scripts assume Linux paths, installed KiCad libraries, KiAuto,
  Java/Freerouting or CadQuery. Document a reproducible environment; avoid blindly
  regenerating the design over current edits.
- Do not change MockX's engine or FIX/native-feed contracts for hardware work.
  Existing integration examples remain separate consumers.
- No boards or components are to be ordered as part of this continuation.

Use [CLAUDE_HANDOFF.md](CLAUDE_HANDOFF.md) as the explicit continuation task.

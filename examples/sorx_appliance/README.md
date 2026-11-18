# SORX appliance hardware workspace

**NOT READY FOR FABRICATION.** The Codex design continues here in the primary MockX checkout on `codex/sorx-appliance`. Edit [the native PCB](hardware/hw/sorx.kicad_pcb). The imported development checkpoint had **30 required opens** (16 ground, 14 other) and **93 other design-rule findings**; these are known defects, not passing release criteria.

From the repository root, show the current queue:

```powershell
python examples/sorx_appliance/tools/routing_status.py
```

Start with the [Codex task prompt](CODEX_RESUME.md) and [routing runbook](docs/routing-runbook.md). The queue, procedure and [pinned container launcher](hardware/tools/env/README.md) use this checkout. No ignored scratch copy or sibling repository is required.

| Keep handy | What it tells you |
|---|---|
| [Decision ledger](docs/routing-ledger.json) | Evidence bindings, accepted repairs, rejected attempts |
| [Transfer manifest](docs/local-transfer.json) · [consolidation checks](hardware/validation/consolidation-20261118/README.md) | What was imported, what is local-only, and which checks were reproduced |
| [Release criteria](docs/release-criteria.md) | Electrical, mechanical and manufacturing gates beyond connectivity |
| [November 17 checkpoint](CODEX_CHECKPOINT_2026-11-17.md) | Original independent implementation, experiments and remaining blockers |
| [Comparison guide](docs/comparison.md) · [independent review](docs/independent-review.md) | Historical benchmark and engineering findings |
| [Baseline](baseline/README.md) · [reference images](docs/image-catalog.md) | Immutable source archives and supplied images; never overwrite |
| [Future simulation lab](docs/future-lab.md) | Deferred Docker/QEMU integration |

Claude’s Rev B step 04 and its handoff are preserved separately at `f4dfc88` on `codex/sorx-appliance-bootstrap`. The original [Claude task prompt](CLAUDE_HANDOFF.md) and [supplier handoff](hardware/HANDOFF.md) are historical. Existing manufacturing outputs must not be sent to a factory. Source provenance can be checked with `python examples/sorx_appliance/tools/validate_bootstrap.py`; this does not qualify the hardware.

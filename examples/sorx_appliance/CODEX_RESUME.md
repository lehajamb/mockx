# Start the next Codex routing task

Use the primary `mockx` checkout, branch `codex/sorx-appliance`. The only editable board is [hardware/hw/sorx.kicad_pcb](hardware/hw/sorx.kicad_pcb). **NOT READY FOR FABRICATION.**

From the repository root:

```powershell
python examples/sorx_appliance/tools/routing_status.py
```

The output is the open-connection queue for the recorded board and rules. If it is stale, reproduce the native checks before choosing a repair. See the [one-ticket routing runbook](docs/routing-runbook.md), [decision ledger](docs/routing-ledger.json) and [release criteria](docs/release-criteria.md). The [original independent-clone checkpoint](CODEX_CHECKPOINT_2026-11-17.md) is preserved unchanged at the same directory depth so its links still work. Its branch, pause status and instructions to use the sibling checkout are historical. Claude’s step-04 circuit is separately recoverable on `codex/sorx-appliance-bootstrap` at `f4dfc88`.

## Copy into a new Codex task

> On `codex/sorx-appliance` in the primary MockX checkout, follow AGENTS.md and `examples/sorx_appliance/docs/routing-runbook.md`. Run `python examples/sorx_appliance/tools/routing_status.py`. From the accepted native board, try one named connection. Inspect the last relevant decision before routing; preserve previously connected pins, other nets and interface constraints. Use the pinned checker on fresh input. Commit a passing repair and its evidence, or record the blocker and a materially different next attempt. Leave the hardware not ready for fabrication, and do not push or order hardware.

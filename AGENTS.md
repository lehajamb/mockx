# MockX Codex Instructions

## Environment

- Windows host using PowerShell.
- Before activating Conda, initialize it in the current PowerShell session with:

  ```powershell
  . "C:\Users\lehaj\anaconda3\shell\condabin\conda-hook.ps1"
  ```

- Then activate the environment with:

  ```powershell
  conda activate mockx
  ```

- Install the package in editable mode when needed.
- Run tests with:

  ```powershell
  python -m pytest -q
  ```

- When running several commands in a fresh PowerShell process, initialize and activate Conda in the same command session before invoking Python or pytest.

## Compatibility and consumers

- MockX has no external or production clients. The only client code that consumes MockX is contained in the repository test suite.
- When making intentional changes, do not preserve legacy code solely for compatibility. Remove obsolete paths, adapters, aliases, and deprecated behavior rather than maintaining them in parallel.
- Backward compatibility is not required unless a task explicitly asks for it. Update the tests, examples, README, and configuration together when a contract intentionally changes.
- Prefer a clean, internally consistent design over compatibility shims or transitional APIs.

## Core correctness invariants

After every completed engine command:

1. If both sides exist, `best_bid < best_ask`.
2. Every live book order has status `NEW` or `PARTIAL`.
3. Every terminal order is absent from the order book.
4. IOC and market orders never rest.
5. Every level aggregate equals the sum of its queued order leaves quantities.
6. Order lookup indexes agree with the book queues.
7. Reports are immutable snapshots.
8. Equal-time scheduled events retain deterministic ordering.
9. Events exactly at the advancement target are processed.
10. Every logical persisted event has a unique identity.

## Change discipline

- Add a failing regression test before fixing a reported defect.
- Do not weaken or remove invariants to make a test pass.
- Do not modify unrelated behavior.
- Do not silently change FIX, REST, or WebSocket contracts.
- Update the README and configuration documentation when behavior changes.
- Run targeted tests and the full suite.
- Do not mark a task complete while any new or existing test fails.
- Explicitly report anything not implemented.

## Existing remediations

The original critical findings around FIFO scheduling, inclusive advancement,
immutable reports, state transitions, atomic quantity accounting, IOC/market
remainders, per-fill reporting, and API wiring have already been fixed.

Do not regress these behaviors.

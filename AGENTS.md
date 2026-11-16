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

- MockX has no production clients. Its development consumers include the repository test suite and the sibling SorX project. Review SorX's FIX and native market-data contracts when changing shared behavior; document coordinated changes without adding legacy compatibility shims.
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

<!-- trail:start -->
## Trail — repo context graph

This repo is indexed in `.trail/cache/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code through git.

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `trail map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `trail ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `trail grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `trail skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `trail callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `.trail/cache/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `trail ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `trail build` (deterministic,
no API key, $0).
<!-- trail:end -->

---
title: Preserve native smoke test coverage in architecture graphs
author: Lehajamb
date: 2026-11-16
branch: main
id: l-763d44dd
scope: repo
status: active
src: agent
---

## Decided
Preserve evidence-backed coverage links to native integration smoke scripts when reviewing an Understand-Anything graph. Check import-map evidence and the test's actual role after the automatic tested_by linker runs.

## Tried and ruled out
Accepting the merge linker's filename classification alone removed six valid production-to-test links. Its conventional test-name heuristic missed live_smoke.py, even though the files were already tagged as tests and import the exercised production components. Fixture-helper and throughput-benchmark links were correctly removed and should not be restored indiscriminately.

## Watch out
A later merge can drop these links again; review the corrected graph after the last merge. This is an analysis-tool limitation, not a defect in the exchange or tests.

In the code: examples/cpp_market_feed/tests/live_smoke.py and examples/cpp_sor/tests/live_smoke.py each import src/mockx/configuration.py, src/mockx/main.py, and src/mockx/models.py. The authoritative importMap and assemble-review.json recorded all six restored tested_by pairs.

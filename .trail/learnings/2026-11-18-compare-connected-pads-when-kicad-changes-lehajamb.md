---
title: Compare connected pads when KiCad changes which endpoint it reports
author: Lehajamb
date: 2026-11-18
branch: codex/sorx-appliance
cost: { minutes: 29, tokens: 389223 }
id: l-4d1b78f3
scope: repo
status: active
src: agent
touches:
  - examples/sorx_appliance/hardware/tools/routing_gate.py
  - examples/sorx_appliance/docs/routing-runbook.md
---

## Decided
Use pad-group membership and per-net counts to decide whether an existing SORX connection survived a routing edit. Compare individual non-open violations separately.

## Tried and ruled out
Comparing every raw DRC item on the identical board produced a false difference: KiCad selected a track endpoint in one stored report and a pad on the same USB1_OC# island in another. Comparing only total opens also hid the historical C64.2 ground regression.

## Watch out
A fresh refill and a successful checker process do not prove the target pins are joined. Stop when the independent pad partitions disagree with native DRC rather than waiving the difference.

In the code: examples/sorx_appliance/hardware/tools/routing_gate.py and examples/sorx_appliance/hardware/validation/consolidation-20261118/integration-checks.json.

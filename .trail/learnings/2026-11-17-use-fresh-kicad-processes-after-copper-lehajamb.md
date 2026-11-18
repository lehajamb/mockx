---
title: Use fresh KiCad processes after copper removal
author: Lehajamb
date: 2026-11-17
branch: codex/sorx-hardware-independent
id: l-af727b20
scope: repo
status: active
src: agent
touches:
  - examples/sorx_appliance/README.md
  - examples/sorx_appliance/hardware/bom/cost-rollup.csv
  - examples/sorx_appliance/hardware/bom/sorx-bom.csv
  - examples/sorx_appliance/hardware/bom/stock-check.csv
  - examples/sorx_appliance/hardware/docs/block-diagram.md
  - examples/sorx_appliance/hardware/docs/block-diagram.svg
  - examples/sorx_appliance/hardware/docs/bring-up-checklist.md
  - examples/sorx_appliance/hardware/docs/design-review-notes.md
  - examples/sorx_appliance/hardware/docs/manufacturing-test-plan.md
  - examples/sorx_appliance/hardware/docs/power-budget.md
---

## Decided

With the pinned KiCad 7.0.11 Python image, isolate board-edit stages in fresh Python processes. Remove reviewed copper by exact serialized segment/via UUIDs and reload in a new process before further pcbnew operations. Preserve removal logs and run fresh native DRC afterward.

## Tried and ruled out

Repeated board.Remove(track) calls corrupted SWIG's wrapping metadata: GetNetInfo, GetFootprints and Pads returned bare SwigPyObject values, and even reloading the board in the same process could return an unwrapped object. Caching NETINFO values and same-process SaveBoard/LoadBoard roundtrips did not reliably cure it. This is a tool-binding failure, not evidence that routing passed.

## Watch out

Do not filter arbitrary board lines or suppress the exception. Match only known segment/via UUIDs, assert the full requested removal set was found, and preserve all required nets/pads. Footprint replacement can also invalidate cached wrappers. Zone copying must use Duplicate() to generate a distinct UUID; the ZONE copy constructor preserves its source identity.

In the code: examples/sorx_appliance/hardware/tools/sync_codex_board.py; correct_high_speed.py; image sha256:23dbab2af8391fba7496a546d6729c37a5e8bdfb8e941ab903589252e8bec1e2.

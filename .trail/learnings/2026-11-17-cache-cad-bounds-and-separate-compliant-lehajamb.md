---
title: Cache CAD bounds and separate compliant supplier parts before checking assemblies
author: Lehajamb
date: 2026-11-17
branch: codex/sorx-hardware-independent
id: l-f7cfc063
scope: repo
status: active
src: agent
touches:
  - examples/sorx_appliance/.gitattributes
  - examples/sorx_appliance/README.md
  - examples/sorx_appliance/hardware/docs/mechanical-validation.md
  - examples/sorx_appliance/hardware/docs/supplier-geometry.md
  - examples/sorx_appliance/hardware/docs/verification-report.md
  - examples/sorx_appliance/hardware/docs/verification-workflow.md
  - examples/sorx_appliance/hardware/mech/vendor/toradex/heatsink-datasheet.pdf
  - examples/sorx_appliance/hardware/mech/vendor/toradex/heatsink-type1-v11-README.txt
  - examples/sorx_appliance/hardware/mech/vendor/toradex/heatsink-type1-v11.step
  - examples/sorx_appliance/hardware/mech/vendor/toradex/heatsink-type1-v11.zip
---

## Decided
Cache each component bounding box once during an assembly fit check, then retain exact solid intersections for every candidate pair. Split supplier assemblies into their actual rigid parts and compliant thermal material. Keep unresolved TIM compression and exact-revision geometry as release blockers.

## Tried and ruled out
Repeated bounding-box extraction inside the all-pairs loop made a supplier-model check exceed six minutes: 106,643 expensive CAD calls for 327 components. A bounded-call regression reproduced this; caching brings the complete nine-test suite to roughly30 seconds. Treating the full Toradex heatsink STEP as one rigid metal body would also include uncompressed TIM and screws at incorrect support heights.

## Watch out
Supplier CAD can contain internal screw/metal overlap and conservative module envelopes; neither authorizes a blanket waiver. The Type1 near feet seat at carrier Z=7, while far feet seat on the module PCB near Z=3.73 above2.5mm standoffs. Hash the vendor originals, validate their identities before cached imports, and copy them into fresh verification scratch. Never trim compliant parts to claim a qualified physical fit.

In the code: examples/sorx_appliance/hardware/tools/gen_mech.py; vendor_geometry.py; tests/test_mechanical.py; docs/supplier-geometry.md. The pinned STEP identity and coordinate derivation are recorded in mech/vendor/toradex/manifest.json.

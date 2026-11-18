# Fixed baseline and engineering comparison

**Baseline status: NOT READY FOR FABRICATION.** This bootstrap changes no
hardware design. All R01–R09 release gates remain open.

The source-only import commit is
`df8247829ee12cb5b1c03e0d8fa1c29383a95f74` (`df82478`), on
`codex/sorx-appliance-bootstrap`, based directly on main commit
`ef5715c3763b98b1f522a865df3beaac2625ffcd`. The following documentation commit
adds the review and continuation instructions; it does not alter that imported
source. Keep the import commit in history and use its full ID for comparison.

The [baseline manifest](../baseline/manifest.json) records original hashes and
every archive member. The [measurement record](baseline-measurements.json)
preserves the submitted starting values and their evidence limits. Preserve both
files, the original archives/handoff and `reference_images/` unchanged. Keep new
measurements in a separate revision-specific result file under active hardware
or a committed results directory.

## Reproduce provenance and inspect changes

Run from the repository root; these commands only inspect files:

```powershell
# On the freshly imported design, require identical active hardware too.
python examples/sorx_appliance/tools/validate_bootstrap.py --pristine

# After hardware edits, check immutable evidence and list changed imports.
python examples/sorx_appliance/tools/validate_bootstrap.py

git diff df8247829ee12cb5b1c03e0d8fa1c29383a95f74 -- examples/sorx_appliance/hardware
git diff --exit-code df8247829ee12cb5b1c03e0d8fa1c29383a95f74 -- examples/sorx_appliance/reference_images
git diff --exit-code df8247829ee12cb5b1c03e0d8fa1c29383a95f74 -- examples/sorx_appliance/baseline/manifest.json 'examples/sorx_appliance/baseline/HANDOFF (1).md' examples/sorx_appliance/baseline/sorx-hw-2026-11-16-final.zip examples/sorx_appliance/baseline/sorx-images-2026-11-16.zip
git status --short -- examples/sorx_appliance
git ls-files --others --exclude-standard -- examples/sorx_appliance
```

`git diff <commit>` includes staged and unstaged tracked changes. Newly added
untracked files require the final two commands. `baseline/README.md` is added in
the documentation commit, so the baseline comparison lists the four preserved
source/manifest paths explicitly. A provenance pass does not execute or approve
the hardware verifier.

## Comparable measurements

| Measure | Submitted baseline | Reproduction / acceptance |
|---|---:|---|
| Footprints / pads / named nets | 348 / 1,500 / 262 | Count actual final PCB objects; explain intentional changes |
| Track segments / vias | 3,317 / 1,180 | Count actual PCB objects; fewer objects alone is not an improvement |
| Archived ERC errors / warnings | 0 / 0 | Rerun ERC using validated symbols and power modeling; retain raw output |
| Unconnected DRC items | 51 | Fresh DRC and object-level review; zero required opens |
| Other raw DRC findings | 255 | Preserve raw counts and document specific reviewed exceptions |
| OE_MDI1 / OE_MDI3 path mismatch | 4.365 / 10.757 mm | Validate real paths against interface requirements, not total-net sums alone |
| Saved J9 enclosure interference | 5.002 mm³ | Recompute with final PCB, complete geometry and explicit tolerances |
| Stock-checked active/electromechanical lines | 7 / 39 | Dated exact-MPN sourcing evidence for every required line |

The numeric record comes from the earlier independent review of this exact
hardware ZIP. ERC, DRC and interference volumes are supplied archived results;
the bootstrap did not rerun their hardware tools. The path-length method and
evidence classes are recorded in the JSON. For a fair comparison, record any
changed tool version, calculation method, model coverage or constraint set.
Keep raw counts even when a legitimate waiver changes the release decision.

## Before/after findings table

Maintain this table, or a linked revision-specific copy, with baseline failure
evidence, the corrective change, the exact command/check, the new raw evidence
and the source revision. Do not replace an open gate with a bare “fixed.”

| ID | Baseline | After / evidence | Status |
|---|---|---|---|
| R01 | 51 unconnected items; signal/supply and GND review required | No hardware changes in bootstrap | OPEN |
| R02 | FRAM package and supply-tolerance mismatch | No hardware changes in bootstrap | OPEN |
| R03 | Exact socket land pattern and retention not fully validated | No hardware changes in bootstrap | OPEN |
| R04 | MDI skew and reference continuity issues | No hardware changes in bootstrap | OPEN |
| R05 | Protection, PD load behavior and power/test review required | No hardware changes in bootstrap | OPEN |
| R06 | J9 interference and incomplete assembly/thermal validation | No hardware changes in bootstrap | OPEN |
| R07 | Verification failures can return success; duplicate drill hits | No hardware changes in bootstrap | OPEN |
| R08 | Incomplete design exports and contradictory documentation | No hardware changes in bootstrap | OPEN |
| R09 | Partial sourcing and missing programming workflows/assets | No hardware changes in bootstrap | OPEN |

Use the [independent review](independent-review.md) for detail and the
[release criteria](release-criteria.md) for closure. A fabrication candidate
requires all applicable design gates closed or an explicit blocked status. It
is not evidence of physical bring-up, latency, signal integrity or compliance.

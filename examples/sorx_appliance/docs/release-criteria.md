# SORX hardware release criteria

**Current status: NOT READY FOR FABRICATION. All R01–R09 gates are open.**

These criteria govern the active design under [hardware/](../hardware/).
The [independent review](independent-review.md) defines the baseline findings.
The imported handoff's “Later” section does not defer work required by these
gates. Existing Gerbers are outputs of the incomplete baseline, not an approved
manufacturing release.

## Evidence and status rules

For every gate, record the exact source revision, tool versions, commands,
dated manufacturer references, raw outputs and artifact hashes used to close it.
Use `OPEN`, `PASS` or `BLOCKED` in the comparison record. A blocker must name the
missing evidence or external dependency and the work already completed.

Do not mark a gate passed solely because another generated artifact agrees with
the same model. Keep these evidence classes distinct:

- Independently executed checks and calculations.
- Manufacturer specifications and fabrication/assembly confirmations.
- Engineering assumptions requiring justification or later measurement.
- Physical tests that cannot be performed before boards exist.

## Required gates for a fabrication candidate

| ID | Gate | Required closure evidence |
|---|---|---|
| R01 | Complete electrical connectivity | Fresh final-board DRC has zero required unconnected items, including GND. Every prior open is resolved or shown to be unnecessary copper, with object-level evidence. Required pins and functions are not deleted or declared no-connect to suppress failures. |
| R02 | FRAM selection and electrical model | Exact orderable FRAM part, package, footprint, pin mapping and operating-voltage tolerance agree. Review critical active-component pin mappings and supply ranges against manufacturer documentation. Record the failing baseline and corrected result. |
| R03 | Exact SoM socket and retention | Compare all numbered pads, pegs, hold-downs, orientation and seating geometry with the TE 2309409-2 drawing and Toradex land pattern. Verify module retention and the selected heatsink's mounting arrangement. Broad family dimensions alone are insufficient. |
| R04 | High-speed layout | Document interface-specific requirements and inspect actual paths, coupling, symmetry, length/skew, vias, reference continuity and connector transitions. Resolve the I210 MDI mismatch and In2 reference interruptions. Agree the controlled-impedance stackup with a fabricator or clearly identify that external gate as blocked. |
| R05 | Power and protection | Verify rail tolerance/current/voltage drop, regulator layout and decoupling, startup/shutdown, PD contract handling, eFuse thresholds and transient-protection coordination. Correct unsafe or invalid test expectations. Support conclusions with datasheets, calculations and available validated models. |
| R06 | Mechanical and thermal integration | Resolve J9's interference; use verified part heights and a specified heatsink/mounting solution; allow manufacturing and assembly tolerances, including the fan. Check the complete assembly and access envelopes against the final PCB. CAD errors fail the check. Thermal assumptions and later measurement limits remain explicit. |
| R07 | Trustworthy automated verification | Required subprocess failures propagate and return nonzero status. Electrical, netlist and required mechanical failures affect overall status. Remove duplicate vias/drill hits. Replace blanket waivers with specific reviewed exceptions. Reproduce any retained negative-test claims and archive their results. |
| R08 | Coherent manufacturing candidate | Regenerate schematic, PCB, BOM, Gerbers, drills, placement/assembly files and mechanical outputs from the same revision. Confirm package contents against their manifest. Reconcile specifications and bring-up instructions. A failed export cannot produce an apparently successful release package. |
| R09 | Sourceability and programming readiness | Check every required BOM item for exact MPN/package, dated availability and assembly quantity; qualify alternates explicitly. Specify PD configuration and I210 NVM/MAC programming assets, lawful acquisition dependencies, programming and read-back procedures. Unknown assets or unverified substitutions remain blockers. |

Power pours, routing-layer changes or component substitutions may be appropriate
engineering remedies. Record the reason, cost/availability implications and
impact on interfaces and mechanics. The acceptance criterion is a justified,
validated design; a particular implementation technique is not a substitute for
that evidence.

## Minimum reproducible check sequence

1. Record the source revision and tool environment. Preserve the submitted
   baseline and references unchanged.
2. Validate the electrical model and footprint selections. Export a fresh
   schematic netlist and compare it with the intended model and PCB.
3. Refill zones and run fresh ERC/DRC on the final revision. Review each waiver
   against exact objects; retain the unfiltered raw reports.
4. Run path-based high-speed and power checks using the documented constraints.
   Inspect remaining issues that ordinary DRC cannot cover.
5. Regenerate mechanical data from that PCB and run comprehensive fit checks.
   A geometry exception or missing required model cannot count as zero overlap.
6. Generate the complete manufacturing candidate only after prerequisite gates
   pass. Recheck exports, drill duplicates, placement, BOM population and hashes.
7. Produce a before/after table for R01–R09, change log, current status, exact
   commands, raw evidence and remaining physical-test requirements.

Archive raw failures as well as successful reruns when they demonstrate a
reported defect and its correction. A scripted green summary without its
supporting evidence is insufficient.

## Physical acceptance remains a later phase

Even a fabrication candidate has not passed hardware acceptance. The physical
bring-up plan must include power sequencing and fault tests, boot/recovery,
all Ethernet and USB interfaces, FRAM/storage, watchdog/indicators, PPS and
timestamp behavior, sustained workload, thermal soak and relevant electrical
robustness checks. Use measured values and explicit pass thresholds.

Do not claim physical boot, signal-integrity measurements, EMC compliance,
thermal performance or SOR wire-to-wire latency from CAD checks or software
emulation. Do not place manufacturing orders as part of completing these gates.

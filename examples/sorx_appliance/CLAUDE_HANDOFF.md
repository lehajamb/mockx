# Claude Code continuation prompt

Paste the text below after opening this repository on
`codex/sorx-appliance-bootstrap`. The source import commit is recorded in the
[comparison guide](docs/comparison.md).

```text
Continue and complete the SORX hardware design in
examples/sorx_appliance/hardware/.

Hardware completion is the priority. The Docker/QEMU integration lab is
future work. Read the repository AGENTS.md, this example's CLAUDE.md,
independent review, release criteria and baseline manifest first.

Treat the original handoff as historical evidence, not authority to defer
release blockers. “Final” filenames and existing Gerbers do not establish
manufacturing readiness.

Preserve baseline/ and reference_images/ unchanged. Work on the active
hardware project and maintain a before/after findings table with evidence.

First inspect the actual files and available tools. Establish a reproducible
tool environment, record versions, and reproduce baseline checks where
possible. Do not assume the original /home/claude paths or installed tools.
Do not blindly run generators that could overwrite the current design.

Preserve the intended appliance capabilities: Verdin iMX8M Plus carrier,
three role-separated GbE ports, PPS, USB-C PD power, console/recovery,
storage, watchdog, status indicators and enclosure. Explain any necessary
component, stackup or mechanical changes and their consequences.

Complete these engineering tasks:

1. Correct the FRAM package/footprint and supply-tolerance mismatch.
   Validate the exact TE socket land pattern and critical component
   pinouts against manufacturer documentation.

2. Review power sequencing, distribution, transient protection and PD
   load control. Correct the reverse-polarity and recovery instructions.
   Specify required PD and I210 programming artifacts and procedures.

3. Close every required signal, supply and GND connection. Investigate
   GND findings individually. Remove duplicate vias and drill hits.
   Do not delete required functionality or mark required pins unconnected
   to obtain a passing result.

4. Review and correct high-speed routing using documented,
   interface-specific constraints. Check actual source-to-destination
   paths, symmetry, coupling, layer transitions and reference continuity.
   Ordinary clearance DRC is insufficient.

5. Resolve J9's reported enclosure collision. Validate connector access,
   component heights, fan tolerance, heatsink selection and module
   mounting. Regenerate mechanical data from the final PCB.

6. Repair verification so failed subprocesses and required electrical or
   mechanical checks produce failure and a nonzero exit status.
   Geometry exceptions must never become zero interference.
   Waivers must identify specific objects and supporting evidence.

7. Regenerate coherent schematics, PCB, BOM, assembly/placement files,
   Gerbers, drills, mechanical models and documentation from the same
   revision. Reconcile contradictory specifications and complete the
   sourcing review.

For each reported defect, reproduce or document the failing condition
before fixing it, then retain the validation evidence. Do not weaken
checks or introduce blanket waivers to make the report green.

Deliver a reproducible fabrication candidate with:
- a concise engineering change log;
- a before/after comparison against the preserved baseline;
- exact validation commands, tool versions and raw reports;
- artifact hashes and an unambiguous release status;
- remaining limitations and physical bring-up tests.

Distinguish independently executed checks, manufacturer requirements,
engineering assumptions and tests requiring real hardware. Never invent
stock checks, NVM assets, simulation results or hardware measurements.

Continue all work that can be completed. If a release gate cannot be
closed, identify the exact blocker and evidence needed; do not label the
design fabrication-ready. Do not order boards or components.
```

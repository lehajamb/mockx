# Independent mechanical correction and validation

**NOT READY FOR FABRICATION.** The corrected verifier deliberately returns failure until the qualification gates below are closed. This is a reproducible geometric model and a defect correction, not certification of the physical assembly.

## Reproduced defect: the saved J9 collision was misattributed

The preserved ZIP's complete generator reports J9 interference of 5.002 mm3. Independently intersecting J9 alone with that same enclosure gives **0.0 mm3**. In the original unused accumulator loop, `comp = s` aliases the first component, J9, and each `comp.add(s)` mutates that component's CadQuery object list. J9 grows from one object to 350 objects. Four mounting-hole footprints, incorrectly represented as solid components, each contribute approximately 1.250567695 mm3; their sum is the reported 5.002270780 mm3.

The correction removes the mutating accumulator. Mounting holes, fiducial copper and testpoint pads are explicitly classified as PCB features, with actual enclosure/PCB holes retained. It does not enlarge the USB opening to conceal an unrelated collision. Exact reproduction is in [alias-diagnosis.json](../validation/mechanical-regressions/alias-diagnosis.json). The baseline ZIP SHA-256 is `2f51a36928e87cb51dcb15f3fa5f04b5b789473a67bb6b2b4529f671bc91addd`.

## Verification behavior

- STEP assembly export now includes every modeled fitted component; a regression first reproduced the prior omission of ICs and passives.
- Every modeled fitted component, PCB and fan is checked against both enclosure halves. Component pairs are also checked using bounding boxes only as a broad-phase filter before actual solid intersection. The sole declared mating-contact exception is the specific J1/M1 socket/module pair.
- CAD exceptions, invalid solids, negative or non-finite volumes fail. An unknown intersection is never converted to zero. The CLI writes a fresh failing report and returns nonzero on input or generation errors.
- Bottom-side package height is no longer clipped to 1.2 mm; the SOT-223 regression retains its full 1.8 mm modeled height.
- `mech_export.py` rejects duplicate references and missing envelope geometry. Its sidecar binds placements to PCB bytes, placement bytes and footprint count. The fit report rejects missing or stale sidecars and includes model-source hashes.
- Placement graphics and fallback pad bounding boxes are not supplier body models. Every remaining height requiring exact-MPN qualification is listed in the report; success cannot be obtained merely by passing geometric intersections.
- Report `max_enclosure_interference_mm3` is the enclosure-only maximum. `max_interference_mm3` also includes component-pair interference. `geometry_pass` and overall `success` are distinct.

The initial five regressions produced three failures and one error against the original code. All eight current tests pass, including new provenance and duplicate-reference checks. See [before.log](../validation/mechanical-regressions/before.log), [after.log](../validation/mechanical-regressions/after.log), [after.json](../validation/mechanical-regressions/after.json) and [cli-failure.json](../validation/mechanical-regressions/cli-failure.json). The failed-before suite contained the original five tests; the two provenance tests and the module-keepout regression were added subsequently.

## Module and heatsink mounting

The selected assembly uses Toradex **23111101, Verdin Industrial Heatsink Type 1 V1.1B**, Wurth WA-SMSI M2 surface-mount standoffs, and four M2x10 heatsink screws. The screw material/head procurement part and thread engagement remain to be confirmed. Maximum assembly torque is 0.2 Nm; this is not a target torque independently qualified for the complete enclosure.

Coordinates below are relative to J1's footprint origin, in PCB X/Y millimetres, with the module extending in +Y. They use the corrected TE socket convention: peg-axis Y=-7.926, center X=-0.0055. Root PCB generation applies these positions.

| Reference | X offset | Y offset | Height | Manufacturer part |
|---|---:|---:|---:|---|
| H5 | -32.6055 | 23.074 | 2.5 mm | Wurth 9774025243R |
| H6 | 32.5945 | 23.074 | 2.5 mm | Wurth 9774025243R |
| H7 | -40.0055 | -7.926 | 7.0 mm | Wurth 9774070243R |
| H8 | 39.9945 | -7.926 | 7.0 mm | Wurth 9774070243R |

The common [Standoff_SMT_M2_WA-SMSI footprint](../hw/sorx.pretty/Standoff_SMT_M2_WA-SMSI.kicad_mod) has a 3.0 mm NPTH, 5.3 mm solder land, 4.35 mm body, 5.8 mm courtyard and separated paste quadrants. It derives from the pinned container's KiCad `Mounting_Wuerth_WA-SMSI-M2_H2.5mm_9774025243` footprint, checked against the Wurth drawings. Pad number 1 is an explicitly unconnected mechanical solder ring; it is not silently tied to electrical GND. The fixed-height upstream STEP reference is removed so it cannot misrepresent the 7 mm part. Wurth specifies a 2.8 mm pilot extending 1.4 mm below the body, nominal height tolerance +/-0.1 mm, and minimum 150 micrometre stencil thickness.

Sources: [2.5 mm standoff drawing](https://www.we-online.com/components/products/datasheet/9774025243R.pdf), [7 mm standoff drawing](https://www.we-online.com/components/products/datasheet/9774070243R.pdf), [Toradex carrier guide](https://docs.toradex.com/108140-verdin-carrier-board-design-guide.pdf), [heatsink product and revision history](https://developer.toradex.com/hardware/verdin-som-family/add-ons/verdin-industrial-heatsink/), [heatsink dimensions](https://docs.toradex.com/109465-verdin_industrial_heatsink_datasheet.pdf).

The selected V1.1B revision changes thermal interface material relative to V1.1A. The current simplified envelope uses the 85.6 x 38 mm outline and a conservative top bound of 21.27 mm above the carrier (7 mm support plus full 14.27 mm supplier height). It does not substitute a generic adhesive heatsink. Supplier STEP inspection confirmed the metal body's 85.6 x 38 x 14.27 mm bounding dimensions, but the actual stepped feet, thermal-pad compression, Verdin component shapes and screw access still require a qualified assembly model. The downloaded reference stays in ignored scratch, not in the immutable baseline:

- Supplier archive: [Toradex Type 1 V1.1 STEP](https://docs.toradex.com/118320-verdin-industrial-heatsink-type-1-v1.1-3d-step-model.zip), SHA-256 `05a7604c77506250360810d99c1c6f3c9b9648d1b838357d34513abe00bbc280`.
- Extracted STEP SHA-256: `dd95472cc1919f86d7e7459711e71772d5f5ebf35befb3d85d4f56e87794e0d9`.

The module is modeled seated at 2.5 mm on H5/H6, with 1.2 mm PCB thickness. Toradex table 43 gives 2.62 mm nominal connector-only spacing and the clip permits 2-3 mm; the chosen standoffs establish the seated parallel position. The added conservative under-module check screens X=43.2-112.8, Y=55.5-90.5 mm at the baseline J1 location. No baseline top-side component envelopes overlap that rectangle, apart from the mating socket. Any future overlap requires relocation or exact manufacturer edge-zone/maximum-height qualification; the general allowed height is zero, with a limited 0.8 mm exception near the edge connector. Carrier bottom-side components are outside that under-module restriction, but remain subject to floor clearance checks.

## Enclosure and remaining release gates

The original cavity had zero nominal vertical tolerance for a 40 mm fan. Floor and lid thickness are now each 1.5 mm, retaining the 44 mm outer height and producing a 41 mm cavity: 0.5 mm nominal vertical clearance per side. This is a design change requiring enclosure material/process and strength qualification; it is not proof that a thin printed rack enclosure is mechanically adequate. The fan's exact maximum body, hole pitch, screw choice and accumulated tolerances still need a signed dimensional check.

Remaining gates are explicit in every fit report:

1. Exact manufacturer maximum-height/body qualification for the populated BOM, including bottom-side parts. Current nominal heights and generic package bounds are evidence of coverage, not validated worst-case envelopes.
2. Supplier heatsink/SoM/TIM geometry, mounting vertical stack, screw engagement and assembly access.
3. Floor/lid strength and process tolerances, fan dimensions and clearance accumulation; carrier/enclosure mounting inserts and screws need exact procurement definitions.
4. Plug housings, RJ45 latch actuation, USB access and microSD insertion/removal travel. Opening alignment alone does not prove usable access.
5. Regeneration and collision checks against the final routed PCB, with a matching source hash. Physical thermal soak, airflow and assembly measurements follow fabrication of a justified prototype and remain separately identified.

No blanket geometry waiver or fabrication-ready label is introduced.

## Reproduce

Use immutable container image `sha256:23dbab2af8391fba7496a546d6729c37a5e8bdfb8e941ab903589252e8bec1e2`; CadQuery 2.4.0 under `/opt/cq/bin/python` (Python 3.11.2), system Python with KiCad pcbnew. Mount only the independent clone and use a dedicated `sorx-codex-mech-*` container. From `hardware/`:

```sh
/usr/bin/python3 tools/mech_export.py hw/sorx.kicad_pcb hw/build/mech_parts.json
/opt/cq/bin/python tools/gen_mech.py --parts hw/build/mech_parts.json --pcb hw/sorx.kicad_pcb --output-dir mech --check-only
/opt/cq/bin/python -m unittest discover -s tests -p test_mechanical.py -v
```

Omit `--check-only` to regenerate development STEP/STL/3MF and render outputs. A nonzero result with reported qualification blockers is the expected honest status until those gates are resolved. Do not reuse a saved report as fresh evidence or copy its success status across PCB revisions.

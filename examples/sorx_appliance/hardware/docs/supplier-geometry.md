# Supplier geometry and assembly placement

**NOT READY FOR FABRICATION.** These models improve geometric checks; they do not qualify material, tolerances, thermal performance or a manufactured assembly.

Original Toradex archives, extracted STEP models, supplier readme/license notices and the heatsink datasheet are preserved in `../mech/vendor/toradex/`. `manifest.json` records URLs, retrieval date and SHA-256 identities. Verification hashes every vendor input and copies them into its fresh scratch project. A changed STEP is rejected before import if its manifest digest does not match.

## Coordinate evidence

Carrier coordinates use PCB top Z=0 and mechanical Y=-PCB Y. For J1=(78,64), the supplier generic module is rotated 180 degrees about Z and translated to (112.7945,-56.074,3.7). The supplier PCB, top maximum-height and bottom maximum-height solids (indices 0,249,250 of the hash-bound model) provide conservative fit geometry. Module bottom-component extent is Z=0.5; top-component extent is Z=6.7. The actual module revision and height tolerances remain to be qualified. The two module holes align to H5/H6 at (45.3945,87.074)/(110.5945,87.074) in PCB coordinates.

The Type 1 heatsink model contains six independent solids: metal, uncompressed TIM and four screws. Rotate each 90 degrees about X and translate to (77.9945,-72.074,7.0). The near feet seat at Z=7 on H7/H8; the far feet extend down to Z=3.73, approximately the selected module PCB top above the 2.5mm standoffs. The model metal top is Z=18.0. The supplier drawing specifies 85.6x38mm overall metal footprint, 80/65.2mm screw-row pitches, 31mm between rows, M2x10 screws and maximum 0.2Nm assembly torque. These dimensions are regression checked independently of the enclosure collision calculation.

The earlier conservative box occupied Z=7..21.27 and did not represent the stepped feet or screws. The new assembly contains the actual metal and all four supplier screw solids; the module is represented by the supplier maximum-height envelope, not a promise that the V1.0 reference matches the purchased module revision.

## Thermal interface remains open

The uncompressed supplier TIM is 3.175mm thick. It is deliberately kept separate from the rigid collision assembly: treating compliant thermal material as a metal interference would give a misleading result, while silently trimming it would invent a verified compression state. The fit report explicitly blocks release until exact CPU height, selected module revision, TIM compression/load and contact pressure are qualified. The selected 23111101 V1.1B changes to softer TGP3500ULM material; the published V1.1 STEP is geometric reference only.

Source: [Toradex heatsink documentation](https://developer.toradex.com/hardware/verdin-som-family/add-ons/verdin-industrial-heatsink/) and [Verdin iMX8M Plus CAD downloads](https://developer.toradex.com/hardware/verdin-som-family/modules/verdin-imx8m-plus/). Do not infer real thermal measurements or fabrication approval from these files.

## Validation

Run `python -m unittest discover -s ../tests -p test_vendor_binding.py -v` with the standard interpreter and ` /opt/cq/bin/python -m unittest discover -s ../tests -p test_vendor_geometry.py -v` in the pinned container. The two source-binding tests failed before the implementation and pass afterwards; raw before/after logs are under `../validation/`. The three supplier geometry tests cover tamper rejection, screw-hole alignment, supplier overall dimensions and maximum-height envelope clearances.

## Current result and unresolved geometry

Fresh check of checkpoint09 has no geometry exceptions and no enclosure interference. It fails on approximate J1-envelope overlaps with H7/H8 and SC3/SC4, and 0.929296mm3 intersections between supplier metal and each near-row screw. No object waiver is applied. The exact TE customer drawing and STEP are linked from its official product page, but direct downloads returned HTTP403; a browser attempt also failed. The unqualified J1 box therefore remains a release blocker rather than being trimmed to produce a pass.

The supplier geometry exposed a checker performance defect: component bounds were recomputed 106,643 times in the pair screen. A failing bounded-call regression preceded caching one bound per component. All exact candidate-pair intersections remain. The nine mechanical tests now complete in about30 seconds; the source-bound current assembly check still correctly exits1 for the above defects and qualification blockers.

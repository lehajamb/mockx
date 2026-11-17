# Supplier geometry and assembly placement

**NOT READY FOR FABRICATION.** These models improve geometric checks; they do not qualify material, tolerances, thermal performance or a manufactured assembly.

Original Toradex archives, extracted STEP models, supplier readme/license notices and the heatsink datasheet are preserved in `../mech/vendor/toradex/`. The exact TE 2309409-2 customer model and revision 6 drawing are preserved in `../mech/vendor/te/`. `manifest.json` records URLs, retrieval date and SHA-256 identities. Verification hashes every vendor input and copies them into its fresh scratch project. A changed STEP is rejected before import if its manifest digest does not match.

## Coordinate evidence

Carrier coordinates use PCB top Z=0 and mechanical Y=-PCB Y. For J1=(78,64), the supplier generic module is rotated 180 degrees about Z and translated to (112.7945,-56.074,3.7). The supplier PCB, top maximum-height and bottom maximum-height solids (indices 0,249,250 of the hash-bound model) provide conservative fit geometry. Module bottom-component extent is Z=0.5; top-component extent is Z=6.7. The actual module revision and height tolerances remain to be qualified. The two module holes align to H5/H6 at (45.3945,87.074)/(110.5945,87.074) in PCB coordinates.

The Type 1 heatsink model contains six independent solids: metal, uncompressed TIM and four screws. Rotate each 90 degrees about X and translate to (77.9945,-72.074,7.0). The near feet seat at Z=7 on H7/H8; the far feet extend down to Z=3.73, approximately the selected module PCB top above the 2.5mm standoffs. The model metal top is Z=18.0. The supplier drawing specifies 85.6x38mm overall metal footprint, 80/65.2mm screw-row pitches, 31mm between rows, M2x10 screws and maximum 0.2Nm assembly torque. These dimensions are regression checked independently of the enclosure collision calculation.

The earlier conservative box occupied Z=7..21.27 and did not represent the stepped feet or screws. The new assembly contains the actual metal and all four supplier screw solids; the module is represented by the supplier maximum-height envelope, not a promise that the V1.0 reference matches the purchased module revision.

## Thermal interface remains open

The uncompressed supplier TIM is 3.175mm thick. It is deliberately kept separate from the rigid collision assembly: treating compliant thermal material as a metal interference would give a misleading result, while silently trimming it would invent a verified compression state. The fit report explicitly blocks release until exact CPU height, selected module revision, TIM compression/load and contact pressure are qualified. The selected 23111101 V1.1B changes to softer TGP3500ULM material; the published V1.1 STEP is geometric reference only.

Source: [Toradex heatsink documentation](https://developer.toradex.com/hardware/verdin-som-family/add-ons/verdin-industrial-heatsink/) and [Verdin iMX8M Plus CAD downloads](https://developer.toradex.com/hardware/verdin-som-family/modules/verdin-imx8m-plus/). Do not infer real thermal measurements or fabrication approval from these files.

## Validation

Run `python -m unittest discover -s ../tests -p test_vendor_binding.py -v` with the standard interpreter and ` /opt/cq/bin/python -m unittest discover -s ../tests -p test_vendor_geometry.py -v` in the pinned container. The two source-binding tests failed before the implementation and pass afterwards; raw before/after logs are under `../validation/`. The six supplier geometry tests cover tamper rejection, screw-hole alignment, supplier overall dimensions, maximum-height envelope clearances, the socket seating datum and near-row rigid-body clearances.

## Exact socket and screw seating corrections

The first supplier-model check of checkpoint 09 failed on the approximate J1 box: it overlapped H7/H8 by approximately 20.76/20.92 mm3 and the near screws by approximately 0.46/0.50 mm3. The actual socket has a different body profile. Initial direct downloads were blocked, but the official TE page subsequently delivered both the customer STEP archive and the revision 6 drawing through its CAD/download controls. These original bytes and hashes are retained; the failed download was not treated as the final availability result.

The exact TE solid is rotated +90 degrees about X, then translated to `(J1.x-0.0055, -(J1.y+12.924), 2.875)`. The 260 solder-tail bottom faces have raw Y=-2.875 mm, establishing PCB top Z=0. The locating-peg axes have raw X=+/-34.4 mm and raw Z=-20.85 mm. This transformation aligns the 68.8 mm peg pitch to the footprint and gives a nominal body maximum Z=5.15 mm, inside the drawing's 5.20+/-0.25 mm envelope. The transform is derived from the seating features; the solid is not trimmed to clear adjacent parts. See [the independent land-pattern check](te-socket-land-validation.md).

The original supplier assembly also places each near-row screw underhead at raw Y=2.85 mm while the corresponding metal seat is at raw Y=3.00 mm. That causes a 0.929296 mm3 rigid intersection per screw. SC3 and SC4 are translated +0.15 mm in assembled Z to seat on those surfaces. SC1 and SC2 retain their supplier positions. No solid shape is changed. The report records both adjustments and their basis; screw torque, engagement and tolerance qualification remain open.

Failing regressions reproduced both conditions before the corrections: [socket](../validation/socket-geometry-before.log) and [screw seating](../validation/screw-seating-before.log). All six supplier tests pass [afterwards](../validation/socket-geometry-after.log). The fresh source-bound [checkpoint 13c fit](../validation/socket-fit-13c/fit-check.json) has no geometry exceptions, no reported component interference and no enclosure interference. The maximum numerical intersection is 0.000006 mm3, below the explicit 0.001 mm3 numerical threshold. `geometry_pass` is true; overall `success` remains false because the listed physical and dimensional qualification gates are open. This result applies to its recorded PCB hash, not automatically to a later routing candidate.

The supplier geometry also exposed a checker performance defect: component bounds were recomputed 106,643 times in the pair screen. A failing bounded-call regression preceded caching one bound per component. Every candidate-pair solid intersection remains. The nine mechanical tests complete in about 30 seconds.

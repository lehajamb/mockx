# PPS SMA footprint correction and remaining connector checks

**NOT READY FOR FABRICATION.** The copper land correction is validated independently; finished board thickness and the complete connector assembly remain release gates.

## Reproduced land-pattern discrepancy

J5 uses Amphenol 132289, a PCB end-launch bulkhead SMA jack. The pinned KiCad footprint used five 1.50 x 5.08 mm pads reaching the board edge. The manufacturer-authored drawing mirrored by Farnell specifies a 1.50 x 5.20 mm signal land, four 1.60 x 4.90 mm ground lands, 4.25 mm lateral center offsets, and 0.30 mm steps from the board edge to the signal and then to the ground lands. The common rear land edge is 5.50 mm inward. The drawing's revision table ends at B (2011-11-30), while its title block still reads A; that inconsistency is retained as evidence. It is not represented as the latest 2026 drawing.

The [original PDF and provenance](../mech/vendor/amphenol/manifest.json) preserve the downloaded bytes and source. The [official product page](https://www.amphenolrf.com/en-us/part/132289/1030/) links a newer customer drawing, but that direct PDF download returned HTTP 403. Its 3D model requires account login. Neither was substituted with an invented asset.

`gen_sma.py` creates `sorx:SMA_Amphenol_132289_Drawing`. At the existing J5 origin (100,2.54) and 90-degree rotation, the signal center becomes (100,2.90), ground centers become (95.75,3.05)/(104.25,3.05), and all pad ends are at Y=5.50 mm. Pin numbering, net assignments, connector location and reference-label placement are retained. The helper changes no tracks or vias. An independently written dimension test failed on all five original pads and passes on the corrected land pattern.

The accepted isolated [native proof](../validation/sma-proof-05/result.json) uses identical absolute custom-library configuration on both sides: required opens remain 54, while other DRC findings fall from 107 to 102. All five J5 copper-edge findings disappear; no new non-open finding is introduced. The exact executed proof script is retained with the hash from that report. Later gate hardening also rejects an increase in required opens; its failing-before and passing-after logs are retained. This proof does not certify a later board unless the helper is reapplied and fresh checks pass.

Earlier trials remain failure evidence. A native `SetPosition` without updating the pad's local coordinate did not survive a save/reload; the fresh native report caught it. A direct in-place edit also produced a library-mismatch warning. The accepted implementation loads the exact generated footprint, preserves board reference fields, assigns the original nets and validates persisted local coordinates in a fresh process. No warning waiver is used.

## Thickness and assembly gates

The official product page lists maximum PCB thickness 1.57 mm, and the historical drawing specifies 1.57 mm nominal. The current proposed six-layer stack totals 1.60 mm. These requirements are **not reconciled**. Before fabrication, obtain an exact finished-thickness/tolerance prescription compatible with J5 and the other mechanical interfaces, then update the native stack, mechanical PCB thickness and all relevant impedance/geometry calculations together. Do not assume that a nominal 1.60 mm board fits because the difference is small.

The drawing also specifies a 6.60 mm panel mounting hole and a maximum 3.5 mm panel thickness. Current enclosure alignment checks use a simplified connector envelope and do not qualify the full body, bulkhead nut/washer, wrench access or mating-cable envelope. The final drawing/model and tolerance review must resolve those items. No physical insertion or torque test is claimed.

## Other exact-object DRC reviews still required

- H5-H8 each report one NPTH-to-annular-land clearance and one solder-mask bridge. The Wurth drawing explicitly recommends a 3.0 mm NPTH inside a 5.3 mm solder land; these are intentional mechanical lands, not shorts between required electrical nets. Retain the raw findings until the fabricator/assembler accepts that exact construction. See [the 7 mm standoff drawing](https://www.we-online.com/components/products/datasheet/9774070243R.pdf) and the matching 2.5 mm drawing in the mechanical review.
- J3 and J10 have eight repeated GND-pad-to-location-hole findings at 0.1944 mm versus the project's 0.2000 mm rule. The exact GCT USB4105 land pattern, positional tolerance and manufacturing capability need review; the small difference is not an automatic waiver.
- J12 has two shell-land copper-edge clearances of 0.075 mm versus 0.250 mm. Its location, card insertion/removal access and any pad/edge change require a coordinated electrical/mechanical correction.
- J1/standoff courtyard overlaps need exact-object assembly review against the supplier CAD and required insertion motion. Passing rigid static fit alone is not insertion-access proof. The separate TP4/R4 courtyard overlap also remains to resolve.
- Silkscreen edge, copper and label overlaps remain manufacturing presentation defects. They are retained in native reports; no category-wide suppression is applied.

`validation/drc-waivers.json` remains empty. These observations classify the remaining work; they do not close the corresponding fabrication gates.

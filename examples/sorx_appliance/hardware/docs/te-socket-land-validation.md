# Exact TE socket land-pattern review

The Codex land pattern was checked against TE customer drawing 2309409, revision 6, sheet 2, downloaded from the official 2309409-2 product page on 2026-11-17. The retained PDF SHA-256 is `8b3221725a67a7f52fc4edb8b1f44856ddddcbec6a9f0c614205b32ca5f4101e`; see `mech/vendor/te/manifest.json`. This drawing independently confirms the previously used Toradex Figure 114 dimensions.

The drawing's recommended PCB view puts pin 1 at the right end, with the module extending toward the hold-down lands. The custom footprint uses the same orientation, module toward +Y, peg midpoint (-0.0055, -7.926) mm. Translation of the coordinate origin does not alter the dimensions below.

| Feature | TE drawing | Custom land pattern | Result |
|---|---|---|---|
| Peg center distance | 68.80 +/-0.05 mm | 68.80 mm | Nominal agrees |
| Pin-1-side locating hole | diameter 1.60 +/-0.05 mm | diameter 1.60 mm | Nominal agrees |
| Opposite locating hole | diameter 1.10 +/-0.05 mm | diameter 1.10 mm | Nominal agrees |
| Signal pitch within each bank | 0.50 mm | 0.50 mm | Agrees |
| Signal land width / length | 0.30 +/-0.03 / 1.75 +/-0.03 mm | 0.30 / 1.75 mm | Nominal agrees |
| Row centers from peg axis | +/-4.10 mm | +/-4.10 mm | Agrees |
| Pin 1 / pin 2 centers inward from right peg | 1.025 / 1.275 mm | 1.025 / 1.275 mm | Agrees |
| Contact banks, center spans | 35.50 and 28.50 mm | 71 x0.5 and 57 x0.5 mm | Agrees |
| Key gap between nearest contacts | 2.50 mm | 2.50 mm | Agrees |
| Hold-down center separation | 67.60 mm | 67.60 mm | Agrees |
| Hold-down center from peg axis | 12.00 mm | 12.00 mm | Agrees |
| Each hold-down land | 3.50 +/-0.10 x4.60 +/-0.10 mm | 3.50 x4.60 mm | Nominal agrees |

No electrical-land change is indicated by the exact drawing. The two locating holes are intentionally asymmetric; do not replace them with the historical two oversized holes. They are non-plated. The board fabricator must acknowledge the finished-hole and copper-registration tolerances on the supplier drawing; nominal KiCad dimensions alone do not establish process capability. The manufacturer STEP, body clearances, latch access, board thickness and module/heatsink mounting are assessed separately. Historical rectangular Fab/courtyard envelopes are approximate and must not overrule the supplier solid.

Source: [TE 2309409-2 official product page](https://www.te.com/en/product-2309409-2.html), customer drawing 2309409 revision 6 sheet 2; local retained copy `mech/vendor/te/2309409-drawing.pdf`.

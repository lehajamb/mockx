# Critical sourcing review - 2026-11-17

**NOT READY FOR FABRICATION. R09 remains open.** This is a targeted review of
changed or critical parts, not a complete orderable BOM. No order was placed.
The imported stock notes and generated historical cost rollup remain unchanged.
Machine-readable observations, access methods, timestamps and limitations are
in `validation/sourcing-20261117/observations.json`.

| Reference | Exact selected part | Evidence and remaining sourcing work |
|---|---|---|
| J1 | TE 2309409-2 | Root observed 20,790 available for immediate shipment and USD 2.22 at quantity 1 in the live US public manufacturer UI on 2026-11-17. Exact UTC was not captured. This is attributed root evidence, not an electrical-agent repeat. [TE product page](https://www.te.com/en/product-2309409-2.html) |
| U1 | Infineon CY15V104QN-50SXI | Live manufacturer page confirms the selected industrial SOIC and active/preferred status. Its EMEA inventory dialog displayed Arrow 200, Mouser 37 and Rochester 206 during 01:42–01:46 UTC. Rochester has an explicit reseller-warranty caveat. These are manufacturer-hosted distributor listings, not separately confirmed allocations. [Infineon product page](https://www.infineon.com/part/CY15V104QN-50SXI) |
| U38 | ADI LT3010EMS8E#PBF | Exact adjustable MSOP-EP model is listed; family is recommended for new designs. Current exact-SKU stock and prototype price are unknown. The fixed-output `-5` part is not interchangeable in this circuit. [ADI product page](https://www.analog.com/en/products/lt3010.html) |
| U39 | TI TL431BIPK | Active SOT-89 PK, industrial −40 to 85 °C. Live public page displayed USD 0.306 for 1–99, but inventory remained unavailable and a human-verification panel prevented further inspection. Price is not a quote; stock is unknown. [TI exact-part page](https://www.ti.com/product/TL431/part-details/TL431BIPK) |
| Q5 | Nexperia PMV65XP,215 | Manufacturer family is in production; exact `,215` carrier stock is unknown. [Nexperia product page](https://www.nexperia.com/product/PMV65XP) |
| U40 and existing U28 | TI SN74LVC1G08DBVR | Exact 5-pin SOT-23 DBV part is active. Current stock and price are unknown; extracted login/unavailable labels cannot establish global stock. DCKR/SC70 and another manufacturer's similar name are not approved substitutions. [TI exact-part page](https://www.ti.com/product/SN74LVC1G08/part-details/SN74LVC1G08DBVR) |
| H5/H6 | Wurth 9774025243R | Exact 2.5 mm standoff drawing validated; current quantity/price not checked. [Manufacturer drawing](https://www.we-online.com/components/products/datasheet/9774025243R.pdf) |
| H7/H8 | Wurth 9774070243R | Exact 7 mm standoff drawing validated; current quantity/price not checked. [Manufacturer drawing](https://www.we-online.com/components/products/datasheet/9774070243R.pdf) |
| HS1 | Toradex 23111101 | Manufacturer revision table identifies Type 1 V1.1B, Q3 2026, with revised TIM resolving HAR-13572. Actual allocation of this revision is unknown; generic Type 1 listings mix revisions. [Toradex revision table](https://developer.toradex.com/hardware/verdin-som-family/add-ons/verdin-industrial-heatsink/) |

The live Infineon page did **not** show the direct-stock quantity 2 and USD 20.60
price present in cached search content dated 2026-10-12. Those cached figures
were excluded from current stock evidence. The TE cached extraction likewise
conflicted with the live page; only the dated live observation is recorded.

Service headers and shunts now have exact selected manufacturer parts:
JP1 uses [61300311121](https://www.we-online.com/components/products/datasheet/61300311121.pdf),
JP2/J14 use [61300211121](https://www.we-online.com/components/products/datasheet/61300211121.pdf),
and separate BOM items SH1/SH2 use [60900213421](https://www.we-online.com/components/products/datasheet/60900213421.pdf).
The generator `tools/gen_service_headers.py` produces the two custom lands.
Existing pin centers, 2.54 mm pitch and 1.7 mm copper pads are preserved.
The drawings recommend 1.10 ± 0.15 mm holes and specify square pins 0.64 ± 0.15 mm.
The selected **finished hole is 1.20 ± 0.05 mm**, within that recommended interval;
its 1.15 mm minimum exceeds the worst pin diagonal 1.1173 mm. Specify finished-hole
tolerance to the fabricator, not an ambiguous tool diameter. This check does not
replace approval of accumulated hole/pin positional tolerances and insertion.

The conservative bare-header maximum height is 8.94 mm from total 11.54 ± 0.2 mm
minus board-side tail 3.0 ± 0.2 mm. Reserve 9.5 mm for the installed shunt and qualify
actual seating. The headers/shunts are 3 A rated, comfortably above the sense and
ID strap allocation, and their 25 mating-cycle rating suits occasional service;
it does not qualify them as a repetitive production test connector. Drawing
hashes, visual-review evidence and remaining fit limits are recorded in
`validation/sourcing-20261117/service-header-selection.json`. Stock is unknown.
The fixture mating connector/cable remain separate procurement items. Recovery
must preserve host-PC VBUS isolation and use the documented shunt positions.

The four heatsink screws require exact head/material/engagement qualification.
Toradex describes included M2 × 10 hardware, but a nominal size is insufficient to
replace the exact supplier assembly and tolerance review. New passives have
model MPNs, but their availability was not newly verified. A full procurement
pass must cover every fitted line, prototype quantities, minimum order sizes,
shipping region, current lead times and approved alternates. Intel NVM licensing
and board-specific PD programming assets are separate unresolved requirements.

# USB3 copper correction: accepted intermediate checkpoint

**NOT READY FOR FABRICATION.** This change closes a specific USB3 topology and planar-matching defect. It does not close R04 or the complete release criteria.

## Fixed source and accepted result

- Input: `runs/route-electrical-09-branches.kicad_pcb`, SHA-256 `e59b8ff8c62ef4b0a7e5d2aacf1f91423f1c1674f42faf68698a1e30af80653c`.
- Accepted intermediate output: `runs/route-electrical-10-usb3.kicad_pcb`, SHA-256 `122ac32215ebd9788de9dec640cb5011972145cf89a5acd8780efb3a2a3efd16`.
- Raw endpoint audits, native reports and exact comparison: `validation/high-speed/usb3-combined-11/`.
- The source-import benchmark and original baseline remain unchanged. This checkpoint is subsequently consumed by power, placement and ground repairs; rerun the audit on the final board.

## Engineering changes

U19 moves from (94.12,46.15) to (66.00,96.00) mm, rotation 90 degrees, front side, near J8. Four manufacturer-permitted straight-through NC pads now carry copper in the actual source-to-destination paths: 1/10 TX_P, 2/9 TX_N, 4/7 RX_P, 5/6 RX_N; 3/8 remain GND. The electrical model uses the explicit passive flow-through symbol. TI TPD4E05U06 RevO Table4-2 permits those straight-through copper connections; this is not an arbitrary NC waiver. Source: <https://www.ti.com/lit/ds/symlink/tpd4e05u06.pdf>.

The obsolete long ESD branches and their unused vias are removed. Both paths in each pair have two through-via transitions. TX uses F/B/F; RX uses F/In3/F. Long trunks use 0.155mm width and 0.400mm center spacing (0.245mm copper gap). A 45-degree RX_P detour adds the needed planar length in the port approach; its uncoupled span remains visible in the audit. Existing connector fanout geometry is retained. Both U19 GND pads receive fresh connected copper/via access.

The source and candidate retain the same logical P/N endpoint assignments. No pair inversion or required functionality removal is used.

## Measured comparison

These values compare the immediately preceding combined checkpoint, using the same endpoint audit convention including explicitly labeled radial via-copper estimates. Earlier baseline reports used an older exact-center convention and have slightly different numerical estimates; they remain historical evidence.

| Measure | Before | After |
|---|---:|---:|
| TX endpoint planar skew |0.863710mm|0.024699mm|
| RX endpoint planar skew |0.422756mm|0.090610mm|
| TX off-path copper P/N |28.095/27.020mm|0/0mm|
| RX off-path copper P/N |30.086/30.573mm|0/0mm|
| TX sampled unpaired path P/N |51.328/52.224mm|11.895/11.332mm|
| RX sampled unpaired path P/N |49.747/50.170mm|22.701/21.871mm|
| USB3 required opens |4|0|
| Whole-board native required opens |72|61|

The adopted 0.150mm planar USB3 target passes for both pairs. The sampled coupling result improves substantially, but the remaining escape and tuning portions require engineering review. The full-board native difference is GND opens−9, USB3 opens−4 and intentionally displaced +1V8 opens+2. All other net open counts are unchanged. Native clearance, hole, courtyard and other category counts are unchanged; two new warnings are the expected dangling +1V8 endpoints. U19's existing unresolved-library warning moves with the component. Native report object-pair choices can change when islands merge, so the JSON retains both the raw item difference and per-category counts.

## Required subsequent repair

Three specific +1V8 front-layer segments obstructed the short port fanout and were removed, with their complete serialized records retained in `trial-00.removed-copper.json`:

- `0725c8e0-d079-49bf-8822-283b3a84f6a6`
- `131fd0e9-dba6-4720-94ac-dd5874184433`
- `cb7209b7-9238-43b4-83a0-621ca612fcbb`

A fresh power-island audit and legal replacement routing must close those two added opens before this intermediate checkpoint can participate in a fabrication candidate. The accepted source is never overwritten; downstream repairs produce new checkpoints.

## Limits that remain release blockers

The six-layer proposal has 0.100mm outer reference dielectric spacing and 0.250mm In3-to-In4 spacing. **The same 0.155mm copper width does not establish 90 ohm differential impedance on both layers.** In3 also faces In2 power. A fabricator/material-specific field calculation must determine per-layer width, gap, delay and loss; any resulting geometry or stackup change requires new routing/DRC/reference validation.

The audit reports all projected GND interruptions, including via antipads, rather than applying blanket exemptions. Their total unreferenced projected lengths after the patch are approximately TX 1.937/2.077mm and RX 2.009/1.795mm per conductor. Return-via location, connected reference planes and local escape discontinuities still need object-specific review. Planar skew excludes qualified barrel/stub and package delay; radial via-copper length is explicitly a geometric estimate. There is no eye-diagram or hardware measurement claim.

Other interfaces, OE MDI routing, PCIe mounting-area opens, PD/power checks, mechanical qualifications and physical bring-up remain separate gates. Exploratory MDI trials were rejected because they do not provide an acceptable complete pair redesign; they are not merged into this checkpoint.

## Reproduction

In the pinned Codex hardware image, from `hardware/`:

```sh
/usr/bin/python3 tools/correct_high_speed.py runs/route-electrical-09-branches.kicad_pcb runs/new-usb3-checkpoint.kicad_pcb --evidence validation/high-speed/new-usb3-run
/usr/bin/python3 -m unittest discover -s tests -p test_high_speed_audit.py -v
/usr/bin/python3 -m unittest discover -s tests -p test_usb_candidate_checks.py -v
```

Use new output and evidence paths; the helper rejects overwriting its source or an existing candidate. Each worker is a fresh process. Exact serialized routing records are filtered before KiCad loads the board, avoiding the observed KiCad7 Python `Remove()`/SWIG registry defect. A strict candidate gate rejects missing paths, graph errors, stubs, extra/asymmetric vias, length failures and native USB/U19 DRC errors. Its success only advances this intermediate correction; `fabrication_ready` is always false.

## Replay and retained evidence

The final hardened helper was replayed from the same input. Both runs have the same normalized routing/placement/pad-net geometry digest `5f6a8ad9f42cd2cabb923557a6e9b08447048aec95ed2444025c18cfc4d722c5`. Fresh KiCad UUIDs make the complete PCB file hashes differ; the normalization explicitly excludes UUIDs and zone-fill serialization. Raw native and reference reports remain separate checks. See `validation/high-speed/replay-comparison.json` and `usb3-replay-13/`. All 15 targeted high-speed tests pass in the pinned image; commands, versions and source hashes are in `all-targeted-tests.json`.

Rejected candidate folders and large trial PCB/project/UI files were moved to ignored `runs/high-speed-experiments/`; no rejected PCB is promoted. `experiment-index.json` records outcomes and `scratch-storage.json` records the relocation. The accepted source and downstream checkpoint paths remain unchanged.

The candidate gate now uses the shared complete native DRC parser, requires all three counted sections and the report footer, rejects unknown finding severity, and treats any required USB open as failure. Off-path measurements must be finite, nonnegative real numbers; required flags must be literal booleans. Empty/truncated reports, NaN, negative values and truthy strings were reproduced as failing regressions before the gate was corrected. Both retained accepted reports pass the hardened gate.

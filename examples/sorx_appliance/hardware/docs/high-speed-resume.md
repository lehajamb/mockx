# High-speed safe-wrap resume state

**NOT READY FOR FABRICATION.** Engineering paused at the user's request on 2026-11-17. No new routing or net-class changes were started after that request. The following paths are relative to `hardware/`. Preserve original proof files and use fresh output/evidence paths for every replay. Root owns the final integrated checkpoint; these notes identify this agent's accepted and unpromoted work, not the current authoritative PCB.

## Accepted integrated PCIe stage

- `runs/route-electrical-14-pcie.kicad_pcb`
- SHA-256: `be0e7b2efc6d55d5b02c0e0923cbbab927f18c36e189ecd4d92fb11a7ceba647`
- Source: accepted electrical 13c. Native opens 54 -> 50; other findings 113 -> 107; zero new non-open findings or per-net open increases.
- TX planar mismatch: 0.032426 mm; clock: 0.047993 mm. Symmetric two-via F/In3/B paths; no off-path tracks. Impedance, coupling/reference discontinuities and full timing remain unqualified.
- `tools/correct_pcie.py`, `docs/pcie-correction.md`, and `validation/high-speed/pcie-11/` retain the implementation and evidence. Verification integrated this copper into 15b, and electrical retained it through 17 and the later power bridge.

## Accepted isolated RX proof, NOT integrated

- Source: `runs/electrical-final17/route-electrical-17-sma.kicad_pcb`, SHA-256 `825e00ab492409f0c4665e1710710c23160ca38cbcf83e9748ebc51d6f933f0d`.
- Output: `runs/rx-trial-01.kicad_pcb`, SHA-256 `4e08c2cedee7f731aa5e918245257377e85e3336fe90106e1885ad83e14501d8`.
- Native 35 opens / 95 other findings before and after, zero new findings. RX planar mismatch 0.919690 -> 0.019690 mm by adding 0.900 mm to one existing uncoupled N segment. Single-via B/F topology retained.
- This improves matching while increasing sampled RX_N unpaired length 15.406505 -> 16.306505 mm and projected missing-reference length 0.897197 -> 0.941150 mm. It is not a complete high-speed qualification.
- Stable new files: `tools/correct_pcie_rx.py`, `tests/test_pcie_rx_candidate_checks.py`, `docs/pcie-rx-correction.md`; shared gate changes: `tools/pcie_candidate_checks.py`. Raw evidence: `validation/high-speed/rx-01/`.
- Current helper SHA-256: `33353143e4dbc2c073d39e5d1a0836d1f79d0ed6aedc8db61a2e0154d786857e`; current shared gate SHA-256: `ca965a3f18bb32569ffa16b3196302f769738a6eb8f146d342b5a380ece30dbd`.
- Raw generation predates later gate-only hardening. Its recorded generation tool hash is therefore historical, not the current helper hash. Nineteen host candidate-gate tests passed before the pause (7 USB, 7 PCIe TX/clock, 5 RX). No new tests were started during safe wrap. Re-run strict gates to bind their final current hash; existing `strict-gate.json` can predate the last malformed-pair-list regression. Replaying the helper onto the next accepted integrated checkpoint and running its native comparison is still required before promotion.

## Rejected / unfinished routing work

- `tools/correct_rgmii.py` and `tools/correct_mdi.py` are experimental prototypes, not accepted production repair tools. Preserve them as WIP. Their partial diagnostic PCBs must never replace an accepted board.
- RGMII trials `validation/high-speed/rgmii-01/` through `rgmii-09/` failed endpoint escapes, trunk access or restoration of neighboring geometry. No complete MG_TXD0 route was accepted. Some trials temporarily remove the TXD1 route or supply segments in isolated filtered scratch boards; these are not valid functional candidates.
- MDI trials `validation/high-speed/mdi-paired-01/` and `mdi-paired-02/` did not produce an accepted complete correction. Trial 02 finds six PHY escapes but fails the OE_MDI3_N internal-layer endpoint connection. No MDI correction was promoted. Existing OE_MDI1/3 planar mismatches remain approximately 4.364641 / 10.756538 mm.
- Earlier failed PCIe trials are preserved under `runs/high-speed-experiments/`, indexed by `validation/high-speed/pcie-experiment-index.json`.
- `tools/rgmii_audit.py` is still WIP: it measures endpoint planar paths, but the RX data/CTL matching screen must exclude RXC and report clock delay separately. Do not interpret its existing aggregate as full MAC-to-PHY timing qualification.
- Existing TX lengths in mm: TXC 19.692510; TXD0 missing; TXD1 23.862699; TXD2 18.498596; TXD3 17.383161; TX_CTL 19.056003. Existing TX spread is already 6.479538 mm. RX data/CTL spread is approximately 27.203101 mm. A local connectivity repair alone does not satisfy the Microchip 50 mil (1.27 mm) carrier matching screen or the full module-plus-carrier timing budget.

## Next bounded TXD0 hypothesis, not validation

Electrical's `validation/phy-txd0-survey.json` identifies a possible U13.19 escape while preserving TXD1: remove only MG_LED1 tracks/vias and these two +1V2_PHY segments in an isolated copy:

- `587b360f-ca17-4750-964c-23ae3a9b430d`
- `48ed1cdd-c2d8-4309-b877-92fcf9a5d823`

A direct 0.15 mm trace from U13.19 `(67.1625,39.75)` to a 0.60/0.30 mm via at `(65.50,39.20)` is 1.7511 mm long. **Corrected survey thresholds:** the trace used 0.235 mm centerline-to-obstacle distance, equivalent to 0.160 mm copper clearance; the via screen used 0.185 mm copper clearance. An earlier message incorrectly described the trace threshold as 0.185 mm. Native DRC and restoration of both removed nets remain mandatory.

A previously demonstrated module escape near `(54.15,50.3)` can reach In3, but PWR_FLT# forms a barrier. A bounded trial may reroute one exact ordinary segment, e.g. UUID `0f001df1-e224-40c0-8505-635f74d3e5a8`, `(63.8,43.4)` to `(60.6,43.4)` on In3, after verifying it still matches the accepted source. Restore PWR_FLT#, MG_LED1 and the 0.25 mm PHY supply branch before accepting anything. Electrical permits that local branch detour with a 0.30 A planning envelope, preserving all decoupler endpoints; this is not a manufacturer maximum-current rating.

U13.4 is a separate required +1V2_PHY open. The read-only survey `validation/phy-supply-survey.json` found no untouched-copper via escape with 0.25 mm trace / 0.60 mm via / 0.30 mm drill and conservative clearance. A neighboring MDI fanout change is required. Do not narrow a generic trace or mark the pin unconnected to hide it.

Coordinate sequential PCB ownership with verification/electrical. At this agent's pause, verification owned ordinary/GND integration; the last known accepted source was `runs/electrical-final17/power-bridge-proof01.kicad_pcb`, SHA-256 `5188991db0633f32b6552605679a701606510d5833916cdc843b0ec5413cead1`, 30 opens / 93 other findings. Consult the root pause manifest for any later accepted checkpoint.

## Proposed net-class reconciliation (NOT implemented)

The Intel I210/I211 FAQ revision 1.0, section 2.28 recommends 85 ohm PCIe routing. The older Toradex layout guide Table 5 gives 90 ohm +/-15% differential and 50 ohm +/-15% single-ended. This requires explicit interface reconciliation; a `DP90` name or ordinary clearance pass cannot qualify the implemented geometry.

Proposed source changes for a resumed engineering pass:

1. Separate `PCIE_DATA` patterns `PCIE_TX_?`, `PCIE_RX_?`, `OE_PET_?` from `PCIE_REFCLK` pattern `PCIE_CLK_?`.
2. Record 85 ohm data intent from Intel. Keep reference-clock impedance unresolved until its module-source/termination requirements are inspected; do not invent a 100 ohm requirement.
3. If defaults are changed, retain explicit provisional clearance 0.18 mm, track/pair width 0.155 mm, gap 0.245 mm and via/drill 0.60/0.30 mm to reflect the new trunk geometry. These are geometric defaults, not a field-solved prescription; do not mutate existing copper or minimum fabrication rules implicitly.
4. Update route-policy protection together with any class rename. Current policy protects DP90/DP100 families; an isolated rename must not make PCIe eligible for ordinary rerouting.
5. Keep project and generator class definitions consistent. USB remains separately 90 ohm intent; MDI remains 100 ohm intent. In3-to-In4 spacing is 0.25 mm versus 0.10 mm at outer references, so identical width/gap cannot be assumed to produce identical impedance.

Primary sources:

- [Intel I210/I211 FAQ, section 2.28](https://cdrdv2-public.intel.com/334026/ethernet-controller-i210-i211-faq.pdf)
- [Toradex Layout Design Guide, Table 5](https://docs.toradex.com/102492-layout-design-guide.pdf)
- [Microchip KSZ9131RNX reference design, page 4](https://ww1.microchip.com/downloads/aemDocuments/documents/UNG/ProductDocuments/SupportingCollateral/KSZ9131RNXReferenceDesign.pdf)

## Process state

No owned background session or container remains running. Read-only `docker ps` during safe wrap listed no running containers. No source checkout, PCB, class definitions or copper was changed during safe wrap; only this resume document was written. Root will archive evidence and commit WIP. Use KiCad 7.0.11 in the pinned Codex image with `KICAD7_FOOTPRINT_DIR=/usr/share/kicad/footprints`; mount the dedicated hardware tree at `/work` and its same tree read-only at `/hardware` to resolve recorded local footprint tables. Never mount Claude's checkout. Never use KiCad API `Remove()` followed by further in-process edits; serialize exact UUID removals and load in a fresh worker to avoid the observed SWIG metadata corruption.

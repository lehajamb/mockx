# PCIe RX planar matching correction

**NOT READY FOR FABRICATION.** This is a measured local matching improvement, not a qualification of differential coupling, impedance or PCIe timing.

The independently executed proof uses `runs/electrical-final17/route-electrical-17-sma.kicad_pcb` (SHA-256 `825e00ab492409f0c4665e1710710c23160ca38cbcf83e9748ebc51d6f933f0d`). Its isolated result is `runs/rx-trial-01.kicad_pcb` (SHA-256 `4e08c2cedee7f731aa5e918245257377e85e3336fe90106e1885ad83e14501d8`). It must be replayed onto the current accepted chain before promotion. Raw reports and the hardened gate result are in `validation/high-speed/rx-01/`.

Only front-layer RX_N segment `0c7c585c-0401-463e-9193-db956eb27f3c` is replaced. Its endpoints (48.8517,56.2713) and (45.5690,56.2713) mm remain fixed. A 45-degree detour adds 0.900 mm centerline length, with0.350 mm end guards and0.155 mm trace width. The tool binds the exact original UUID, endpoints, net, layer and width; checks foreign copper/drill clearance before changing anything; and filters the serialized segment before a fresh KiCad worker loads it.

| Measure | Before | After |
|---|---:|---:|
| RX P endpoint planar length |26.330850mm|26.330850mm|
| RX N endpoint planar length |25.411160mm|26.311160mm|
| Endpoint planar mismatch |0.919690mm|0.019690mm|
| Off-path track length P/N |0/0mm|0/0mm|
| Via transitions P/N |1/1|1/1|
| Native required opens |35|35|
| Other native findings |95|95|
| New native findings |—|0|

Both AC capacitors, logical endpoints, the positive conductor and all other net copper remain unchanged. Both source and output resolve the same local footprint table through a read-only `/hardware` alias while the dedicated work mount is `/work`; this is necessary for an honest native comparison.

The existing tuning region was already uncoupled. The detour increases RX_N's sampled unpaired length from15.406505 to16.306505mm; RX_P remains17.319287mm. Projected missing-ground length changes from0.897197 to0.941150mm on N, with P unchanged at1.191445mm. These quantities are explicitly retained for review. Reducing planar mismatch alone does not close those discontinuities, qualify via/pad propagation delay, or demonstrate an eye diagram. A broader paired route redesign and material-specific impedance/timing review may supersede this local correction.

Run in the pinned Codex image with KiCad7.0.11 and `KICAD7_FOOTPRINT_DIR=/usr/share/kicad/footprints`:

```sh
/usr/bin/python3 tools/correct_pcie_rx.py runs/electrical-final17/route-electrical-17-sma.kicad_pcb runs/new-rx.kicad_pcb --evidence validation/high-speed/new-rx
/usr/bin/python3 -m unittest discover -s tests -p '*candidate_checks.py'
```

Choose fresh paths. The strict RX gate requires the single-via B/F topology, complete native reports, actual connected endpoint paths, finite off-path lengths, passing explicit geometry flags and no newly required open on any net. Five new RX regressions and the 14 existing USB/TX/clock gate tests pass. The retained accepted proof and TX/clock proof both pass the hardened shared gate; `strict-gate.json` binds its exact source hash. Full board release and physical tests remain mandatory.

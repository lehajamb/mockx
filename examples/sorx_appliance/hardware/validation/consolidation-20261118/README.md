# Consolidation check — not ready for fabrication

Claude's handoff and Rev B step 04 are committed on `codex/sorx-appliance-bootstrap` at `f4dfc88e6b89b8a6cb36d5b29df92be87bb91e41`. They were not merged into the six-layer circuit. In the primary checkout, `codex/sorx-appliance` was created at local main `ef5715c` and fast-forwarded through all 19 independent commits to `94c7f2b58e9bb5608879bf14a2d8f805cbc34174`. The import tree for the 994 tracked appliance files was `de68b4332b578df512b37611fe3cdbd025d6338c`. The [separate transfer receipt](../../../docs/local-transfer.json) identifies the additional portable helper sources and the optional, ignored local files.

The native board SHA-256 remained `5188991db0633f32b6552605679a701606510d5833916cdc843b0ec5413cead1`. New [stored](drc-stored.txt) and [refilled](drc-refilled.txt) KiCad reports from the primary checkout each show **30 required opens and 93 other findings**. Both retain the same counts by net, including 16 ground opens, and the same 93 non-open findings as the native-promotion reports. KiCad picked a different endpoint on the same USB1_OC# net for one *stored* open compared to the archived report. A report line for a still-open net is not a stable pad-group ID.

[The model check](board.json) found 371 footprints, 1,588 pads, 9,362 track segments, 1,258 vias, no pad/model discrepancies and no duplicate via sites. [The provenance check](bootstrap.json) verified three original files, 161 imported files and 16 reference images. The historical fabrication files do not qualify the current PCB. No circuit or manufacturing artifacts were regenerated.

## Reproduction

From the primary repository root with Docker Desktop and the [pinned KiCad image](../../tools/env/README.md) installed:

```powershell
python examples/sorx_appliance/tools/validate_bootstrap.py
python examples/sorx_appliance/tools/routing_status.py
$run = './examples/sorx_appliance/hardware/tools/env/run.ps1'
$name = 'consolidation-check-' + [guid]::NewGuid().ToString('N')
New-Item -ItemType Directory "examples/sorx_appliance/hardware/runs/$name" -Force | Out-Null
$out = "/work/hardware/runs/$name"
& $run -ToolArgs @('python3','verification_worker.py','board','/work/hardware/hw/sorx.kicad_pcb',"$out/board.json",'--drc',"$out/drc-stored.txt")
if ($LASTEXITCODE -ne 0) { throw 'KiCad worker failed' }
& $run -ToolArgs @('python3','-m','unittest','discover','-s','/work/hardware/tests','-v')
if ($LASTEXITCODE -ne 0) { throw 'Hardware tests failed' }
foreach ($suite in @('test_mechanical.py','test_vendor_geometry.py')) {
    & $run -ToolArgs @('/opt/cq/bin/python','-m','unittest','discover','-s','/work/hardware/tests','-p',$suite,'-v')
    if ($LASTEXITCODE -ne 0) { throw "$suite failed" }
}
```

The worker's successful exit means it produced the reports, **not** that their findings passed. The dedicated CAD interpreter exercised the two groups skipped by system Python. MockX was tested with the repository's Conda initialization, `conda activate mockx`, `PYTHONPATH` set to this checkout's `src`, and `python -m pytest -q`. See the [routing runbook](../../../docs/routing-runbook.md) for the distinct procedure that accepts or rejects a proposed connection.

## Results with the final routing checker

| Check | Result |
|---|---|
| [MockX and workflow regressions](mockx-final.log) | 1,132 passed, 16 skipped; exit 0. |
| [Hardware suite in pinned KiCad](hardware-final.log) | 144 tests, two CAD groups skipped; exit 0. [Mechanical](cad-mechanical.log) and [supplier geometry](cad-vendor.log) ran separately in CadQuery: 9 and 6 passed. |
| [Real historical candidates](integration-checks.json) | The power bridge passed the connectivity gate (33 → 30 opens); the candidate that split C64.2 from ground was rejected with exit 1 despite 36 → 31 opens. Neither board was promoted. |
| [Portable handoff](portable.log) | Exported only indexed appliance paths to a temporary directory, with no Git metadata or ignored trial PCBs. The status command, the native project/ledger binding and all seven gate regression tests passed there. |

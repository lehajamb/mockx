# Bootstrap validation record

**NOT READY FOR FABRICATION.** This record validates the import and MockX
repository integration only. No hardware design defect was corrected here.

Date: 2026-11-16. Host: Windows / PowerShell. Source import revision:
`df8247829ee12cb5b1c03e0d8fa1c29383a95f74`, based on
`ef5715c3763b98b1f522a865df3beaac2625ffcd`.

## Executed checks

| Check | Result |
|---|---|
| Supplied ZIPs and external handoff | All three source SHA-256 hashes match [the manifest](../baseline/manifest.json) |
| Archive structure | All members inventoried; safe relative paths, distinct case-insensitive paths, expected roots, no symlinks; ZIP CRCs and member SHA-256 verified |
| Imported working/reference files | 161 byte-identical files; source relationships, checkpoints and supplied exports retained |
| Git import identity | All 167 files in the source import commit match working-file Git blob hashes with filters disabled |
| Exclusions | 18 entries excluded, including a cache directory; only Python bytecode/cache and KiCad personal UI state |
| Python syntax | All 22 imported Python sources parsed with `ast.parse`; no imported generator or verifier executed |
| Image provenance | All 16 bundle files cataloged and hashed, including its README; nine files duplicate hardware outputs by SHA-256 |
| New documentation links | 90 local link targets checked across 10 authored Markdown files; preserved historical documents are not rewritten |
| Repository context graph | `trail build` passed; local ignored graph includes the imported hardware tools and bootstrap validator |
| Repository regression suite | **1,032 passed, 1 skipped**, 60.04 seconds, exit status 0 |

The reusable provenance command was executed using Python **3.14.7**:

```powershell
C:/Python314/python.exe examples/sorx_appliance/tools/validate_bootstrap.py --pristine
```

Its result:

```json
{
  "check": "bootstrap provenance and Python syntax only",
  "fabrication_approval": false,
  "pristine": true,
  "sources_verified": 3,
  "imported_files_verified": 161,
  "excluded_entries": 18,
  "python_files_parsed": 22,
  "reference_image_files": 16,
  "changed_hardware_files": []
}
```

The repository tests were executed in the existing `mockx` Conda environment:

```powershell
. "C:\Users\lehaj\anaconda3\shell\condabin\conda-hook.ps1"
conda activate mockx
python -m pytest -q
```

The first sandboxed activation failed with an “Illegal System DLL Relocation”
error. The same activation/test command was rerun outside that restriction and
passed. No package installation or MockX behavior change was needed.

## Hardware checks not executed

`kicad-cli`, FreeCAD and OpenSCAD were not found on the checked host PATH.
No hardware-tool installation was attempted. CadQuery and other imported tool
dependencies were not exercised, and the original Linux environment was not
recreated. This does not establish that every possible installation location
was searched.

Fresh KiCad ERC/DRC and zone refill, CAD-kernel interference checks, high-speed
electrical simulation, PD/NVM programming, sourcing refresh, export regeneration
and physical bring-up were **not run**. Their baseline figures are explicitly
classified in [baseline-measurements.json](baseline-measurements.json).

The Docker/QEMU multi-venue lab is documented in [future-lab.md](future-lab.md)
and intentionally has no runtime implementation in this bootstrap. The
existing MockX tests do not validate the SORX circuit, enclosure or latency.

See [release-criteria.md](release-criteria.md) for the remaining design gates and
[comparison.md](comparison.md) for the fixed before/after reference.

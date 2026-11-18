# Independent hardware environment

Run from this independent clone using `hardware/tools/env/run.ps1 python3 <tool.py>`.
The launcher resolves this checkout from its own location, mounts only its hardware/docs,
mounts baseline and reference images read-only, disables networking, and uses unique
Codex container names. No host scratch directory is inherited.

`image-id.txt` pins the existing reviewed tool image, not a mutable tag. The separate
`sorx-hw-codex:baseline-1` tag is for discoverability only. This image was originally
built by Claude; `Dockerfile.claude-reference` is attributed historical tooling, not
an independent build recipe. Its apt/transitive dependencies are not fully pinned.
The image ID and captured package inventories define the environment actually used.
Reproduction elsewhere needs that exact image or a newly qualified rebuild; do not
claim that rebuilding the reference recipe gives identical bytes.

The image has KiCad 7.0.11, KiAuto 2.3.4, system Python, Shapely 2.0.6, and
CadQuery 2.4.0 under `/opt/cq/bin/python`. All execution records retain image identity,
commands, statuses, input hashes and raw evidence. The environment alone is not a
hardware release approval.

The active hardware root is `/work/hardware`. A read-only alias of this same
clone's `hardware/hw` directory is mounted at `/hardware/hw`, because accepted
electrical candidate project tables explicitly refer to
`/hardware/hw/sorx.pretty`. This mounts no second checkout and grants no new
write path. Existing proof tables are retained unchanged. The launcher sets
`KICAD7_FOOTPRINT_DIR=/usr/share/kicad/footprints` for the pinned standard
KiCad7 libraries. Both path conventions are recorded in
`path-conventions.json`, which is included in verification source hashing.
The alias smoke loaded an actual custom footprint and checked `/proc/mounts`
to confirm read-only access; evidence is under
`validation/library-resolution-01/alias-smoke.json`.

Use an explicit argument array when passing options such as `-p`, which may
otherwise be parsed as ambiguous PowerShell common parameters:

```powershell
& hardware/tools/env/run.ps1 -ToolArgs @('python3', '-m', 'unittest', 'discover', '-s', '/work/hardware/tests', '-p', 'test_kicad_environment.py', '-v')
```

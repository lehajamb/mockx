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

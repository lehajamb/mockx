# Paused independent hardware verification

**NOT READY FOR FABRICATION. Paused at the user's request.**

The current native PCB is the accepted development checkpoint in
[the native promotion record](../validation/native-promotion-01/promotion.json).
Its fresh stored and refilled DRC reports contain **30 required opens and 93
other findings**. The model check reports 371 footprints,1,588 pads, no model
mismatches and no duplicate via sites.

The old full-verifier smoke run is historical, predates the integrated board,
and deliberately failed. Its raw report and source identity remain in
`validation/verifier-smoke-20261117/`; it does not qualify the current source.
The previous summary is preserved in
[the pause evidence](../validation/pause-checkpoint/historical-verification-report.md).

A fresh full verifier run, final integrated high-speed/mechanical checks and
coherent development export remain to be done after engineering resumes.
No release gate is newly marked passed at this pause.

Read [CODEX_RESUME.md](../../CODEX_RESUME.md) before continuing.

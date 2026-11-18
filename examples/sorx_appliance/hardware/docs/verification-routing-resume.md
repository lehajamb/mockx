# Verification and routing safe-wrap handoff

Status at the user's requested stop on 2026-11-17: **NOT READY FOR FABRICATION**.
This is a resume record, not permission to restart the stopped work. No engineering
changes were made after the stop request; this documentation was requested by the
parent agent. All paths below are relative to this hardware directory.

## Accepted common checkpoint

The parent reports that the accepted board was promoted to `hw/sorx.kicad_pcb`
and committed as `ad3d633`. Its accepted routing source is:

- `runs/electrical-final17/power-bridge-proof01.kicad_pcb`
- SHA-256: `5188991db0633f32b6552605679a701606510d5833916cdc843b0ec5413cead1`
- Fresh accepted native result: **30 required opens / 93 normalized other findings**.

Accepted sequential chain: `route-electrical-17-sma.kicad_pcb` (35/95),
`debug-power-proof02.kicad_pcb` (33/95), then `power-bridge-proof01.kicad_pcb`
(30/93), all under `runs/electrical-final17/`. Stronger per-net gates are recorded
in `validation/debug-power-proof-02/strong-gate-review.json` and
`validation/power-bridge-proof-01/strong-gate-review.json`: repaired target nets
have zero opens and no other net's open count increased. The independent
`validation/power-bridge-proof-01/net-identity.json` records no changed retained
copper nets, no changed pad nets, and no unexpected removals versus accepted15b.

The baseline and reference images remain comparison evidence. Do not replace the
accepted native board with any later-numbered experiment merely because its
filename looks newer. U13.4 remains a required power connection; MG_TXD0 remains
a protected timing signal requiring its dedicated interface review.

## Rejected and unpromoted experiments

| Experiment | Exact retained files | Result and reason |
|---|---|---|
| Ordinary completion proof21 | `runs/route-verifier-21-ordinary-proof.kicad_pcb`; `validation/ordinary-completion-proof-01/` | Six ordinary connections closed, but C64.2 became a new disconnected GND island. Total36 to31 opens is insufficient: per-net GND regressed. Do not promote. |
| Targeted C64 ground pass | `runs/ordinary-ground-repair/ordinary-input.kicad_pcb`; `runs/ordinary-ground-repair/ordinary-ground-repaired.kicad_pcb`; `validation/ordinary-ground-repair-01/` | Added zero copper; C64 is still disconnected despite the output filename. Isolated-table result31 opens/98 other, unchanged. Exit1. |
| RTC ground fanout | `validation/rtc-ground-proof-01/` | Could not restore the explicitly selected VBAT_RTC segment. Proposed `runs/route-verifier-22-rtc-proof.kicad_pcb` was not produced. |
| Closer J1.195 inward fanout | `validation/ground-inward195-proof-01/` | Could not restore RESET_MOCI#. Proposed `runs/ground-proof23/route-verifier-23-inward195.kicad_pcb` was not produced. |

Proof21 SHA-256 is
`9e4f04e0c5a1aacff22f042f629b0677f8b1434f5d74cbb7e14045035744ddb1`.
It used accepted15b, not the final accepted power chain. Its first comparison also
had library-warning wording differences; the isolated-table recheck does not
excuse the genuine GND regression. No subset of its routes has been accepted.

C64.2 is a B.Cu GND pad at (33.279999,49.11), size0.56 by0.62mm,
UUID `68f8c9a6-4ded-4c96-bea4-3685b643c316`.
Read-only files `c64-pad-obstacles.py/.json`, `c64-via-feasibility.py/.json`,
`c64-via-feasibility-grid.py/.json`, and `c64-track-obstacles.py/.json` in the
ordinary proof evidence directory reject the proposed named via-in-pad solution:
all25 sampled offsets have foreign copper conflicts, including protected
PCIE_CLK_N and the newly proposed PCIE_RST#_3V3 on In3. No via was added.
Rejecting or rerouting just the new reset route is an unexecuted future idea.

## Late helper state and validation limits

- `tools/ground_fanout.py` and `tests/test_rtc_fanout.py` add an explicit
  `--rtc-backup-detour` option only for reviewed VBAT_RTC at the existing0.15mm
  width, retained endpoints/C9, and at most20mm added planar length. Its1mA
  engineering allocation is not a manufacturer maximum. Three regression tests
  passed after failing-before evidence (`validation/rtc-fanout-tests-before.txt`
  and `rtc-fanout-tests-after.txt`). The native board trial failed as above.
- `tools/ground_stitch.py` adds `--target REF.PAD`, filtering proposed work while
  retaining global GND/native gates and nonzero status for unresolved GND.
  Four geometry tests passed; the C64 native run correctly reported no progress.
- `tools/trim_dangling_copper.py` and `tests/test_dangling_copper.py` are WIP.
  Four fixture tests passed. **No native dangling-copper cleanup was run.**
  The helper excludes nets with required opens, protected power/timing/analog
  nets, zones and locked copper, and requires actual pad-group equivalence.
  The requested object-by-object cleanup remains unfinished.
- `tools/env/run.ps1`, `tools/env/README.md`, and
  `tools/env/path-conventions.json` document the same independent clone's
  `hardware/hw` mounted read-only at `/hardware/hw` in addition to `/work/hardware`.
  This resolves the absolute local footprint-table path. The smoke evidence
  `validation/library-resolution-01/alias-smoke.py/.json` confirms identical
  source content, a loadable custom footprint and a read-only alias.

These additions do not make the board release-ready. Earlier verifier/export
foundation, source bindings, exact-object waiver rules and raw report retention
remain in place; `validation/release-gates.json` must be assessed against the final
native source, not assumed closed from fixture tests.

## Export and process requirements still unfinished

**`tools/export.py` has not been integrated with `tools/export_filled_vias.py`.**
The latter must be run against the eventual final PCB and its source-bound
via/pad-UUID manifest packaged with the fabrication process note. Existing proof
manifest: `validation/power-bridge-proof-01/filled-power-vias.json`; process note:
`docs/debug-power-and-via-fill.md`. The three accepted debug power via-in-pad sites
require an explicitly qualified filled/capped process. This is not an NC drill
file or supplier approval. Thermal and any later named ground-pad sites also need
their own reviewed process coverage. No final verification/export was performed
by this worker on the accepted30/93 board.

Microchip DS00002841D Figure7-3 shows a recommended5-by5 thermal-via field; its25
illustrated sites are **not a mandatory electrical via count**. The five accepted
U13 vias close the relevant electrical GND connection, but published JEDEC-board
thermal resistance cannot be assumed for this partial field and this board.
Keep thermal/assembly qualification open. Three separately accepted U2 EP vias
were added by the electrical stage; the fourth proposed site was rejected for
foreign-copper conflict.

## Ownership and stopped activity

This worker had no active command sessions when the stop was acknowledged.
All launched containers used `--rm`; no running owned container was deliberately
left for continuation. No push, merge, ordering or new fabrication approval was
performed. The parent owns Git staging and the final WIP checkpoint.

Independent PCIe RX skew and label proofs, and the unfinished MG_TXD0 proof, were
owned by other workers. They were not integrated by this worker into the accepted
power checkpoint; consult their safe-wrap records before applying anything.

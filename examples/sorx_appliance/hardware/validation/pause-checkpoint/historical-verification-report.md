# Independent hardware verification

**NOT READY FOR FABRICATION**

Run: verifier-smoke-20261117. Source identity: `06e0451dddd3ee74b3b15d94d40b44fed6ff725492d5de0f8134cf0a89e6e575`.

Raw commands, exit statuses, input hashes and evidence are in validation/verifier-smoke-20261117/verification.json.

## Executed checks

| Check | Status |
|---|---|
| erc | PASS |
| netlist-parity | FAIL |
| board-model-and-drills | FAIL |
| drc-stored | FAIL |
| drc-refilled | FAIL |
| mechanical | FAIL |

## Remaining failures

- Source inputs changed during verification; results cannot qualify this revision
- netlist-parity: model and schematic disagree
- board-model-and-drills: U1: wrong footprint <pcbnew.LIB_ID; proxy of <Swig Object of type 'LIB_ID *' at 0x7560de0aec10> >
- board-model-and-drills: U1: value 'CY15B104QN' != 'CY15V104QN'
- board-model-and-drills: U1.7: FRAM_HOLD# != FRAM_DNU
- board-model-and-drills: R11.2: FRAM_HOLD# != FRAM_DNU
- board-model-and-drills: R76: value '12.1k' != '6.04k'
- board-model-and-drills: Duplicate via/drill sites remain
- drc-stored: 307 unwaived findings
- drc-refilled: 307 unwaived findings
- mechanical: missing mechanical schema version 2
- mechanical: mechanical PCB hash mismatch
- mechanical: mechanical placement input hash mismatch
- mechanical: mechanical checker did not pass
- mechanical: geometry errors or missing geometry status
- mechanical: invalid or excessive interference
- mechanical: clearances not qualified
- Engineering review is not bound to current source inputs
- R01: OPEN â€” Independent engineering implementation and closure evidence pending
- R02: OPEN â€” Independent engineering implementation and closure evidence pending
- R03: OPEN â€” Independent engineering implementation and closure evidence pending
- R04: OPEN â€” Independent engineering implementation and closure evidence pending
- R05: OPEN â€” Independent engineering implementation and closure evidence pending
- R06: OPEN â€” Independent engineering implementation and closure evidence pending
- R07: OPEN â€” Independent engineering implementation and closure evidence pending
- R08: OPEN â€” Independent engineering implementation and closure evidence pending
- R09: OPEN â€” Independent engineering implementation and closure evidence pending

These software/CAD checks do not establish physical boot, thermal performance, EMC or wire-to-wire latency.

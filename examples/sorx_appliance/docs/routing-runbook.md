# Close one required connection

Start in the repository root on `codex/sorx-appliance`. All paths below the launcher are inside the pinned KiCad environment. A **net** is the set of pins meant to connect; an **island** is a group actually connected by copper. The goal of one ticket is to join two named islands without splitting any existing group. A successful ticket is not fabrication approval.

**1. Read the queue, then one relevant proof.** Run `python examples/sorx_appliance/tools/routing_status.py`. Use `--net 'NAME'` to see example endpoints from the saved report and matching decisions. Do not load the full board, all reports or the ignored trial archive into context. If hashes disagree or the report is missing, stop and reproduce native checks as in step 5. Do not change the ledger merely to silence a stale-input error.

**2. Name the repair before drawing.** Pick a local low-speed connection or a ground pin with a clear route to continuous ground copper. Record its net, two specific component pads on separate islands, adjacent protected connections, permitted edit area and one success condition. Resolve track or zone endpoints to real pins using the board and schematic. For example, `OE_JTAG_PU` has R49.2, U10.18 and U10.29: inspect which are already connected before proposing a join. Keep the U13.4 supply and U13.19 Ethernet transmit line for coordinated, class-specific work; do not feed them to an ordinary signal router.

**3. Make one local edit.** Start a unique directory below `hardware/runs/` from the accepted native PCB. Give the candidate its own same-basename copy of the native `.kicad_pro` (project rules) and any `.kicad_dru` custom rules. Preserve footprint resolution; the launcher mounts the checked-in footprint library. Record the parent SHA from the queue. Constrain any routing helper to the named scope, or make a local edit in KiCad. For ground, [ground_stitch.py](../hardware/tools/ground_stitch.py) supports `--target REF.PAD --trace-escapes`; its remaining-ground status is global. Treat its output as a proposal only. `route_completion.py left` omits ground and is not an acceptance check. Try at most one alternative that addresses a different cause.

**4. Gate the exact candidate.** In PowerShell set the inputs below to the real job and the two pads identified in step 2. The gate takes a *new* output directory, checks the accepted parent SHA, refills copper in fresh KiCad processes, and checks the proposed join against existing pad groups. Do not compare only total opens or the exact endpoint text of an open: KiCad can select a different track on the same island even when the board hasn't changed.

```powershell
$launcher = './examples/sorx_appliance/hardware/tools/env/run.ps1'
$pcb = '/work/hardware/hw/sorx.kicad_pcb'
# Set $job (e.g. '/work/hardware/runs/led-01'), $parentSha, $fromPad and $toPad.
& $launcher -ToolArgs @('python3','routing_gate.py',$pcb,"$job/candidate.kicad_pcb",
    '--parent-sha256',$parentSha,'--join',$fromPad,$toPad,'--out',"$job/gate")
if ($LASTEXITCODE -ne 0) { throw 'Connection check failed; do not promote' }
```

Only the named target net may change by default. If the ticket already documents a necessary neighboring detour, add `--allow-net NAME` for that specific net and review its geometry separately. The command never changes or promotes the board. On success it writes `comparison.json` with both PCB hashes and the touched nets; on failure preserve the reason and the worker log. A passed connectivity comparison alone is not an accepted repair.

A missing, failed or stale check rejects the attempt. No new short, spacing error, duplicate hole, or broken pad group is acceptable; compare other findings by fingerprint including multiplicity. Never clear an open by changing pin membership or suppressing a rule. If the edit touches a supply, confirm conductor width, current, voltage drop and return path ([power audit](../hardware/docs/electrical-resume.md)). For a fast interface, check actual path lengths, pair spacing, layer transitions, and the adjacent return copper ([interface evidence](../hardware/docs/high-speed-resume.md)). A connectivity pass cannot stand in for either review.

**5. Promote once, or log why you stopped.** Only promote if the connectivity gate **and** the applicable engineering checks pass. Copy the exact passing candidate to the active PCB; verify its SHA before and after. Reopen the native project via the launcher and run `verification_worker.py` into a new tracked evidence directory. Example from the repository root:

```powershell
$hw = 'examples/sorx_appliance/hardware'
$ticket = 'replace-with-a-new-unique-ticket'
New-Item -ItemType Directory -ErrorAction Stop "$hw/validation/$ticket"
$out = "/work/hardware/validation/$ticket"
& $launcher -ToolArgs @('python3','verification_worker.py','board',$pcb,
    "$out/board-check.json",'--drc',"$out/drc-stored.txt")
if ($LASTEXITCODE -ne 0) { throw 'Native recheck failed; retain the previous accepted board' }
```

Check subprocess status, empty model-mismatch and duplicate-via lists, and the newly written `drc-refilled.txt`. Match its violation fingerprints, pad groups and per-net counts against the passing candidate; only tolerate equivalent reporting endpoints for an already-open island. Keep commands, statuses, checked image ID, parent/candidate hashes and class-specific evidence paths. If they do not match, the promotion failed; retain the previous accepted board.

Update [routing-ledger.json](routing-ledger.json) only after the exact native recheck. For an acceptance, replace `inputs` with the current `routing_status.py --inputs` output, set `report` to the durable appliance-relative report path and `report_sha256` to its SHA-256. Append a decision with the existing fields (ID, result, parent/candidate hashes, nets, one-line lesson, evidence path). For a rejection, append only the decision; leave active board and evidence bindings untouched. If no candidate was produced, set its hash to null. Keep scratch boards out of Git. Rerun the queue and commit the accepted edit and its evidence, or just the rejected decision and its evidence.

**The C64.2 counterexample.** On an earlier board, the ordinary-21 trial joined six openings but cut ground to capacitor C64.2. The headline count improved from 36 to 31; [the pad-group audit](../hardware/validation/ordinary-completion-proof-01/ground-split-audit.json) identifies the newly isolated pin. A proposed vertical copper connection (a *via*) at C64.2 failed [all 25 sampled locations](../hardware/validation/ordinary-completion-proof-01/c64-via-feasibility-grid.json). Skip those offsets. A different local path for the proposed reset signal is a new hypothesis; overwriting today's board with trial 21 is not. In contrast, the [accepted power-bridge proof](../hardware/validation/power-bridge-proof-01/strong-gate-review.json) removed three opens with no increase on any other net. Both are development evidence, never a general waiver.

To choose the next repair, use the two commands in the [appliance quick start](../README.md#quick-start).

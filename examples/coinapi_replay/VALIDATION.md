# Replay validation

Status: functional tests and a complete one-hour replay with clean shutdown
pass. The original real-time performance targets have **not** passed. The PR
is ready for review with that limitation explicitly accepted; this is not
performance qualification.

## Environment and input

Validation on 2026-11-04 used Windows, an Intel Core i5-13500H, 16 GB RAM,
Conda Python 3.13.14, QuickFIX 1.15.1, and the supplied replay configuration.
Prometheus remained enabled. Python's default thread-switch interval was
unchanged. The example requested Windows 1 ms timer resolution.

The local Coinbase BTC-USDT capture contains 3,007 initial orders and 550,205
increments over approximately one hour. Its SHA-256 is:

```text
f67adb2e960582b369b4def960b985773b9aeb989f5ee20caea7cca9bb9ba801
```

The capture and generated FIX logs are not distributed with this repository.

## Functional checks

- `python -m pytest -q`: **1,033 passed** in 40.33 seconds on 2026-11-13.
- Existing C++ market-feed client built successfully; its CTest passed.
- Real-socket integration covers controller on/off, actual strategy fills,
  final full WebSocket depth equality, report drain, and owned process exit.
- Native qualification uses late GLIMPSE join, one deliberately dropped future
  MoldUDP64 packet, fresh recovery, exact final top-ten depth, and end-of-session.
- The unchanged viewer was observed through its UI during live runs.

## Complete controller-on run (2026-11-13)

Run `coinapi-20261113T124426-03408982` replayed all **550,205** historical
updates at 1x, after installing 3,007 initial orders. It sent 558,421 FIX
requests with the midpoint controller, real FIX strategy probe, full-book
WebSocket observer, and native-feed recovery enabled. Playback took
3,599.912 seconds; the full command took 3,610.691 seconds.

This run used the 60-second child shutdown allowance and
`--max-lag-seconds 500`. That relaxed abort cutoff allowed observation of the
whole capture; it does not change the original qualification thresholds.

| Measurement | Observed | Original target | Result |
|---|---:|---:|---|
| Runner start to first book | 0.630 s | <=5 s | Pass |
| Launcher entry to first book | 2.661 s | Recorded separately | — |
| Final FIX/feed drain | 1.769 s | <=2 s | Pass |
| Dispatch lag p99 upper bound | 9.0 ms | <=20 ms | Pass |
| Dispatch lag maximum | 126.017 ms | <=250 ms | Pass |
| HTTP-sampled scheduler lag p99 upper bound | 370.0 ms | <=20 ms | Fail |
| HTTP-sampled scheduler lag maximum | 1,493.908 ms | <=250 ms | Fail |

Final public depth and native top-ten depth verification passed. The native
observer recovered from exactly one deliberately dropped packet, observed no
unexpected upstream gaps, and reached End-of-Session. The simulation session
ended, the exchange exited with code zero, and no cleanup error was recorded.

Dispatch statistics contain 542,572 samples; scheduler statistics contain
3,594 HTTP samples. They describe different sample populations and do not
establish continuous-time maxima. No per-sample timeline is retained.

| Process memory (MiB) | Initial | Final / sampled peak |
|---|---:|---:|
| Exchange working set | 122.61 | 614.63 |
| Exchange private committed | 581.06 | 1,081.36 |
| Runner working set | 95.80 | 188.86 |
| Runner private committed | 76.56 | 173.69 |

All 3,595 samples per process succeeded. Initial samples follow seeding;
final samples precede session shutdown. Private committed memory is not
resident RAM. These endpoints alone do not establish a memory leak.

To reproduce from the repository root with the capture identified above:

```powershell
$replayCapture = 'data\coinapi\l3\BTC-USDT_COINBASE_20260806T231206Z_D-2026071000_IDDI-5950967_SC-COINBASE_SPOT_BTC_USDT_S-BTC__002DUSDT.itch.csv'
$replayNative = Join-Path (Get-Location) 'examples\cpp_market_feed\build-replay\Release\mockx_market_feed.exe'

python -m examples.coinapi_replay `
    --input $replayCapture `
    --native-client $replayNative `
    --probe `
    --speed 1 `
    --max-lag-seconds 500
```

Use the `mockx` Conda environment and build the existing C++ native client
first. Keep the host awake throughout playback. The launcher creates a fresh
output directory automatically. The capture is not included in the repository.

## Earlier one-minute measurements

A completed controller-off measurement processed all 11,003 events,
with a real FIX strategy client, full-book WebSocket observer, native recovery,
and the browser viewer joined during playback. It used the native FIX template
serializer. Seed-counter and cleanup review fixes followed this measurement.

| Measurement | Observed | Required | Result |
|---|---:|---:|---|
| Runner start to first book | 0.761 s | <=5 s | Pass |
| Launcher entry to first book | 2.808 s | Recorded separately | — |
| Final FIX/feed drain | 1.644 s | <=2 s | Pass |
| Dispatch lag p99 upper bound | 28.0 ms | <=20 ms | Fail |
| Dispatch lag maximum | 34.146 ms | <=250 ms | Pass |
| HTTP-sampled scheduler lag p99 upper bound | 296.5 ms | <=20 ms | Fail |
| HTTP-sampled scheduler lag maximum | 296.441 ms | <=250 ms | Fail |
| Final public/native feed verification | Passed | Required | Pass |
| Actual strategy fills | Present | Required | Pass |

Scheduler statistics above contain 62 HTTP samples, approximately one per
second; p99 is consequently the maximum sample for that run. Dispatch has
11,003 samples. Neither metric is an instantaneous continuous-time maximum.
Frame age includes publication and client processing and is reported separately.

An earlier one-minute run passed dispatch at 11.5 ms p99 but failed scheduler
lag at 219.5 ms p99. There is no claim that six consecutive windows passed.
The qualification helper retains the original limits and stops on failure.

## Earlier sustained controller-on attempt

The full-capture attempt on runtime commit `c733c66` failed after 837.44 seconds
of runner elapsed time and 125,660 source events. The one-second scheduler-lag
limit stopped playback at **1,038.083 ms**. Dispatch p99/max were
14.5/44.364 ms; HTTP-sampled scheduler p99 was 468.5 ms (835 samples).
The capture did not complete, so final feed equality and healthy final drain
were not certified. Native recovery had observed exactly the planned gap and
no upstream gaps before failure. Cleanup ended the owned session and the
exchange process exited with code zero, with no cleanup error.

| Process memory (MiB) | Initial | Last sample | Sampled peak |
|---|---:|---:|---:|
| Exchange working set | 123.64 | 266.71 | 266.71 |
| Exchange private committed | 580.64 | 725.96 | 725.96 |
| Runner working set | 97.54 | 123.31 | 123.31 |
| Runner private committed | 77.15 | 105.08 | 105.08 |

All 836 memory samples succeeded. These are partial-run measurements, not
full-hour memory qualification. The original one-minute and sustained gates
remain unmet; no accelerated run or relaxed limit substitutes for them.

Diagnostic GC observations found full-collection pauses on retained history,
but neither disabling automatic collection nor deferring the oldest generation
passed live qualification. A normal-GC comparison completed with dispatch p99
17.0 ms and sampled scheduler p99 39.5 ms, also above the target. These results
do not identify a single cause; default GC and thread-switch policies remain
unchanged. No experimental runtime settings were included in the implementation.
## Reproduce

Use the commands and input requirements in [README.md](README.md). Run
`tests/perf_coinapi_replay.py` for three one-minute windows with the controller
off and three with it on; add `--full-hour` for the sustained run. Generated
`result.json` and `qualification.json` retain failure details and measurements.

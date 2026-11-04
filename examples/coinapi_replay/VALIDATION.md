# Replay validation

Status: functional tests pass; the real-time performance targets have **not**
passed. Do not treat the replay as performance-qualified yet.

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

- `python -m pytest -q`: **1,031 passed** in 42.42 seconds.
- Existing C++ market-feed client built successfully; its CTest passed.
- Real-socket integration covers controller on/off, actual strategy fills,
  final full WebSocket depth equality, report drain, and owned process exit.
- Native qualification uses late GLIMPSE join, one deliberately dropped future
  MoldUDP64 packet, fresh recovery, exact final top-ten depth, and end-of-session.
- The unchanged viewer was observed through its UI during live runs.

## One-minute measurements

The latest completed controller-off measurement processed all 11,003 events,
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

## Sustained controller-on attempt

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

## Reproduce

Use the commands and input requirements in [README.md](README.md). Run
`tests/perf_coinapi_replay.py` for three one-minute windows with the controller
off and three with it on; add `--full-hour` for the sustained run. Generated
`result.json` and `qualification.json` retain failure details and measurements.

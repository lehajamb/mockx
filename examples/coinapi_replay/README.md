# CoinAPI replay

Run one Coinbase L3 capture through MockX's existing matching engine and real
FIX sockets. Use it to inspect client execution, latency, strategy fills, and
market-data handling while historical liquidity provides a realistic background.
The existing viewer is unchanged. See [validation status](VALIDATION.md) for measured results and the remaining performance qualification.

## Run

From the repository root on Windows:

```powershell
. 'C:/Users/lehaj/anaconda3/shell/condabin/conda-hook.ps1'
conda activate mockx
python -m pip install -r requirements.txt
python -m examples.coinapi_replay --input C:/captures/coinbase.csv.gz --date 2026-07-10 --duration 60
```

Omit `--date` when the filename contains `D-YYYYMMDD` or every timestamp
contains its calendar date. Omit `--duration` to consume the entire capture.
No CoinAPI account or download is required: the input is a local file.

The default BTC-USDT configuration expects source symbol
`COINBASE_SPOT_BTC_USDT`, including when a capture is renamed `coinbase.csv`.
Any symbol metadata in the filename or `symbol_id` column must agree. For a
custom `--symbol`, also supply its explicit `--source-symbol`; the launcher
records that source identity and never infers another instrument from prices.

The launcher owns its exchange process and a new output directory under
`replay-runs/`. It prints the viewer URL, verifies the child exchange identity,
logs on its FIX actors, installs the initial book, and starts paced playback.
Ports default to HTTP 8001, FIX 4001, MoldUDP64 18000 and GLIMPSE 18001; override
them with `--api-port`, `--fix-port`, `--md-port`, and `--glimpse-port`.
`--output` must name a new directory. Effective YAML ignores inherited
`QUESTDB_*` overrides; configure persistence explicitly in `--config` if needed.

Useful options:

| Option | Effect |
|---|---|
| `--speed 0.25` | Slow both historical timing and exchange simulated latency to one quarter of wall speed |
| `--no-controller` | Observe replay divergence without midpoint correction |
| `--controller-order-quantity 0.1` | Maximum requested quantity in one corrective IOC |
| `--controller-total-quantity 100` | Whole-run controller request budget |
| `--probe` | Run the included real FIX execution client with IOC orders and resting-order cancellations |
| `--audit` | Stream source events, requests, and reports to JSONL; increases disk and serialization work |
| `--native-client PATH` | Qualify the existing C++ native-feed client, including one deliberately lost packet and GLIMPSE recovery |

`--speed` accepts only `(0, 1]`. Default request timeout is 15 wall seconds,
pending request limit 4,096, and maximum pacing lag one wall second. The
scheduler lag limit is one simulated second (four wall seconds at speed 0.25).
Exceeding a limit fails the run visibly; the driver does not silently skip data,
pause simulated time, or switch to a different execution path.

On Windows, the launcher and its exchange request 1 ms timer resolution for the
run and release it on exit. This avoids coarse sleeps in the paced event loops.
The request follows [Windows timer semantics](https://learn.microsoft.com/en-us/windows/win32/api/timeapi/nf-timeapi-timebeginperiod);
actual wake timing remains subject to OS scheduling and is measured by the
qualification checks.

## Connect a client

The generated `strategy.cfg` is a real QuickFIX initiator configuration for
`strategy_bot -> MOCKX_COINAPI`. Use that session from a separate process when
`--probe` is absent. It has a participant distinct from the historical buyer,
historical seller, and midpoint controller. Execution reports and cancel/replace
responses use the ordinary exchange contracts and configured latency scheduler.
FIX SendingTime and heartbeat handling retain wall-clock protocol semantics.
Coordinate an external client's stop with the replay interval: final delivery
and book verification require a quiet book. Continued submissions can prevent
the bounded drain from completing. The included probe stops automatically.

The example instrument is BTC-USDT with a $0.01 tick and 0.00000001 BTC lot.
Clients must use those exact units. ITCH quantities represent integer lots;
prices retain native Price(4) encoding. SorX is a development consumer whose
live adapter must honor these units, native partial-cancel messages, current
FIX order identity, and GLIMPSE bootstrap/recovery. This example does not modify
or certify SorX's separate adapter or its in-process synthetic mode.

The supplied YAML uses inbound/outbound latency zero and one simulated
millisecond of engine latency. Change the YAML to test realistic inbound,
processing, outbound, and session-specific delays. There is no extra simulated
market-data delay; ITCH and WebSocket timing comes from the existing publisher
and actual transport. At slower speed all configured simulated latency takes
proportionally longer in wall time.

## Historical semantics

CSV and gzip CSV are read incrementally. Comma and semicolon delimiters are
supported. The fields are `time_exchange`, `time_coinapi`, `update_type`,
`is_buy`, `entry_px`, `entry_sx`, and `order_id`. Source timestamps retain up to
nine fractional digits; engine timestamps have microsecond resolution. Equal
source timestamps retain file order. Pacing follows `time_exchange`;
`time_coinapi` remains source metadata and adds no extra delivery delay.
The exchange keeps its normal simulated-clock origin; `source_start_ns` and
`simulation_anchor_ns` in the result record the mapping from capture time.
The capture must begin with a contiguous
`SNAPSHOT` block, followed by increments. Invalid decimals, crossed reference
books, duplicate live IDs, unknown deletes, underflow, or time reversal fail
with the source row rather than being silently repaired.

| Source operation | Executable action |
|---|---|
| Initial `SNAPSHOT` | Validated atomic native seed owned by the configured historical FIX sessions |
| `0` / ADD | FIX GTC limit NEW |
| `1` / SUB, `2` / SET | FIX amendment of historical total quantity, or cancel when no executable leaves remain |
| `3` / DELETE | FIX cancel if the simulated order is still live |
| `4` / MATCH | One opposite, price-capped FIX IOC; ordinary price-time matching decides the counterparty |

There is one executable order book. A compact reference book tracks only
historical state for targets and comparison. Historical MATCH does not also
subtract from a simulated passive order and never targets a chosen order ID.
The historical total budget is remaining source quantity plus cumulative
historical MATCH quantity. Actual MockX fills reduce executable leaves.
External fills are never restored by subsequent source amendments. A reused
source order ID starts a new lifetime.

Snapshot row order determines initial FIFO priority at equal prices. The capture
does not establish the original arrival order of all resting orders, so this
does not claim to reconstruct unknown historical queue priority.

Requests pipeline across independent orders. NEW can be followed immediately
by cancel; only amendments requiring an unresolved replacement identity wait
locally. FIX reports settle requests and accepted replacement IDs. Final
completion requires report drain, a FIX heartbeat fence, and the final public
WebSocket book matching the REST cut. Cleanup ends only the owned generation.

## Midpoint controller and accuracy

The controller sees the public WebSocket book and historical targets already
released by the replay clock. It never sees future targets or privileged engine
depth. Every 100 simulated milliseconds it may choose an improving sweep within
the first ten observed levels. It leaves the opposite level available, caps
price at the worst consumed level, permits only one in-flight IOC, and requires
a fresh book revision before another action. A one-tick deadband, stale-data
checks, lag checks, and quantity budgets limit intervention.

The controller can reduce midpoint drift; it cannot promise identical historical
execution, depth, or midpoint. Historical orders compete with strategies using
MockX price-time priority, configured latency, and self-cross prevention. These
differences are part of what the experiment measures.

`result.json` records input SHA-256, options, source operation counts, reports
and participating fill lots by actor, controller requests, dispatch/release/
scheduler/frame lag, final feed verification, sampled midpoint/spread/depth
differences, and final exchange statistics. Fill lots are attributed to each
participating side; summing actors double-counts a two-sided trade. Quality
samples compare the latest released reference state with the latest received
book, so they include transport and publication lag. They are diagnostics, not
an assertion of same-instant market fidelity.

Timing histograms use bounded half-millisecond buckets. They preserve sample
count, mean and exact maximum; p99 is an upper bucket bound. `first_book_seconds`
measures runner setup through the first book; `command_to_first_book_seconds`
starts at launcher entry and also includes input opening, hashing and child
process startup. Launcher-module import time is excluded. `drain_seconds`
includes final FIX and feed verification. The public snapshot interval in the
replay YAML is 50 ms: trade-free states coalesce before formatting, while trade
snapshots publish immediately. Matching, FIX reports and ITCH events do not
coalesce. The existing downstream WebSocket queue can still replace queued
snapshots for a slow subscriber, including snapshots containing trades. Use
FIX reports and native ITCH for execution and event completeness. The default
exchange interval remains zero.

Memory is bounded for replay input buffering, dependency queues, callback
reports, diagnostics and formatting caches. Exchange order IDs and terminal
order history grow within a run. FIX client-order-ID claims survive simulation
resets until process restart. This is not a constant-memory claim for an
unlimited run or process. Audit and QuickFIX file logs also grow on disk.

Memory samples cover the active seeded run: the initial sample follows seeding,
and the final sample precedes session end. OS peak working set covers each
process's lifetime. `drain_seconds` measures final FIX and feed verification;
session end, native End-of-Session verification and process shutdown follow it.
`command_elapsed_seconds` includes those cleanup phases.

## Native feed qualification

Build the [existing C++ client](../cpp_market_feed/README.md), then pass its
executable to `--native-client`. The helper relays native bytes to that client;
the C++ implementation performs decoding, book reconstruction, continuity
checks and GLIMPSE recovery. The helper joins after the seed, drops one future
data packet, requires a fresh coherent snapshot, then compares final top-ten
depth in exact ticks/lots and checks clean end-of-session. Unexpected upstream
loss or extra native gaps fail qualification. The entire C++ book is maintained,
but the displayed comparison covers ten levels.

Use `--probe` with the native helper for execution qualification. The generated
viewer URL can be opened throughout the run. The lightweight example contains
no second matching engine, distributed replay coordinator, or accelerated
external backtester. Multi-venue replay, other CoinAPI formats, checkpoint/
resume, full historical-fill identity reproduction, and a SorX adapter upgrade
are not implemented.

## Repeatable performance checks

After building the native client, run the manual qualification script:

```powershell
python tests/perf_coinapi_replay.py --input C:/captures/coinbase.csv.gz --native-client examples/cpp_market_feed/build-replay/Release/mockx_market_feed.exe
```

It runs three 60-second windows with the controller off and three with it on,
using the same input, real FIX strategy orders, a full-book WebSocket observer,
and native recovery. Strategy execution must include actual fills. Each run
must complete with runner initialization through
the first seeded book at most five seconds, dispatch and sampled scheduler lag p99 at most 20 ms and maximum
at most 250 ms, and final drain at most two seconds. The script saves its checks
in `qualification.json` and stops at the first failure. Performance depends on
host load; functional tests use no wall-clock performance assertions.
Total command-to-first-book time is recorded separately, including child-process
startup and input preparation, and can exceed the seed initialization budget.

Add `--full-hour` for a single complete capture run with controller and strategy
enabled. It requires at least 3,590 seconds of actual playback and records
initial, final, sampled peak and OS peak process memory. No accelerated clock
is substituted. `tests/perf_coinapi_core.py` separately measures native matching
and performs a final exhaustive book audit; it is not a substitute for socket
qualification.

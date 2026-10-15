# MockX

MockX is a deterministic mock exchange with native FIX 4.4 order entry,
Nasdaq ITCH 5.0 over MoldUDP64, GLIMPSE 5.0 over SoupBinTCP, and a QuestDB
command journal. It supports externally controlled simulated time or realtime
advancement, concurrent participants, and independent venues sharing QuestDB.
Grafana displays an external projection of the journal.

The matching engine uses integer ticks/lots, price/time priority, immutable
reports and unconditional self-trade prevention. FIX, ITCH, GLIMPSE and QuestDB
are required runtime components. REST controls simulations and provides diagnostics.

## Run the example

```powershell
docker compose up --build
```

The single-venue stack provides FIX on `4001`, REST on `8001`, GLIMPSE on
`18001`, ITCH via the host relay on `18000`, QuestDB on `9000`, and Grafana on
`3000`. Open Grafana's **MockX native exchange** dashboard. Anonymous dashboard
access has the Viewer role; its QuestDB PGWire connection is read-only.

The live FIX client starts a labelled simulation and supplies liquidity and
strategy traffic. To stop it, use `docker compose stop live-market`.
Use a fresh `questdb-native-data` volume for this schema; there are no legacy
writes or historical-table migrations. Existing unrelated volumes need not change.

For a local Python exchange with QuestDB running:

```powershell
. "C:\Users\lehaj\anaconda3\shell\condabin\conda-hook.ps1"
conda activate mockx
python -m src.mockx.cli --config configs/mockx-local.yml --fix-config configs/mockx1.cfg
```

Start a fresh run through `POST /api/v1/session/start` with
`{"scenario_label":"example"}`. The response's `session_id` is a server-generated
UUID; `scenario_label` is retained separately. It also returns `run_generation`
and `starting_time`. In external clock mode, submit FIX messages and then call
`POST /admin/advance-time` with `{"milliseconds":100}` or an aware `target_time`.
Events exactly at the target are included. End with `POST /api/v1/session/end`.
Normal end waits for journal acknowledgment. Query visibility can lag that
acknowledgment; journal readers poll for the completion watermark.

```powershell
python examples/submit_orders.py --fix-config examples/fix_client.cfg --symbol BTC-USDT --timeout 30
python examples/batch_orders.py --help
python examples/live_market.py --help
```

`batch_orders.py` demonstrates bounded pipelined D messages and native E lists,
then native replacement and cancellation. `examples/fix_support.py` contains the
shared message builders. Each pipeline window/list waits for individual outcomes
before advancing to the next window.

## FIX contract

| Incoming message | Behavior |
|---|---|
| NewOrderSingle (`35=D`) | Limit/market; GTC/IOC |
| NewOrderList (`35=E`) | Immediate, unfragmented lists; individual ExecutionReports |
| OrderCancelRequest (`35=F`) | Cancel pending or accepted live orders owned by the session |
| OrderCancelReplaceRequest (`35=G`) | Amend accepted live GTC limit orders |
| MarketDataRequest (`35=V`) | Rejected; native market data uses ITCH/GLIMPSE |

Outgoing messages include ExecutionReport (`35=8`), OrderCancelReject (`35=9`),
Session Reject (`35=3`), Business Message Reject (`35=j`) and MarketDataRequestReject
(`35=Y`). QuickFIX uses in-memory session stores. Logs provide observability.

A native list carries ListID, ListExecInstType=1 (immediate), BidType=3 (no
bidding), TotNoOrders equal to NoOrders, LastFragment=Y when supplied, and ProgRptReqs=3 (realtime
reports). Members have contiguous ListSeqNo from 1. The default maximum is 100,
configurable up to 1,000. Structural validation precedes identifier claims;
malformed lists are rejected as a whole. Identifiable semantic errors reject
individual members. Children enter the ordinary receive scheduler in ListSeqNo
order at their parent's deterministic ingress position. They retain individual
network/engine latencies, matching, rejection checks and reports. Lists have no
transactional rollback. ListID is included in member reports and must match on
subsequent F/G requests.

Replacement preserves logical FIX OrderID, participant ownership and cumulative
fills. Successful replacement changes the current ClOrdID; subsequent F/G
requests must reference that identifier. Failed replacement leaves the original
untouched. OrderQty is the intended **total**, including existing fills, and is
validated at processing time. A positive total at or below CumQty stops the
remainder, clamps total to CumQty, and acknowledges replacement without a trade.

Same-price reduction/no-op preserves FIFO. Repricing or increasing total quantity
loses priority and can match normally. A priority-preserving reduction emits ITCH
X; losing priority emits Delete, passive executions, then Add with a fresh
displayed reference if a remainder rests. A full reduction removes the order.
Cancel Reject distinguishes F (`CxlRejResponseTo=1`) from G (`=2`). Initial-order
rejection probability applies only to new orders.

Quantities and prices must be finite, positive and exactly on the instrument's
lot/tick grid; values are parsed independently of ambient Decimal context.
Market orders omit price. IOC/market remainders never rest. A missing TIF defaults
to GTC; unsupported values are rejected. ClOrdID is 1–64 visible ASCII characters.
Session-scoped identifier claims survive simulation resets and reconnects.

Every QuickFIX session has an exact `fix.session_participants` mapping. Aliases
only determine immutable liquidity classification (`strategy_bot` or market),
never ownership. On self-cross, passive and aggressor remainders are cancelled,
prior third-party fills remain truthful, and no self-trade or volume is invented.

## Publication, persistence and recovery

Matching is synchronous. A completed command yields an immutable ordered effects
envelope containing book changes, reports, trades and request outcomes. Effects
become publishable after invariant checks. ITCH encodes the same native book
changes stored in the journal; the engine builds no full-book publication snapshot.

QuestDB's single authoritative `exchange_events` WAL table uses `TIMESTAMP_NS`
and `DEDUP UPSERT KEYS(timestamp, run_id, event_sequence)`. It stores venue/run identity,
command sequence, event sequence, ordinal, exact simulated nanoseconds, typed
order/report/trade fields and immutable liquidity categories. Instrument definitions
are stored once per UUID run. Logical event identity is
`(timestamp, run_id, event_sequence)`; deduplication makes uncertain HTTP retries
idempotent. Delayed reports retain their originating UUID even after a reset.

The bounded writer admits whole command groups. Consecutive groups are combined
in transactional single-table HTTP writes, without splitting an ordinary command.
Automatic table/column creation is disabled; the sink creates and verifies the
explicit schema. Queued **and in-flight** rows/bytes count against capacity. One
completed command can await admission. Defaults are 1,000 rows or 4 MiB per flush,
100 ms interval, 128 scheduler events per cooperative processing batch, and a
10-second pressure deadline. Between commands, 80% count/byte occupancy pauses
production until both reach at most 50%. Retries preserve identities and use the
existing bounded backoff policy.

| Failure | Result |
|---|---|
| Normal invalid/rejected request | That request fails; other commands continue |
| Admission failure after validated matching | Truthful fills/reports retained; run incomplete; later trading stops |
| Pressure timeout after admission | Records retained; distinct PERSISTENCE_BACKPRESSURE_TIMEOUT |
| Required ITCH/GLIMPSE publisher/listener failure | Current reporting finishes; GLIMPSE revoked; run fails |
| Individual client disconnect or receiver UDP gap | Isolated from exchange correctness |
| Unexpected engine/invariant failure | Scheduler stops; no fabricated completed-command marker; fresh run required |

There is no rollback or automatic restart restoration. End a failed run and start
a fresh UUID run. Shutdown before normal end records an incomplete run.

Protocol-neutral order-level captures are shared by GLIMPSE and checkpoints.
They preserve FIFO, displayed reference, logical identity, leaves, price, side
and category. Checkpoints occur at start, successful normal end, and
`POST /api/v1/checkpoints` during coherent active runs. Bounded checkpoint chunks
are followed by a completion marker containing expected count and journal/ITCH
cuts. Readers ignore incomplete checkpoints and expose command-complete state.
ITCH packets pack multiple messages without suppressing deltas and flush before
clock changes, yields, persistence waits and snapshot cuts. Feed session names
are fresh across process restarts.

## Configuration

All models reject unknown fields; legacy enabled flags, WebSocket/CORS options
and lossy overflow policies are removed. The venue identity is `exchange_name`.
Use distinct names for independently running exchanges.

<!-- validated-config-example -->
```yaml
exchange_name: MockX-local
instruments:
  BTC-USDT:
    tick_size: '0.01'
    lot_size: '0.001'
latency:
  network_ms: 10
  engine_ms: 2
execution:
  reject_prob: 0.0
  random_seed: 42
admin_api:
  host: 127.0.0.1
  port: 8000
fix:
  max_orders_per_list: 100
  session_participants:
    'FIX.4.4:MOCKX1->CLIENT1': participant-1
scheduler:
  mode: external
  processing_batch_size: 128
questdb:
  url: http://127.0.0.1:9000
  health_url: http://127.0.0.1:9003
  username: mockx-user
  password: mockx-password
  flush_batch_size: 1000
  flush_batch_bytes: 4194304
  flush_interval_ms: 100
  max_buffer_records: 10000
  max_buffer_bytes: 67108864
  backpressure_timeout_ms: 10000
moldudp64:
  multicast_group: 239.192.0.1
  multicast_port: 18000
soupbintcp:
  host: 127.0.0.1
  port: 18001
  credentials:
    GLIMP: SECRET
```

ITCH symbols are ASCII, at most eight bytes; ticks must be representable as
Price(4). Soup usernames/passwords and transport limits are validated at startup.
QuestDB environment overrides include `QUESTDB_URL`, `QUESTDB_HEALTH_URL`,
`QUESTDB_USERNAME`, `QUESTDB_PASSWORD`, `QUESTDB_FLUSH_BATCH_SIZE`,
`QUESTDB_FLUSH_BATCH_BYTES`, `QUESTDB_FLUSH_INTERVAL_MS`,
`QUESTDB_MAX_BUFFER_RECORDS`, `QUESTDB_MAX_BUFFER_BYTES`,
`QUESTDB_BACKPRESSURE_TIMEOUT_MS`, `QUESTDB_RETRY_BACKOFF_MS`,
`QUESTDB_MAX_RETRY_DELAY_MS`, `QUESTDB_SHUTDOWN_TIMEOUT_MS` and
`QUESTDB_REQUEST_TIMEOUT_MS`. Credentials must be supplied together.

The authenticated health endpoint can be checked with:

```powershell
curl.exe --fail --user mockx-user:mockx-password http://localhost:9003
```

## Diagnostics and dashboards

REST routes are `/`, `/healthz`, `/readyz`, `/symbols`, `/book/{symbol}`, `/stats`,
`/metrics`, `/api/v1/session/start`, `/api/v1/session/end`,
`/api/v1/checkpoints` and `/admin/advance-time`. `/ui`, WebSockets and HTTP order
entry are absent. On-demand `/book` uses canonical decimal strings. Prometheus
contains operational counters, CPU histograms and gauges; command/report timing
for trading analysis is stored in the native journal.

The external [projector](examples/dashboard/projector.py) uses the shared
[reconstruction reader](src/mockx/journal_reader.py). It writes 40 levels per side
at most every 250 ms into `dashboard_depth`, with source run/cut, simulated
 timestamp and display-only double arrays. Source identities deduplicate retries.
Grafana is pinned to 12.3.3, the QuestDB datasource to 0.1.8 and nLine Plotly to
1.8.1. It includes venue/run/symbol selectors, depth, liquidity walls, BBO/spread,
trades, volume, OHLC and VWAP. Live views follow simulated time; historical views
use the selected run. Cursor/status and sample timestamps expose persistence lag.
Dashboard/projector failures do not fail the exchange. See
[dashboard deployment](doc/native-dashboard.md) for provisioning and attribution.

## Multiple venues and native clients

```powershell
docker compose -f examples/cpp_sor/docker-compose.yml up --build
```

The two-venue stack uses distinct venue names with shared QuestDB and Grafana.
The [C++ market-feed client](examples/cpp_market_feed/README.md) combines
ITCH/MoldUDP64 and GLIMPSE/SoupBinTCP recovery. The
[C++ SOR example](examples/cpp_sor/README.md) retains native feeds and existing
routing algorithms; its shared decoder handles X reductions as well as A/E/C/D.

CoinAPI downloading/replay, restart restoration and new SOR algorithms are
subsequent work. This foundation permits a replay client and strategy client to
use separate participants on the same venue.

## Tests

```powershell
python -m pytest -q
```

The suite covers deterministic ingress, native lists/replace, invariant and exact
arithmetic regressions, journal groups/reconstruction, pressure/fault behavior,
checkpoints, lifecycle cancellation, feeds and dashboard projection. Native C++
CTest, Docker/QuestDB integration and representative throughput checks supplement
the Python suite. Architecture and source references are in [doc](doc/index.html).

The [native submission benchmark](doc/native-benchmark.md) compares individual
`D`, pipelined `D`, and native `E` with acknowledged persistence. To run against
a real QuestDB instance:

```powershell
$env:QUESTDB_HEALTH_URL = "http://localhost:9000"
$env:QUESTDB_REQUEST_TIMEOUT_MS = "10000"
# Set both when QuestDB authentication is enabled:
# $env:QUESTDB_USERNAME = "your-user"
# $env:QUESTDB_PASSWORD = "your-password"
python scripts/benchmark_native_exchange.py --questdb-url http://localhost:9000 --depths 100,1000 --orders 100 --window 100 --sink-delays-ms 0,5 --dashboard-every 0
```

With `--dashboard-every 0`, dashboard activity belongs to a separately running
projector and does not contribute to the benchmark producer's process CPU.

# MockX

MockX is a deterministic, FIX-speaking mock exchange for strategy tests and
simulation. It provides a price-time-priority order book, an externally
controlled simulated clock, FIX order entry, typed REST administration,
immutable WebSocket market data, process metrics, and optional buffered
InfluxDB persistence.

## 1. What MockX implements

- Deterministic scheduling with FIFO ordering for equal timestamps.
- Inclusive time advancement: events exactly at the requested target execute.
- Atomic order-book quantity accounting and invariant checks.
- Limit and market orders with GTC and IOC lifecycle rules.
- Stable-FIX-session-scoped `ClOrdID` identity and execution-report routing.
- Atomic simulation lifecycle transitions and generation-aware public IDs.
- One execution report per order side and per fill.
- Frozen market-data snapshots containing only scalar values.
- A global per-exchange market-data sequence.
- FastAPI liveness, readiness, REST, WebSocket, UI, stats, and Prometheus
  exposition on one API port.
- Optional buffered InfluxDB writes that flush at session end and shutdown.

MockX is a simulator, not a production trading venue.

## 2. Supported order and FIX behavior

MockX uses FIX 4.4 through QuickFIX, but supports a deliberately narrow message
set.

| Direction | Supported messages |
|---|---|
| Incoming | New Order Single (`35=D`), Order Cancel Request (`35=F`) |
| Special incoming | Market Data Request (`35=V`) is rejected because market data is served by REST/WebSocket |
| Outgoing | Execution Report (`35=8`), Order Cancel Reject (`35=9`), Market Data Request Reject (`35=Y`), session Reject (`35=3`) |

Supported orders are limit and market orders with GTC or IOC. Replace
(`OrderCancelReplaceRequest`) is unsupported.

- An unfilled or partially filled IOC remainder is canceled and never rests.
- A market-order remainder is canceled regardless of its supplied TIF.
- A partially filled GTC limit remainder rests without regressing to `NEW`.
- A wholly unfilled GTC limit receives `NEW` and rests.
- Each fill creates passive then aggressor trade reports at the same simulated
  timestamp; scheduler FIFO sequence preserves that order.
- Every syntactically valid New Order Single (`D`) and Order Cancel Request
  (`F`) claims its `ClOrdID` in one namespace scoped to the stable QuickFIX
  `SessionID`. Its history survives transport replacement, logout/relogon, and
  simulation reset; it is cleared only by process restart. A distinct
  `SessionID` has an independent scope.
- A cancel can affect only the owning session's live order.
- Cancel targets are resolved from the authoritative engine state when cancel
  processing executes, not from a gateway snapshot taken in the FIX callback.
  Pending and resting `NEW`/`PARTIAL` orders can be canceled. Filled or
  rejected orders return `TOO_LATE_TO_CANCEL`; canceled orders return
  `ALREADY_CANCELED`; unknown and foreign-session `OrigClOrdID` values remain
  distinct rejects.

Public exchange identifiers include the successful simulation-run generation:

| FIX/public field | Format |
|---|---|
| `OrderID` | `G{generation:08d}-O{counter:08d}` |
| `ExecID` | `G{generation:08d}-E{counter:08d}` |
| `TradeID` | `G{generation:08d}-T{counter:08d}` |

Engine and gateway rejection reports share the same run scope. Numeric engine
handles remain internal and are not serialized as FIX or WebSocket IDs.

Real matching and serialization durations use a monotonic wall clock.
Configured network, engine, and end-to-end latency values use simulated time.
The two clock domains are reported with distinct units.

### Deterministic FIX ingress and cancel latency

QuickFIX callbacks for `D` and `F` parse immutable normalized command data
only. They never mutate the engine. A run-scoped central ingress sequencer
timestamps and acknowledges each command, then drains acknowledged envelopes
in this ascending order:

```text
simulated receive timestamp
complete QuickFIX SessionID.toString()
FIX MsgSeqNum
run-local ingress sequence
```

The ingress sequence is the final tie-break, never the sole FIFO source, and
resets to zero on every successful simulation start. Sorted envelopes become
`new_order_received` or `cancel_received` scheduler events. Scheduler FIFO
then preserves causal ordering among later equal-time stages.

Cancel requests use four explicit scheduled stages:

```text
cancel_received
cancel_arrived_at_exchange
cancel_engine_process
cancel_report_delivery
```

`cancel_arrived_at_exchange` applies configured inbound network latency plus
the requester's session-alias delay. `cancel_engine_process` additionally
applies configured engine latency. `cancel_report_delivery` applies configured
outbound latency to both successful execution reports and order-cancel
rejects. A successful pending cancel advances the order version, so an older
queued new-order process command is harmless.

Equal-time events already in the scheduler keep their earlier scheduler
priority. Consequently, an already-queued match can fill an order before a
later cancel; the cancel then returns `TOO_LATE_TO_CANCEL`. Conversely, a
cancel wins only when configured timing and the explicit ingress/scheduler
ordering place its engine event first.

## 3. Architecture

`MockExchange` owns one lifecycle coordinator, scheduler, engine, metrics
collector, FIX application, optional FIX acceptor, `ExchangeAPI`, and FastAPI
app. The lifecycle coordinator's one asynchronous lock serializes component
startup/shutdown, session start/end, scheduler advancement, full run reset, and
committed FIX ingress. The retained references are:

```python
exchange.api                 # ExchangeAPI
exchange.app                 # exchange.api.app
exchange.app.state.exchange_api
exchange.app.state.engine
exchange.app.state.scheduler
exchange.app.state.metrics
exchange.app.state.session_registry
exchange.lifecycle           # ExchangeLifecycle
```

No route inspection or closure internals are used to recover ownership.

Construction creates and validates components without starting external
traffic. FastAPI lifespan startup then:

1. Marks readiness false.
2. Captures the running asyncio loop.
3. Confirms the engine, scheduler, metrics, registry, API, and callbacks.
4. Assigns the loop to the QuickFIX adapter.
5. Starts the FIX acceptor when enabled.
6. Starts the optional realtime scheduler task.
7. Enables administrative actions and marks readiness true.

Shutdown:

1. Marks readiness false and disables administrative actions.
2. Stops the FIX acceptor.
3. Cancels the realtime scheduler task.
4. Closes and removes WebSockets.
5. Flushes and closes metrics persistence.
6. Clears adapter loop references and marks components stopped.

Shutdown is idempotent. A persistence-close failure is logged and reported as
degraded without skipping the remaining teardown.

## 4. Clock modes

The shipped configuration uses:

```yaml
scheduler:
  mode: external
```

In `external` mode, only `POST /admin/advance-time` or direct test calls move
the simulated clock. The route requires an `ACTIVE` simulation session;
`STOPPED` returns `409 SESSION_NOT_ACTIVE`. Submitting a FIX order queues
events; it does not process them until time advances.

In-process backtesters and tests that submit through QuickFIX callbacks should
first await `exchange.fix_app.wait_for_ingress()`. The returned
`IngressBarrier` identifies the active run generation and the captured
run-local high-water sequence. The await guarantees those commands have
entered the central queue and flushes only through that captured boundary into
scheduler receive events. Administrative and realtime advancement also
capture, await, and flush pending ingress before running the scheduler. There
is intentionally no public HTTP ingress-barrier endpoint.

The optional `realtime` mode advances simulated time only while the lifecycle
is `ACTIVE`, by
`scheduler.interval_ms` after each non-busy asyncio interval. Event ordering
remains deterministic at a given simulated timestamp, but the relationship to
machine scheduling is necessarily nondeterministic. Manual advancement returns
`409 CLOCK_MODE_CONFLICT` in realtime mode.

Market-data timestamps, fills, reports, and scheduler targets are always
timezone-aware simulated UTC. Wall-clock time is not used as simulated time.

### Fail-stop scheduler failures

Each scheduler generation is explicitly `RUNNING`, `FAILED`, or `STOPPED`.
An unexpected event-handler exception is fatal to the active simulation run:
the responsible event is removed from the live queue into one immutable
dead-letter record, the original exception is retained and chained for local
diagnostics, and later events remain queued but are not processed. Missing
handlers are treated the same way. Arbitrary events are never retried because
a handler may have failed after partially mutating engine state.

Once failed, scheduling new events, processing the queue, or moving simulated
time is rejected. `POST /admin/advance-time` returns HTTP 503 with
`SCHEDULER_FAILED`, `/readyz` returns HTTP 503 with
`components.scheduler="failed"`, and `/stats` remains available for
inspection. Engine state is not transactionally rolled back: an order, book,
trade, metric, or report may show a partial mutation, but the failed run can
never return a normal advancement response or remain traffic-ready.

Every advancement has a shared event budget. The default limit is 100,000
successfully handled events across the whole call, including zero-delay work
scheduled by handlers. If another eligible event exists, it is dead-lettered
with `EVENT_PROCESSING_LIMIT_EXCEEDED`; this prevents infinite same-time
self-rescheduling.

## 5. Local quick start

QuickFIX includes native code, so install its platform build prerequisites
first. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

Start the local trusted-interface configuration in terminal 1:

```bash
python -m src.mockx.cli \
  --config configs/mockx-local.yml \
  --fix-config configs/mockx1.cfg
```

Verify process liveness and traffic readiness:

```bash
curl -f http://127.0.0.1:8001/healthz
curl -f http://127.0.0.1:8001/readyz
```

Start a clean simulation session:

```bash
curl -f -X POST http://127.0.0.1:8001/api/v1/session/start \
  -H "Content-Type: application/json" \
  -d '{"run_id":"quickstart"}'
```

In terminal 2, start the bounded example client:

```bash
python examples/submit_orders.py \
  --fix-config examples/fix_client.cfg \
  --symbol BTC-USDT \
  --timeout 30
```

While it waits for reports, advance the external clock from terminal 3:

```bash
curl -f -X POST http://127.0.0.1:8001/admin/advance-time \
  -H "Content-Type: application/json" \
  -d '{"milliseconds":10}'
```

Inspect the same committed state through each public surface:

```bash
curl -f http://127.0.0.1:8001/book/BTC-USDT
curl -f http://127.0.0.1:8001/stats
curl -f http://127.0.0.1:8001/metrics
```

Open `http://127.0.0.1:8001/ui/BTC-USDT` for the WebSocket-backed book UI.
End and flush the session:

```bash
curl -f -X POST http://127.0.0.1:8001/api/v1/session/end
```

## 6. Docker quick start

Docker Compose runs two exchanges and InfluxDB:

```bash
docker compose up --build -d
docker compose ps
```

The container health checks use each service's internal
`http://localhost:8000/readyz`. For MockX-1:

```bash
curl -f http://localhost:8001/readyz
curl -f -X POST http://localhost:8001/api/v1/session/start \
  -H "Content-Type: application/json" \
  -d '{"run_id":"docker-quickstart"}'
python examples/submit_orders.py --fix-config examples/fix_client.cfg
curl -f -X POST http://localhost:8001/admin/advance-time \
  -H "Content-Type: application/json" \
  -d '{"milliseconds":2000}'
curl -f http://localhost:8001/book/BTC-USDT
curl -f -X POST http://localhost:8001/api/v1/session/end
```

The Docker MockX-1 configuration gives `liquidity_bot` a 1000 ms inbound
session delay, so the Docker example advances 2000 ms.

## 7. REST API

Core routes are stable across both instances:

| Method | Route | Contract |
|---|---|---|
| GET | `/` | Exchange name and process status |
| GET | `/healthz` | Process liveness |
| GET | `/readyz` | Traffic readiness and component states |
| GET | `/symbols` | Configured symbols |
| GET | `/book/{symbol}` | Immutable current book snapshot |
| GET | `/stats` | Typed process metrics snapshot |
| GET | `/metrics` | Prometheus text exposition when enabled |
| GET | `/ui/{symbol}` | Browser book UI |
| GET | `/openapi.json` | Published REST schema |
| WS | `/ws/{symbol}` | Market-data stream |

Book response:

```json
{
  "symbol": "BTC-USDT",
  "sequence": 42,
  "timestamp": "1970-01-01T00:00:02+00:00",
  "bids": [
    {
      "price": 99.0,
      "qty": 1.0,
      "strategy_qty": 0.0,
      "market_qty": 1.0,
      "order_count": 1
    }
  ],
  "asks": []
}
```

Unknown symbols return HTTP 404:

```json
{
  "detail": {
    "code": "UNKNOWN_SYMBOL",
    "message": "Unknown symbol: INVALID"
  }
}
```

`/stats` separates orders, trades, cancellations, reports, sessions, book
gauges, latency, persistence, and scheduler state. The `cancellations` object
contains `accepted`, `unknown_order`, `already_terminal`, `wrong_owner`,
`duplicate_request`, and `stale_run_generation`. Scheduler diagnostics contain `state`,
`pending_events`, the current-generation `failure_count`, the configured
`max_events_per_advance`, and an optional `dead_letter`. The dead letter
contains only deterministic scalar metadata captured when the event was
scheduled; it never exposes the live command object. Latency field names
include `_us` or `_ms`; REST reads do not mutate counters.

## 8. WebSocket protocol

Connect to `/ws/{symbol}` on the same origin as the API. The first message is a
current immutable snapshot with empty `trades`. It uses the latest published
sequence and does not increment it:

```json
{
  "type": "market_data",
  "symbol": "BTC-USDT",
  "sequence": 42,
  "timestamp": "1970-01-01T00:00:02+00:00",
  "bids": [],
  "asks": [],
  "trades": []
}
```

One committed matching action publishes one `market_data` message. Its
`trades` array contains only fills from that action:

```json
{
  "type": "market_data",
  "symbol": "BTC-USDT",
  "sequence": 43,
  "timestamp": "1970-01-01T00:00:02.001000+00:00",
  "bids": [],
  "asks": [],
  "trades": [
    {
      "trade_id": "17",
      "price": 100.0,
      "qty": 2.0,
      "aggressor_side": "BUY",
      "timestamp": "1970-01-01T00:00:02.001000+00:00"
    }
  ]
}
```

Heartbeat messages are distinct and do not advance market-data sequence:

```json
{
  "type": "heartbeat",
  "symbol": "BTC-USDT",
  "timestamp": "1970-01-01T00:00:02.001000+00:00",
  "last_sequence": 43
}
```

Unknown symbols receive `type="error"`, code `UNKNOWN_SYMBOL`, then close code
1008.

Sequence scope is global per exchange, across all symbols. It starts at 0,
increments exactly once for each published book snapshot, and resets to 0 only
when a new simulation session resets the engine. Session start closes existing
WebSockets so clients reconnect across that boundary. Sequence state is not
persisted across process restarts.

WebSocket registration is lifecycle-gated. A connection attempted during
`STARTING` or `ENDING` receives an error envelope and close code 1013. Delayed
snapshots from an older run generation are discarded, so they cannot reach
clients registered after a reset.

Broadcasting copies the connection registry to a tuple before sending, holds no
registry lock across network awaits, removes failed clients afterward, and
does not let one client block another. Serialization uses the already-created
snapshot and never rereads a live book.

The browser derives `ws://` or `wss://` and host/port from
`window.location`. It never hardcodes a container port. It tracks heartbeat and
market-data freshness separately. Heartbeats alone do not suppress REST
fallback; stale data enables polling, a newer WebSocket snapshot disables it,
gaps trigger `/book/{symbol}`, and duplicate or old sequences are ignored.
Reconnect uses bounded exponential backoff with one timer.

## 9. Simulation session control

Administrative routes are enabled by `admin_api.enabled` and are intended only
for a trusted local or simulation network. They are not production-safe public
control endpoints. The default host applied when no host is configured is
`127.0.0.1`; Docker configurations explicitly use `0.0.0.0` inside their
isolated network. CORS defaults to no allowed cross-origin callers.

| Method | Route | Behavior |
|---|---|---|
| POST | `/api/v1/session/start` | Flush previous metrics, reset engine/scheduler/sequence/latency state, start named simulation session |
| POST | `/api/v1/session/end` | Flush metrics, return final stats, end active simulation session |
| POST | `/admin/advance-time` | Inclusively process events through an absolute UTC target or positive delta |

The exchange-wide lifecycle is:

| State | Simulation behavior |
|---|---|
| `STOPPED` | Administrative start is available; FIX application ingress and time advancement are rejected |
| `STARTING` | Ingress is gated while the previous buffer is flushed and all run state is reset |
| `ACTIVE` | FIX application ingress and the configured clock mode are enabled |
| `ENDING` | Ingress is gated while persistence and final run statistics are captured |
| `FAILED` | A destructive reset failed; readiness and mutating routes remain unavailable until process restart |

`/readyz` reports `components.lifecycle` and returns 503 during transitions or
in `FAILED`. Simulation-state REST reads also return
`503 LIFECYCLE_TRANSITION_IN_PROGRESS` during `STARTING`/`ENDING`. `/healthz`
and `/metrics` remain available. FIX application messages receive session
Reject (`35=3`) with `RefMsgType` and one stable text code:
`LIFECYCLE_TRANSITION_IN_PROGRESS`, `SESSION_NOT_ACTIVE`,
`SCHEDULER_FAILED`, `LIFECYCLE_CHANGED`, or `LIFECYCLE_FAILED`. Gated messages
do not reserve `ClOrdID`, consume run identifiers, or enter run metrics.

Starting while active returns `409 SESSION_ALREADY_ACTIVE`. A successful start
flushes inactive metrics, closes WebSockets, resets scheduler/engine/latency and
run-local statistics, reseeds the RNG from `execution.random_seed`, assigns
`last_successful_generation + 1`, installs fresh identifier counters, and only
then publishes `ACTIVE`. A failure before destructive reset work restores
`STOPPED`; a failure after it begins returns `503 SESSION_START_FAILED`, enters
`FAILED`, and requires process restart.

Ending with no active session is idempotent and returns
`status="no_active_session"` with null `session_id`, `run_generation`, and
`final_stats`. A successful end returns the ended run ID, generation, and only
that run's statistics. It does not drain or clear pending scheduler events:
they remain frozen and observable in `final_stats`, cannot execute while
stopped, and are discarded by the next successful start. A failed scheduler's
dead letter is therefore retained in final statistics; end the run and start a
new one to reset it.

Lifecycle error codes are:

| HTTP | Code |
|---:|---|
| 409 | `SESSION_ALREADY_ACTIVE`, `SESSION_NOT_ACTIVE`, `TIME_REVERSAL`, `CLOCK_MODE_CONFLICT` |
| 503 | `LIFECYCLE_TRANSITION_IN_PROGRESS`, `LIFECYCLE_FAILED`, `SESSION_START_FAILED`, `SESSION_END_FAILED`, `METRICS_FLUSH_FAILED`, `SCHEDULER_FAILED` |

Absolute advancement accepts any aware offset and normalizes it to UTC:

```json
{"target_time":"1970-01-01T01:00:00.010000+01:00"}
```

Relative advancement accepts one positive bounded value:

```json
{"milliseconds":10}
```

Response:

```json
{
  "previous_time": "1970-01-01T00:00:00+00:00",
  "current_time": "1970-01-01T00:00:00.010000+00:00",
  "events_processed": 6,
  "pending_events": 0
}
```

Naive datetimes, zero/negative deltas, and ambiguous requests return HTTP 422.
Backward targets return `409 TIME_REVERSAL` without changing scheduler state.
If an eligible event fails, advancement stops at that event's simulated
timestamp and returns:

```json
{
  "detail": {
    "code": "SCHEDULER_FAILED",
    "message": "Scheduler is failed; end the current simulation session and start a new session to reset it"
  }
}
```

## 10. Metrics and observability

`GET /stats` returns a JSON-safe point-in-time view. Its order, trade, report,
latency, and persistence-failure counters are reset at every successful run
start; the connected-session value remains a live gauge. Successful end
captures the snapshot before clearing the active run tag. `GET /metrics`
exposes process-lifetime monotonic Prometheus collectors from the same
process's isolated registry on the API port; there is no separate metrics server or metrics host port.

Cancel latency is not mixed into new-order latency. `/stats.latency` exposes
`average_sim_cancel_inbound_latency_ms`,
`average_sim_cancel_engine_latency_ms`,
`average_sim_cancel_outbound_latency_ms`, and
`average_sim_cancel_end_to_end_latency_ms`. Prometheus exposes the
corresponding fixed histograms:

```text
mockx_sim_cancel_inbound_latency_seconds
mockx_sim_cancel_engine_latency_seconds
mockx_sim_cancel_outbound_latency_seconds
mockx_sim_cancel_end_to_end_latency_seconds
```

Fixed outcome counters are exposed as
`mockx_cancel_accepted_total`, `mockx_cancel_unknown_order_total`,
`mockx_cancel_already_terminal_total`, `mockx_cancel_wrong_owner_total`,
`mockx_cancel_duplicate_request_total`, and
`mockx_cancel_stale_run_generation_total`. Persisted cancel latency samples
include the run generation, ingress sequence, cancel `ClOrdID`,
`OrigClOrdID`, stable session identity, and resolved internal order ID when
available. Delivery failure does not trigger a disconnected-report retry.

Compose does not deploy a Prometheus server, Grafana, provisioning, or
prebuilt dashboards. Scrape `mockx1:8000/metrics` and
`mockx2:8000/metrics` from an independently deployed Prometheus if desired.
Grafana is intentionally not included.

Compose does deploy InfluxDB 2.7 on host port 8086. Both exchange
configurations use the same org, bucket, and environment override contract.
Metric writes are buffered; explicit session end and application shutdown
flush before closing. InfluxDB is optional to core matching readiness:
observed sink failure makes persistence `degraded` while the core service can
remain ready.

Every logical Influx record has a deterministic identity. Influx identifies a
point by its measurement, complete tag set, and timestamp, so domain IDs stored
as fields are not sufficient when several records occur at the same simulated
time. MockX assigns the following metadata centrally before a record enters the
asynchronous buffer:

| Influx value | Meaning |
|---|---|
| `run_id` tag | Stable caller-provided identity for the simulation run |
| `event_type` tag | Bounded logical type, equal to the measurement name |
| `metric_event_bucket` tag | Low-cardinality overflow bucket for dense timestamps |
| `metric_event_sequence` field | One-based, contiguous persistence order within the run |
| `event_timestamp_ns` field | Exact original simulated Unix timestamp in nanoseconds |
| `_time` | Collision-free persistence identity timestamp |

Python simulated timestamps have microsecond resolution. For the zero-based
occurrence `ordinal` at one exact simulated timestamp, MockX derives:

```text
_time = event_timestamp_ns + (ordinal % 1000)
metric_event_bucket = ordinal // 1000
```

The nanosecond tie-break therefore never crosses into the next representable
simulated timestamp. The first 1,000 records use bucket `0`; another bucket is
introduced only for each additional block of 1,000 records at that same
timestamp. `metric_event_sequence` remains a field rather than a per-event tag,
avoiding one Influx series per event. The bucket values are reused across
timestamps, so ordinary workloads add only the constant bucket value `0`.

Queries that need simulated time must use `event_timestamp_ns`, while `_time`
is the persistence identity timestamp. Queries that need authoritative
cross-measurement event order should pivot fields and sort by
`metric_event_sequence`, for example:

```flux
from(bucket: "mockx")
  |> range(start: -1h)
  |> filter(fn: (r) => r.run_id == "quickstart")
  |> pivot(
      rowKey: ["_time", "_measurement", "metric_event_bucket"],
      columnKey: ["_field"],
      valueColumn: "_value",
  )
  |> sort(columns: ["metric_event_sequence"])
```

Failed writes retain the same immutable records in the buffer. Retrying a
flush does not allocate new sequences, timestamps, or buckets. Replaying the
same logical run with the same `run_id` and inputs intentionally reproduces
the same identities; use a distinct `run_id` for a distinct logical run.
Order, trade, and latency points also retain their available domain IDs, such
as order, execution, and exchange trade IDs, as fields.

Prometheus collectors are per application instance, so multiple exchanges in
one Python process do not register duplicate global collectors.

## 11. Multi-instance deployment

| Service | Container port | Host port | Purpose |
|---|---:|---:|---|
| MockX-1 FIX | 4001 | 4001 | FIX 4.4 acceptor |
| MockX-1 API | 8000 | 8001 | REST, WebSocket, UI, `/metrics` |
| MockX-2 FIX | 4002 | 4002 | FIX 4.4 acceptor |
| MockX-2 API | 8000 | 8002 | REST, WebSocket, UI, `/metrics` |
| InfluxDB | 8086 | 8086 | Optional metric persistence |

The exchange name labels metrics and distinguishes instances. Both instances
expose the same API contract. Their latency and random-execution profiles
differ intentionally. Browser WebSocket URLs work for either mapped API port
because they use the current origin.

## 12. Configuration

Important YAML fields:

```yaml
exchange_name: MockX-Local
symbols: [BTC-USDT, ETH-USDT]
latency:
  network_ms: 0
  engine_ms: 1
execution:
  reject_prob: 0.0
  random_seed: 42
admin_api:
  host: 127.0.0.1
  port: 8001
  enabled: true
fix:
  enabled: true
scheduler:
  mode: external
  interval_ms: 10
  max_advance_ms: 86400000
  max_events_per_advance: 100000
websocket:
  heartbeat_interval_seconds: 10
  market_data_stale_seconds: 30
  fallback_poll_seconds: 2
cors_origins: []
prometheus:
  enabled: true
```

Matching consumes all immediately executable liquidity. Partial fills occur
naturally only when the book cannot fill the complete order.

`execution.random_seed` is stored explicitly and must be an integer. Every
successful start restores that seed and the epoch initial simulated time. For
the same configuration and command sequence, matching decisions, fills,
simulated timestamps, event order, and local identifier suffixes repeat across
runs. The generation prefix intentionally changes, and monotonic wall-clock
latency measurements are not part of deterministic replay.

Startup validation rejects missing fields, duplicate/empty symbols, invalid
ports, noninteger random seeds, unsupported scheduler modes, nonpositive
intervals, stale timeouts not greater than heartbeat cadence, nonpositive
event-processing limits, wildcard CORS, and incomplete enabled TSDB
configuration.

`CFG_PATH` and `FIX_CONFIG_FILE` select the YAML and QuickFIX files.
`INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, and `INFLUXDB_BUCKET`
override YAML and can enable the configured persistence contract in either
instance.

## 13. Examples

The supported order example interface is:

```text
python examples/submit_orders.py --help
python examples/submit_orders.py --fix-config examples/fix_client.cfg
```

It waits for FIX logon, creates unique IDs within the run, submits three
orders, prints acknowledgements/fills, times out with a nonzero exit status on
connection or report failure, and always stops the initiator. It does not
advance MockX implicitly; use the time-control API in external mode.

For an in-process contract check without external services, run:

```bash
python -m pytest -q tests/test_readme_examples.py
```

## 14. Known limitations

- No production authentication, TLS termination, or reverse-proxy config is
  included. Disable admin routes or bind them to a trusted interface.
- No guaranteed WebSocket replay or sequence persistence across restarts.
- No distributed WebSocket fan-out or message broker.
- No FIX resend/sequence-recovery redesign.
- No replace workflow, stop orders, or new order types beyond limit/market.
- FIX market-data requests are rejected; use REST/WebSocket.
- No Prometheus server, Grafana, dashboard assets, OpenTelemetry, or cloud
  manifests are bundled.
- Floating quantities remain for compatibility; integer lots are deferred.

## 15. Troubleshooting

`/healthz` succeeds but `/readyz` returns 503:

- Inspect `components` in the readiness response.
- Confirm the QuickFIX loop handoff and acceptor startup.
- Validate symbols, scheduler mode, and configuration paths.
- If `components.scheduler` is `failed`, inspect
  `/stats.scheduler.dead_letter`, end the active simulation session, and start
  a new session. Do not retry the dead-lettered event.

FIX client logs on but receives no reports:

- The default clock is external.
- Call `/admin/advance-time` far enough to cover inbound, engine, and
  session-specific delays.
- Verify the target component and FIX port match the chosen instance.

The browser says `HTTP fallback` while heartbeats continue:

- This is expected when no valid market-data message arrived within the stale
  timeout.
- REST polling keeps the book current until a newer WebSocket sequence arrives.

Session end returns `METRICS_FLUSH_FAILED`:

- The buffered records were retained rather than discarded.
- Restore the configured InfluxDB sink and retry session end or shutdown.

Validate the repository with:

```bash
python -m compileall -q src tests examples
python -m pytest -q
docker compose config
```

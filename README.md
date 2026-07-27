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
- Session-scoped `ClOrdID` identity and execution-report routing.
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
- `ClOrdID` uniqueness is scoped to the stable FIX session, not the process.
- A cancel can affect only the owning session's live order.

Real matching and serialization durations use a monotonic wall clock.
Configured network, engine, and end-to-end latency values use simulated time.
The two clock domains are reported with distinct units.

## 3. Architecture

`MockExchange` owns one scheduler, engine, metrics collector, FIX application,
optional FIX acceptor, `ExchangeAPI`, and FastAPI app. The retained references
are:

```python
exchange.api                 # ExchangeAPI
exchange.app                 # exchange.api.app
exchange.app.state.exchange_api
exchange.app.state.engine
exchange.app.state.scheduler
exchange.app.state.metrics
exchange.app.state.session_registry
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
the simulated clock. Submitting a FIX order queues events; it does not process
them until time advances.

The optional `realtime` mode advances simulated time by
`scheduler.interval_ms` after each non-busy asyncio interval. Event ordering
remains deterministic at a given simulated timestamp, but the relationship to
machine scheduling is necessarily nondeterministic. Manual advancement returns
`409 CLOCK_MODE_CONFLICT` in realtime mode.

Market-data timestamps, fills, reports, and scheduler targets are always
timezone-aware simulated UTC. Wall-clock time is not used as simulated time.

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

`/stats` separates orders, trades, reports, sessions, book gauges, latency, and
persistence. Latency field names include `_us` or `_ms`; REST reads do not
mutate counters.

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

Starting while a session is active returns `409 SESSION_ALREADY_ACTIVE`.
Ending with no active session is idempotent and returns
`status="no_active_session"`. Session end does not drain pending scheduler
events; it preserves them for inspection until the next session start resets
the queue.

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

## 10. Metrics and observability

`GET /stats` returns a JSON-safe point-in-time view. `GET /metrics` exposes the
same process's isolated Prometheus registry on the API port; there is no
separate metrics server or metrics host port.

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

Startup validation rejects missing fields, duplicate/empty symbols, invalid
ports, unsupported scheduler modes, nonpositive intervals, stale timeouts not
greater than heartbeat cadence, wildcard CORS, and incomplete enabled TSDB
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

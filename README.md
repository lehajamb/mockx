# MockX

MockX is a deterministic, FIX-speaking mock exchange for strategy tests and
simulation. It provides a price-time-priority order book, an externally
controlled simulated clock, FIX order entry, typed REST administration,
immutable WebSocket market data, process metrics, and optional buffered
QuestDB persistence. An experimental downstream Nasdaq ITCH 5.0 feed can be
published over MoldUDP64 multicast, with an optional GLIMPSE 5.0 recovery
service over SoupBinTCP unicast for deterministic market-data tests.

## 1. What MockX implements

- Deterministic scheduling with FIFO ordering for equal timestamps.
- Inclusive time advancement: events exactly at the requested target execute.
- Atomic order-book quantity accounting and invariant checks.
- Exact integer tick/lot arithmetic in every authoritative matching path.
- Limit and market orders with GTC and IOC lifecycle rules.
- Mandatory participant ownership and unconditional self-cross prevention.
- Stable-FIX-session-scoped `ClOrdID` identity and execution-report routing.
- Atomic simulation lifecycle transitions and generation-aware public IDs.
- One execution report per order side and per fill.
- Frozen market-data snapshots containing only scalar values.
- A global per-exchange market-data sequence.
- FastAPI liveness, readiness, REST, WebSocket, UI, stats, and Prometheus
  exposition on one API port.
- Optional acknowledged QuestDB writes that flush at session end and shutdown.
- Optional run-scoped ITCH 5.0 displayed-order output over downstream-only
  MoldUDP64, with message-count sequencing and scheduler-driven heartbeats.
- Optional process-lifetime GLIMPSE 5.0/SoupBinTCP listener providing atomic
  resting-order snapshots and bounded interrupted-spin replay.

MockX is a simulator, not a production trading venue.

## 2. Supported order and FIX behavior

MockX uses FIX 4.4 through QuickFIX, but supports a deliberately narrow message
set.

| Direction | Supported messages |
|---|---|
| Incoming | New Order Single (`35=D`), Order Cancel Request (`35=F`) |
| Special incoming | Market Data Request (`35=V`) is rejected because market data is served by REST/WebSocket |
| Outgoing | Execution Report (`35=8`), Order Cancel Reject (`35=9`), Market Data Request Reject (`35=Y`), session Reject (`35=3`), Business Message Reject (`35=j`) |

Supported orders are limit and market orders with GTC or IOC. Replace
(`OrderCancelReplaceRequest`) is unsupported.

All FIX and REST order values pass through one strict normalization boundary.
Quantity must be positive, finite, and exactly divisible by the instrument lot
size. A limit price must be positive, finite, and exactly divisible by the tick
size. Market orders must omit price. Unsupported side, order type, or TIF values
are rejected rather than defaulted; only a missing TIF defaults to GTC.
`ClOrdID` must contain 1-64 visible ASCII characters with no surrounding
whitespace.

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

### Participant ownership and self-cross prevention

Every accepted order has one immutable `participant_id`. REST callers provide
it explicitly. FIX ownership is configured by exact QuickFIX session string in
`fix.session_participants`; every session in the QuickFIX settings file must
have exactly one mapping, and several sessions may map to the same participant.
`session_alias` remains display metadata and never determines ownership.

Self-trading cannot be enabled. When an aggressor first encounters a passive
order owned by the same participant, MockX removes and cancels the passive
order's complete remainder, cancels the aggressor's complete remainder, emits
passive then aggressor cancellation Execution Reports, and stops matching that
aggressor. It allocates no trade ID and records no trade or volume. Both reports
use:

```text
SELF_TRADE_PREVENTION: canceled passive and aggressor; prevented_qty=<quantity>
```

Any earlier third-party fills remain in cumulative quantity and average price;
both cancellation reports have zero leaves. Prometheus exposes only bounded
symbol labels through `mockx_self_trade_preventions_total{symbol}` and
`mockx_self_trade_prevented_quantity_total{symbol}`. Participant identity and
both order IDs are retained only in audit/persistence fields.

Public exchange identifiers include the successful simulation-run generation:

| FIX/public field | Format |
|---|---|
| `OrderID` | `G{generation:08d}-O{counter:08d}` |
| `ExecID` | `G{generation:08d}-E{counter:08d}` |
| `TradeID` | `G{generation:08d}-T{counter:08d}` |
| cancel-reject domain ID | `G{generation:08d}-R{counter:08d}` |
| FIX delivery ID | `G{generation:08d}-D{counter:08d}` |

Engine and gateway rejection reports share the same run scope. Numeric engine
handles remain internal and are not serialized as FIX or WebSocket IDs. The
cancel-reject domain ID is an application identity used for deduplication and
observability; FIX `OrderCancelReject (35=9)` has no `ExecID`, so the ID is not
serialized on the wire. Delivery IDs are also application-internal.

Measured CPU boundaries use a monotonic clock and microseconds: match loop,
book commit, complete engine command, immutable report creation, and outbound
FIX construction. Configured inbound, engine, outbound, end-to-end, and cancel
stage delays use simulated time and milliseconds. The clock domains never
share one ambiguous metric.

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
fix_delivery_due
```

`cancel_arrived_at_exchange` applies configured inbound network latency plus
the requester's session-alias delay. `cancel_engine_process` additionally
applies configured engine latency. The report or reject enters the outbound
FIX queue when created, and `fix_delivery_due` makes its immutable record
eligible after the configured outbound latency. A successful pending cancel
advances the order version, so an older queued new-order process command is
harmless.

Equal-time events already in the scheduler keep their earlier scheduler
priority. Consequently, an already-queued match can fill an order before a
later cancel; the cancel then returns `TOO_LATE_TO_CANCEL`. Conversely, a
cancel wins only when configured timing and the explicit ingress/scheduler
ordering place its engine event first.

### Reliable FIX application delivery

Execution reports (`35=8`) and order-cancel rejects (`35=9`) enter a
process-lifetime application queue exactly once when created. Each record
retains its delivery ID, origin run generation, stable destination
`SessionID`, per-session sequence, immutable report or reject payload,
`ExecID` or cancel-reject domain ID, original simulated due time, attempt
history, last failure, and explicit state. The states are `PENDING`, `DUE`,
`RETRY_PENDING`, `DELIVERED`, and `PERMANENTLY_FAILED`.

Only the oldest non-delivered record for a stable FIX session may be
submitted. A disconnected session retains its due record without calling the
sender. Transport exceptions and `sendToTarget(...) == False` are retried at
`fix.delivery_retry_interval_ms`, with at most
`fix.delivery_max_attempts_per_cycle` sender calls in a cycle. Exhausting a
cycle leaves the record `RETRY_PENDING`; the next logon starts a fresh bounded
cycle immediately. Deterministic serialization failures become
`PERMANENTLY_FAILED` and visibly block later records for that session. One
blocked session never prevents another session from progressing.

Logon registers the replacement live transport and triggers an immediate
FIFO drain. Logout removes only that transport. Duplicate due events,
repeated logons, and transport retries reuse the original record and cannot
resubmit a record already marked `DELIVERED` inside MockX. Logical report,
trade, and reject metrics are keyed by their domain identifiers, while
delivery-attempt metrics are separate. Terminal order and cancel latency
traces remain retained until local QuickFIX submission succeeds.

Full `DELIVERED` payload records are capped by
`fix.delivery_terminal_retention_records` (default 10,000). Lifetime scalar
totals survive pruning. A minimal generation-scoped tombstone prevents a
pruned delivered payload from being enqueued again while its run can still
produce work. `PENDING`, `DUE`, `RETRY_PENDING`, `PERMANENTLY_FAILED`, and any
FIFO-blocking record are never pruned. Stable-session `ClOrdID` history is an
intentional process-lifetime exception because correct duplicate detection
requires it. Engine terminal orders, metrics deduplication sets, completed
latency traces, and unrelated delivery tombstones are cleared at their run
lifecycle boundary.

Generic session Rejects always contain positive `RefSeqNum`, `RefMsgType`,
applicable `RefTagID`, `SessionRejectReason`, and `Text`. The inbound header
sequence is captured before deferred lifecycle dispatch; QuickFIX session
state is only a fallback for an unreadable header. Well-formed unsupported
application messages, lifecycle/application unavailability, and unusable
application data use Business Message Reject with `RefMsgType`, bounded
`BusinessRejectReason`, known `RefSeqNum`, and an optional reference ID. Both
builders validate against the bundled `configs/FIX44.xml` dictionary.

This is application-level reliability, not end-to-end exactly-once FIX
delivery. MockX guarantees one immutable application record and performs no
further application retry after QuickFIX reports successful local submission.
QuickFIX remains responsible for `MsgSeqNum`, message-store persistence,
resend requests, and protocol-level duplicate/resend behavior. An ambiguous
transport outcome can therefore still produce a client-visible duplicate.
The queue is process-local and does not survive MockX or host restart.

### Experimental ITCH 5.0 / MoldUDP64 feed

When `itch.enabled` is true, `MockExchange` owns one `ITCHFeedPublisher`. A
fresh ten-byte Mold session is opened for each successful simulation run. The
session is the one- or two-byte `moldudp64.session_prefix`, padded with `_` to
two bytes, followed by the eight-digit run generation; run 1 with prefix `MX`
is `MX00000001`.

Wire fields and packet semantics follow the official
[Nasdaq TotalView-ITCH 5.0 specification](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHspecification.pdf)
and [MoldUDP64 specification](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/moldudp64.pdf).

The matching engine publishes protocol-neutral displayed-order events only
after authoritative book mutation and invariant validation. The feed maps
those events to:

| Engine event | ITCH message |
|---|---|
| A GTC limit remainder first rests | Add Order, no MPID (`A`) |
| A fill consumes a displayed passive order | Order Executed (`E`), or Executed With Price (`C`) when execution price differs |
| A displayed remainder is explicitly canceled or removed by STP | Order Delete (`D`) |

One fill produces exactly one passive-side `E` or `C`, and its Match Number is
the engine `Fill.trade_id`. Client execution reports and the engine `Trade`
observer remain separate and never cause extra ITCH messages. Market and IOC
remainders never rest, and a partially filled GTC aggressor receives `A` only
for the quantity that actually rests. This version does not publish `F`, `X`,
`U`, `P`, `Q`, or `B` messages.

One internal lot is exactly one ITCH share. The configured instrument
`lot_size` remains the out-of-band conversion from that share to base units;
it is never multiplied into an ITCH share field. Price(4) is exactly
`price_ticks * tick_size * 10000`, without rounding. Enabling ITCH therefore
requires every symbol to be ASCII and at most eight bytes and every tick size
to be exactly representable at four decimal places. REST and FIX reject
quantities that cannot fit the unsigned 32-bit share field and prices above
the specified Price(4) maximum `200000.0000` before the order is scheduled.

Mold sequence 1 identifies the first ITCH message. A data packet advances the
next sequence by its Message Count; heartbeat (`Message Count=0`) and
end-of-session (`0xffff`) packets carry but do not consume that next sequence.
Large batches are split without reordering. Heartbeats use simulated scheduler
time and are emitted at the configured cadence only for an interval with no
data packet.

This is a downstream-only multicast implementation. It does not listen for
MoldUDP64 request packets, retain replay history, or serve retransmissions.
Without GLIMPSE, consumers that detect a sequence gap must establish a later
MockX run for a new coherent session. With GLIMPSE enabled, they can keep
buffering live ITCH and request a new atomic snapshot in the current coherent
run. A runtime encoder/socket failure similarly degrades only ITCH: matching
and the other interfaces remain available, `/readyz` returns HTTP 200 with
`status="degraded"`, no restart is attempted inside that run, and the next
simulation generation attempts a fresh session.

### GLIMPSE 5.0 / SoupBinTCP snapshot and recovery

When `glimpse.enabled` is true, MockX binds one process-lifetime SoupBinTCP
listener. GLIMPSE requires the real-time ITCH/MoldUDP64 feed because the final
Snapshot (`G`) value is the next ITCH message sequence at the atomic snapshot
cut. The listener remains bound while no simulation run is active, but an
authenticated login receives Soup Login Reject reason `S` until lifecycle is
`ACTIVE` and the current ITCH session is coherent.

A fresh client sends a Soup Login Request with an all-space Requested Session
and Requested Sequence Number 1. The server replies with Login Accepted and
one Soup Sequenced Data (`S`) packet per higher-level message:

1. Stock Directory (`R`) for every configured instrument, in locate-code order.
2. Stock Trading Action (`H`) with current state `T` for every instrument.
3. Unattributed Add Order (`A`) for every live resting order, using current
   leaves quantity. Instruments are ordered by locate; each book is bids
   best-to-worst/FIFO followed by asks best-to-worst/FIFO.
4. End of Snapshot (`G`) containing the 20-byte, left-space-padded decimal
   sequence of the next TotalView-ITCH message.

The engine book and Mold next sequence are copied together under the lifecycle
lock after book invariant validation. Encoding and every TCP `drain()` occur
after that lock is released. A slow or disconnected GLIMPSE client therefore
cannot delay matching, time advancement, FIX, WebSocket, or ITCH publication.

The recovery workflow is:

1. Subscribe to the MoldUDP64 feed and buffer real-time ITCH messages by
   message sequence.
2. Request a fresh GLIMPSE spin at Soup sequence 1 and build a local book from
   its `A` messages.
3. Read `N` from the final `G`, discard buffered ITCH messages below `N`, then
   apply a contiguous stream beginning with ITCH message `N`.

Soup credentials are case-insensitive fixed-width ASCII. The server handles
arbitrary TCP fragmentation/coalescing, server and client heartbeats, login and
write timeouts, logout, and clean End of Session (`Z`). If TCP fails before a
spin completes, its immutable messages remain available by named Soup session
and next expected Soup sequence until the configured TTL. Replay is bounded by
connection, session, and aggregate-byte limits; capacity exhaustion rejects a
new login with reason `S` instead of evicting a replayable spin.

MockX currently models neither dynamic halts nor Reg SHO, RPII, or operational
halt state, so GLIMPSE emits only `R`, `H`, `A`, and `G`. It deliberately uses
the same unattributed `A` contract as the real-time ITCH path.

## 3. Architecture

`MockExchange` owns one lifecycle coordinator, scheduler, engine, metrics
collector, FIX application, optional FIX acceptor, optional ITCH publisher,
optional GLIMPSE service, `ExchangeAPI`, and FastAPI app. The lifecycle coordinator's one asynchronous
lock serializes component
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
exchange.itch_feed           # ITCHFeedPublisher or None
exchange.glimpse_service      # GlimpseService or None
```

No route inspection or closure internals are used to recover ownership.

Construction creates and validates components without starting external
traffic. FastAPI lifespan startup then:

1. Marks readiness false.
2. Captures the running asyncio loop.
3. Confirms the engine, scheduler, metrics, registry, API, and callbacks.
4. Assigns the loop to the QuickFIX adapter.
5. Binds the optional GLIMPSE/SoupBinTCP listener.
6. Starts the FIX acceptor when enabled.
7. Starts the optional realtime scheduler task.
8. Enables administrative actions and marks readiness true.

Shutdown:

1. Marks readiness false and disables administrative actions.
2. Stops accepting GLIMPSE connections and revokes active spins.
3. Stops the FIX acceptor.
4. Cancels the realtime scheduler task.
5. Sends coherent ITCH end markers/end-of-session and closes its socket.
6. Stops the scheduler and closes/removes WebSockets.
7. Flushes and closes metrics persistence.
8. Clears adapter loop references and marks components stopped.

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

Docker Compose builds the MockX image and starts a complete live demo:
QuestDB 9.4.3, one MockX exchange, and the continuous two-session FIX market
driver from `examples/live_market.py`.

```bash
docker compose up --build
```

The driver starts only after QuestDB and MockX pass their health checks. It
then owns the simulation session, maintains the maker ladder, submits taker
flow, and advances the external clock continuously. Open the live book at
`http://localhost:8001/ui/BTC-USDT`.

To run in the background and follow the market cycles:

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f live-market
```

The published development endpoints are:

```bash
curl -f http://localhost:8001/readyz
curl -f http://localhost:8001/book/BTC-USDT
curl --fail --user mockx-user:mockx-password http://localhost:9003
```

Stop and remove the containers while retaining QuestDB data with:

```bash
docker compose down
```

To also delete the `questdb-data` volume and reset the demo database:

```bash
docker compose down -v
```

Open the QuestDB Web Console at `http://localhost:9000`. The bundled username
and password are development-only values and must not be reused in production.

## 7. REST API

Core routes are stable across local and Docker deployments:

| Method | Route | Contract |
|---|---|---|
| GET | `/` | Exchange name and process status |
| GET | `/healthz` | Process liveness |
| GET | `/readyz` | Traffic readiness and component states |
| GET | `/symbols` | Configured symbols |
| GET | `/book/{symbol}` | Immutable current book snapshot |
| POST | `/api/v1/orders` | Validate and enqueue one REST order |
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
      "price": "99.00",
      "qty": "1.000",
      "strategy_qty": "0.000",
      "market_qty": "1.000",
      "order_count": 1
    }
  ],
  "asks": []
}
```

Submit orders with `POST /api/v1/orders`. The request uses decimal strings so
conversion never passes through binary floating point:

```json
{
  "session_id": "REST:backtest-1",
  "participant_id": "strategy-1",
  "cl_ord_id": "ORDER-0001",
  "symbol": "BTC-USDT",
  "side": "BUY",
  "order_type": "LIMIT",
  "quantity": "0.125",
  "price": "61234.50",
  "time_in_force": "GTC"
}
```

Accepted orders return HTTP 202 with the assigned public/internal identity and
`PENDING_NEW`. Request-shape and field/value errors return a stable structured
422 body. Session-scoped duplicate `ClOrdID` and other request-level business
conflicts return 400; lifecycle conflicts remain 409. FIX maps malformed
headers to Session Reject, unusable/missing `ClOrdID` to Business Message
Reject, and identifiable semantic order failures to rejected Execution Reports.
Rejection `Text` begins with the stable validation code. NaN and infinity are
never copied into orders, JSON, metrics, or persistence.

With the instrument above, `quantity: "0.0005"` is rejected as
`OFF_LOT_QUANTITY`, `price: "61234.505"` as `OFF_TICK_PRICE`, and a MARKET
order carrying any `price` as `PRICE_NOT_ALLOWED`. Zero, negative, NaN, and
infinite values are rejected before an engine order is registered; unsupported
order types and TIF values are rejected rather than defaulted.

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
gauges, FIX delivery, ITCH/GLIMPSE delivery, latency, persistence, and scheduler state. The
process-lifetime `fix_delivery` object exposes enqueued records, queue depth,
counts by delivery state, sessions with backlog, cumulative attempts,
failures, deliveries, permanent failures, abandonments, and oldest pending
age. The `cancellations` object
contains `accepted`, `unknown_order`, `already_terminal`, `wrong_owner`,
`duplicate_request`, and `stale_run_generation`. Scheduler diagnostics contain `state`,
`pending_events`, the current-generation `failure_count`, the configured
`max_events_per_advance`, and an optional `dead_letter`. The dead letter
contains only deterministic scalar metadata captured when the event was
scheduled; it never exposes the live command object. Latency field names
include `_us` or `_ms`; REST reads do not mutate counters.
The process-lifetime `itch` object exposes enable/state, active Mold session,
next message sequence, sessions, messages, packets, heartbeats, failures, and
the last error. The process-lifetime `glimpse` object exposes listener state,
active connections, cache gauges, fresh/resumed/completed/rejected sessions,
sequenced messages, bytes, heartbeats, timeouts, failures, and the last error.

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
      "price": "100.00",
      "qty": "2.000",
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

Each valid subscription owns a stable internal connection ID, one bounded
outbound queue, and one sender task. The current immutable snapshot is pinned
into that queue before the connection becomes visible to broadcasts, so it is
always the first application message. Every later message for that socket uses
the same sender; the registry lock is never held across network I/O.

The default queue capacity is eight pending frames, excluding the frame
currently being sent. Market-data messages are complete latest-state
snapshots, so at most one later unsent snapshot is retained: a newer sequence
replaces it, while duplicate or older snapshots are dropped. At most one
heartbeat is pending; it is coalesced or dropped under congestion and cannot
overtake the initial snapshot. Snapshots may displace heartbeats, while fatal
error and close control traffic retains priority.

Broadcasting only performs bounded in-memory enqueues. A blocked socket
therefore cannot delay another symbol or client, REST work, scheduler
progression, or market-data publication. Each network send has a five-second
default timeout. A timed-out client is removed, its sender is terminated, and
a bounded close with code 1013 is attempted. Session reset concurrently
cancels all sender tasks and closes their sockets with code 1001.

The browser derives `ws://` or `wss://` and host/port from
`window.location`. It never hardcodes a container port. It tracks heartbeat and
market-data freshness separately. Heartbeats alone do not suppress REST
fallback; stale data enables polling, a newer WebSocket snapshot disables it,
and duplicate or old sequences are ignored. Because sequence is global while
each message is a complete symbol snapshot, gaps may represent activity on
other symbols and do not themselves trigger REST polling. Reconnect uses
bounded exponential backoff with one timer. All browser heartbeat, freshness,
polling, reconnect, and staleness-check timings are rendered from server YAML.

## 9. Simulation session control

Administrative routes are enabled by `admin_api.enabled` and are intended only
for a trusted local or simulation network. They are not production-safe public
control endpoints. The default host applied when no host is configured is
`127.0.0.1`; Docker configurations explicitly use `0.0.0.0` inside their
isolated network. CORS defaults to no allowed cross-origin callers.

| Method | Route | Behavior |
|---|---|---|
| POST | `/api/v1/session/start` | Flush previous metrics, reset run-local engine/scheduler/sequence/latency state, preserve and reschedule the FIX delivery backlog, start named simulation session |
| POST | `/api/v1/session/end` | Flush metrics, return final stats, end active simulation session |
| POST | `/admin/advance-time` | Inclusively process events through an absolute UTC target or positive delta |

The exchange-wide lifecycle is:

| State | Simulation behavior |
|---|---|
| `STOPPED` | Administrative start is available; FIX application ingress and time advancement are rejected |
| `STARTING` | Ingress is gated while the previous buffer is flushed and all run state is reset |
| `ACTIVE` | FIX application ingress and the configured clock mode are enabled |
| `RUN_FAILED` | Metrics were lost; trading is halted, delivery and inspection remain available, and the run must be ended and rerun |
| `ENDING` | Ingress is gated while persistence and final run statistics are captured |
| `FAILED` | A destructive reset failed; readiness and mutating routes remain unavailable until process restart |

`/readyz` reports `components.lifecycle` and returns 503 during transitions or
in `RUN_FAILED` or `FAILED`. Simulation-state REST reads also return
`503 LIFECYCLE_TRANSITION_IN_PROGRESS` during `STARTING`/`ENDING`. `/healthz`
and `/metrics` remain available. Well-formed FIX application messages gated by
the lifecycle receive Business Message Reject (`35=j`) with reason
`Application not available at this time`, their captured `RefSeqNum`,
`RefMsgType`, and one stable text code:
`LIFECYCLE_TRANSITION_IN_PROGRESS`, `SESSION_NOT_ACTIVE`,
`SCHEDULER_FAILED`, `LIFECYCLE_CHANGED`, `METRICS_INCOMPLETE`, or `LIFECYCLE_FAILED`. Gated messages
do not reserve `ClOrdID`, consume run identifiers, or enter run metrics.

Publishing `STARTING` or `ENDING` accepts a session transition. After that
point, cancellation of the caller does not interrupt the transition: its owned
task completes while the lifecycle lock remains held, then the caller receives
cancellation. Repeated cancellation does not abandon flushing, WebSocket
cleanup or feed initialization. A timeout that cancels the request can therefore
take longer than its requested duration; existing dependency timeouts still
apply. If the transition fails, the existing failure state and recovery rules
apply, and the failure is logged even when the caller can no longer receive it.

Cancellation while waiting for the lifecycle lock, or while draining final
broadcasts before `ENDING`, does not start a transition. Already scheduled
broadcasts remain owned by the exchange and continue. After a canceled request,
inspect `/readyz` and `/stats` before retrying: an accepted start may now be
active, and an accepted end may already have closed the run. Cancellation is
not a successful HTTP response and does not introduce a new API error code.

Starting while active returns `409 SESSION_ALREADY_ACTIVE`. A successful start
flushes inactive metrics, closes WebSockets, resets scheduler/engine and
run-local statistics, reseeds the RNG from `execution.random_seed`, assigns
`last_successful_generation + 1`, installs fresh run identifier counters, and
only then publishes `ACTIVE`. The process-lifetime FIX delivery queue and its
per-session sequence are not reset. Latency traces referenced by its
non-delivered records are preserved with `(run_generation, order_id)` or
`(run_generation, cancel_ingress_sequence)` keys; unrelated traces are reset.
A failure before destructive reset work restores `STOPPED`; a failure after it
begins returns `503 SESSION_START_FAILED`, enters `FAILED`, and requires
process restart. Permanent metric admission loss is a separate, recoverable
run failure: an initial-snapshot loss returns `503 METRICS_INCOMPLETE` and
retains the failed run ID/generation so it can be ended normally.

If a dependency cancels the transition task itself, startup restores `STOPPED`
before reset or enters `FAILED` after reset, closing its ITCH run and revoking
GLIMPSE run access. The GLIMPSE listener remains process-scoped. An internally
canceled end keeps the run open in `ACTIVE` or `RUN_FAILED` for retry. Interrupted
persistence writes restore their buffered batch with the same identities and
FIFO order; cancellation by itself does not mark metrics incomplete.

An enabled ITCH feed starts a fresh generation-qualified Mold session before
`ACTIVE`. Runtime feed failure is noncritical and leaves the run active with
readiness degraded. Normal session end publishes the ITCH market/system/end
markers and Mold end-of-session before closing the socket. A degraded session
is never resumed in place; only the next generation attempts recovery.

The process-lifetime GLIMPSE listener is bound during component startup and
retried once at the next run start after a bind failure. Each run enables a new
snapshot generation only after ITCH starts. Run end, reset, shutdown, or ITCH
degradation revokes the generation and cancels its active/cached spins before
ITCH end markers are published.

Ending with no active session is idempotent and returns
`status="no_active_session"` with null `session_id`, `run_generation`, and
`final_stats`. A successful end returns only the ended run's business metrics,
run persistence outcomes, and scheduler diagnostics. Process-lifetime FIX,
WebSocket, and persistence totals remain available only from `/stats`; they
are never mixed into `final_stats`. Session end does not drain or abandon
undelivered FIX records. They remain frozen while stopped; the next
start schedules old `PENDING` records at their original due time and old
`DUE`/`RETRY_PENDING` FIFO heads at the new scheduler's current time. Older
generations remain ahead of new-run records for the same stable FIX session.
Other pending scheduler events are discarded by the next start. A failed
scheduler's dead letter remains in final statistics and is reset by a
successful new start.

Lifecycle error codes are:

| HTTP | Code |
|---:|---|
| 409 | `SESSION_ALREADY_ACTIVE`, `SESSION_NOT_ACTIVE`, `TIME_REVERSAL`, `CLOCK_MODE_CONFLICT`, `RUN_REQUIRES_END` |
| 503 | `LIFECYCLE_TRANSITION_IN_PROGRESS`, `LIFECYCLE_FAILED`, `SESSION_START_FAILED`, `SESSION_END_FAILED`, `METRICS_FLUSH_FAILED`, `METRICS_INCOMPLETE`, `SCHEDULER_FAILED` |

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

`GET /stats` labels its scopes explicitly. The flat `orders`, `trades`,
`cancellations`, `reports`, `book`, and `latency` objects have
`scope="current_run"` at the response root and reset after a successful end.
`run_active` distinguishes an active run from the cleared stopped state.
`process_lifetime` contains cumulative business counters and duration
aggregates. `persistence`, `scheduler`, `fix_delivery`, `websocket`, `itch`,
and `glimpse` are separate operational sections with their own scope labels. The connected FIX
session value and delivery queue depths are current gauges, not run totals.

Successful session end captures the current-run snapshot before clearing it;
its `final_stats` never includes `process_lifetime`, lifetime persistence,
FIX-delivery, or WebSocket totals. `GET /metrics` exposes process-lifetime
monotonic Prometheus collectors from the same process's isolated registry on
the API port. Prometheus counters are never reset. There is no separate metrics server or metrics host port.

Cancel latency is not mixed into new-order latency. `/stats.latency` exposes
`average_sim_cancel_inbound_latency_ms`,
`average_sim_cancel_engine_latency_ms`,
`average_sim_cancel_outbound_latency_ms`, and
`average_sim_cancel_end_to_end_latency_ms`. Prometheus exposes the
corresponding fixed millisecond histograms:

```text
mockx_sim_cancel_inbound_latency_ms
mockx_sim_cancel_engine_latency_ms
mockx_sim_cancel_outbound_latency_ms
mockx_sim_cancel_end_to_end_latency_ms
```

Measured CPU durations are separate microsecond histograms and `/stats`
averages:

```text
mockx_match_loop_duration_us
mockx_book_commit_duration_us
mockx_engine_command_duration_us
mockx_report_creation_duration_us
mockx_serialization_duration_us
```

Fixed outcome counters are exposed as
`mockx_cancel_accepted_total`, `mockx_cancel_unknown_order_total`,
`mockx_cancel_already_terminal_total`, `mockx_cancel_wrong_owner_total`,
`mockx_cancel_duplicate_request_total`, and
`mockx_cancel_stale_run_generation_total`. Persisted cancel latency samples
include the run generation, ingress sequence, cancel `ClOrdID`,
`OrigClOrdID`, stable session identity, and resolved internal order ID when
available. Failed or disconnected terminal delivery retains its latency trace;
successful retry records delivery latency once and then removes the trace.

Scheduler observers update queue depth on every enqueue, dequeue, clear,
reset, and stop transition. `/stats.scheduler` exposes pending count, state,
dead letter, dead-letter count, and event-processing-limit failures.
Prometheus uses `mockx_scheduler_pending_events`,
`mockx_scheduler_failed`, `mockx_scheduler_dead_letters_total`, and
`mockx_scheduler_event_processing_limit_failures_total` without event IDs or
other high-cardinality labels.

FIX delivery Prometheus collectors include
`mockx_fix_delivery_enqueued_total`,
`mockx_fix_delivery_attempts_total`,
`mockx_fix_delivery_retries_total`,
`mockx_fix_delivery_failures_total`,
`mockx_fix_delivery_successes_total`,
`mockx_fix_delivery_permanent_failures_total`,
`mockx_fix_delivery_queue_depth`,
`mockx_fix_delivery_oldest_pending_age_seconds`, and
`mockx_fix_delivery_records{state="..."}`. `/readyz` exposes
`components.fix_delivery`: `ready` without a due/error backlog, `degraded`
with HTTP 200 for retry-pending or permanently failed records, and `disabled`
when FIX is disabled. A delivery backlog alone never makes core matching
unavailable.

ITCH collectors use no order, participant, symbol, or session labels:

```text
mockx_itch_sessions_started_total
mockx_itch_sessions_ended_total
mockx_itch_messages_sent_total
mockx_moldudp64_packets_sent_total
mockx_moldudp64_heartbeats_sent_total
mockx_itch_failures_total
mockx_itch_session_active
mockx_itch_degraded
mockx_moldudp64_next_sequence_number
```

GLIMPSE/SoupBinTCP collectors are also process-lifetime and have no username,
session, connection, symbol, order, or participant labels:

```text
mockx_glimpse_connections_accepted_total
mockx_glimpse_fresh_sessions_total
mockx_glimpse_resumed_sessions_total
mockx_glimpse_completed_sessions_total
mockx_glimpse_rejected_sessions_total
mockx_glimpse_sequenced_messages_total
mockx_glimpse_sequenced_bytes_total
mockx_glimpse_heartbeats_sent_total
mockx_glimpse_timeouts_total
mockx_glimpse_failures_total
mockx_glimpse_active_connections
mockx_glimpse_cached_sessions
mockx_glimpse_cached_bytes
mockx_glimpse_ready
mockx_glimpse_degraded
```

WebSocket delivery collectors are process-lifetime and intentionally have no
connection-ID labels:

```text
mockx_websocket_active_clients
mockx_websocket_outbound_queue_depth
mockx_websocket_snapshots_conflated_total
mockx_websocket_snapshots_dropped_total
mockx_websocket_slow_client_disconnects_total
mockx_websocket_send_failures_total
mockx_websocket_send_successes_total
```

The two gauges report current valid-symbol subscriptions and aggregate pending
frames; they return to zero after disconnect or session reset. `/stats`
exposes the gauges and cumulative WebSocket counts under
`websocket.scope="process_lifetime"`, outside the run-local business fields.

Compose does not deploy a Prometheus server, Grafana, provisioning, or
prebuilt dashboards. Scrape `mockx:8000/metrics` from an independently
deployed Prometheus on the Compose network if desired.
Grafana is intentionally not included.

Compose deploys QuestDB 9.4.3 with its Web Console and REST/ILP endpoint on
port 9000, PostgreSQL wire endpoint on 8812, and health endpoint on 9003. The
exchange receives credentials and writer settings through the supported
environment override contract.
Persistence admission is explicit: `ACCEPTED`,
`ACCEPTED_FLUSH_REQUIRED`, `ACCEPTED_DROPPED_OLDEST`, `REJECTED_CLOSED`,
`REJECTED_FULL`, or `FAILED`. `add_nowait()` never blocks and never reports
success after close. Rejections and evictions are counted; no record is
silently discarded. Typed flush and close results distinguish empty/already
closed, sink failure, timeout, and exact unflushed counts.

Ordinary metric producers isolate admission failures from exchange execution.
If a buffer rejects a record, a queued record is evicted, or record construction
or admission fails, the current command finishes all fills, reports and market
data updates before the lifecycle enters `RUN_FAILED`. No subsequent queued
order or cancel may trade, including another event at the same timestamp.
Unprocessed new orders receive terminal rejections; queued cancels receive
`EXCHANGE_UNAVAILABLE` (`CxlRejReason=99`) without changing their target orders.
New FIX ingress is rejected with `METRICS_INCOMPLETE`.

The book remains available for inspection. Existing FIX reports and the final
WebSocket/ITCH updates are preserved. Realtime or external clock advancement
continues to service delivery latency, retries and feeds without trading.
`POST /admin/advance-time` returns HTTP 503 for a failed run, with
`detail.code=METRICS_INCOMPLETE`, `run_failure`, the actual `current_time`,
`events_processed` and `pending_events`; it never reports a successful simulation
advance after metrics were lost.

One coalesced worker owns threshold, interval, explicit-flush, probe, and retry
signals. Queued and in-flight records and logical encoded bytes count against
the configured capacity. The shipped policy is `reject_new`, which preserves
every accepted record. Opt-in `drop_oldest` may evict only queued records and
reports `ACCEPTED_DROPPED_OLDEST`; it never evicts an in-flight or
delivery-required record. Async `add()` may wait for the configured bounded
retry attempts; `add_nowait()` immediately applies the policy. Failed batches
are restored in original order with their immutable run ID, sequence,
designated timestamp, symbols, and columns. Runtime retry uses exponential
backoff capped at `max_retry_delay_ms`.

```yaml
questdb:
  enabled: true
  url: http://questdb:9000
  health_url: http://questdb:9003
  username: mockx-user
  password: mockx-password
  flush_batch_size: 1000
  max_buffer_records: 10000
  max_buffer_bytes: 67108864
  flush_interval_ms: 1000
  retry_backoff_ms: 1000
  max_retry_delay_ms: 30000
  overflow_retry_attempts: 3
  shutdown_timeout_ms: 10000
  overflow_policy: reject_new
  request_timeout_ms: 10000
```

Both overflow policies retain their capacity limits. `reject_new` preserves
accepted rows but any rejected run record invalidates the run. `drop_oldest`
also invalidates the run on the first eviction. Size `max_buffer_records` and
`max_buffer_bytes` for the workload and recover QuestDB before rerunning;
there is no unbounded fallback queue or automatic continuation of a failed run.

Session end performs an explicit drain and fails with
`METRICS_FLUSH_FAILED` if accepted records cannot be written. A previously
active run remains active; an already failed run stays `RUN_FAILED` and cannot
resume trading. Retry session end after the sink recovers. Process shutdown
seals acceptance, stops the worker, and performs
one timeout-bounded final drain/close attempt; its result reports the exact
unflushed count rather than pretending success.

Persistence health is current state, not a sticky historical failure:
`DISABLED`, `HEALTHY`, `RETRYING`, `DEGRADED`, `UNAVAILABLE`, or `CLOSED`.
`/stats.persistence` includes last successful write, last failure,
consecutive and lifetime failures, buffered records/bytes, oldest age,
accepted/rejected/dropped outcomes, worker state, and shutdown result. A
successful write or probe clears consecutive failures and restores `HEALTHY`;
lifetime failures remain cumulative. Prometheus exposes the same state with a
fixed state label and bounded outcome labels.

Run integrity is separate from sink health. The boolean
`/stats.persistence.current_run_outcomes.integrity_complete` becomes false on
the first permanent loss and remains false until a new run starts. A transient
write failure that retains every row for retry does not invalidate integrity.
`/stats.run_failure` identifies the first loss with its code, message, run ID,
generation, simulated failure time, reason and attempted metric sequence.
While `RUN_FAILED`, `run_active` is false and `/readyz` stays 503 even after
the sink becomes healthy.

End the failed run with `POST /api/v1/session/end`. After remaining accepted
records drain successfully, the endpoint closes the run to `STOPPED` but returns
HTTP 503 with `status="session_failed"`, `detail.code="METRICS_INCOMPLETE"`,
the ended session identifiers and `final_stats`. The error message is
"Metrics were lost; this simulation must be rerun." Final statistics retain
`run_failure` and `persistence.integrity_complete=false`; successful flushing
cannot restore records rejected earlier. Starting before ending the failed run
returns `409 RUN_REQUIRES_END`. Once it is closed and persistence has recovered,
start a fresh run and rerun the input. No service restart is required.

Persistence is optional only when disabled. When enabled it is required for
traffic readiness: `/readyz` returns 503 unless persistence is `HEALTHY`.
The response separately reports `core_ready`, `traffic_ready`, and
`persistence_required`, so matching health remains visible during a QuestDB
outage. Disabled persistence is non-critical.

Every logical QuestDB row has a deterministic identity. All eight tables use
the actual event time as their designated `TIMESTAMP_NS` value and store the
same integer nanosecond value in `event_timestamp_ns`. `run_id` is a `SYMBOL`,
and `metric_event_sequence` is a one-based `LONG` allocated across all
persistence tables within the run. Identities are reserved before writer
admission and never reused, including when a writer enqueues a record and then
raises. A healthy run has contiguous sequences; a failed run may have gaps
where attempts were rejected.

Tables are daily-partitioned WAL tables with deduplication enabled on
`(timestamp, run_id, metric_event_sequence)`. Events at the same timestamp
remain distinct because their sequences differ. A retry sends the same keys,
so QuestDB upserts the replay rather than adding a duplicate. There are no
artificial timestamp offsets or overflow buckets.

MockX pre-creates `orders`, `trades`, `self_trade_prevention`, `latency`,
`fix_messages`, `fix_connections`, `book_snapshot`, and `pending_events`.
Categorical columns are `SYMBOL`; identifiers, JSON, and exact display values
are `VARCHAR`; ticks, lots, counters, and sequences are `LONG`; flags are
`BOOLEAN`; and latency values are `DOUBLE`. The bundled server disables ILP
table and column auto-creation, so an unexpected producer field fails visibly
instead of changing the schema.

Use QuestDB SQL in the Web Console to inspect a run or reconstruct global
cross-table ordering:

```sql
SELECT timestamp, run_id, metric_event_sequence, event_timestamp_ns,
       exchange, symbol, price, quantity
FROM trades
WHERE run_id = 'quickstart'
ORDER BY metric_event_sequence;

SELECT 'orders' table_name, timestamp, metric_event_sequence
FROM orders WHERE run_id = 'quickstart'
UNION ALL
SELECT 'trades', timestamp, metric_event_sequence
FROM trades WHERE run_id = 'quickstart'
ORDER BY metric_event_sequence;
```

Every committed book update is persisted as one immutable `book_snapshot`,
and run start emits an initial empty snapshot for every symbol after run
identity begins. Each record contains `sequence`, `has_bid`, `has_ask`,
explicit canonical `best_bid`/`best_ask` strings, `spread_valid`, canonical
spread/mid strings, bid/ask depth, and canonical JSON arrays containing every
current level. Bid-only, ask-only, and empty books are valid states. Consumers
select the latest complete sequence; they must not carry indexed fields from
an older two-sided/deeper record. A depth shrink therefore cannot leave stale
levels appearing current.

Failed writes retain the same immutable records in the buffer. Retrying a
flush does not allocate new sequences or timestamps. Replaying the
same logical run with the same `run_id` and inputs intentionally reproduces
the same identities; use a distinct `run_id` for a distinct logical run.
Order, trade, and latency rows also retain their available domain IDs, such as
order, execution, and exchange trade IDs, as `VARCHAR` or `LONG` columns
according to their domain type.

Prometheus collectors are per application instance, so multiple exchanges in
one Python process do not register duplicate global collectors.

## 11. Docker live-demo topology

| Service | Container port | Host port | Purpose |
|---|---:|---:|---|
| MockX FIX | 4001 | 4001 | FIX 4.4 acceptor |
| MockX API | 8000 | 8001 | REST, WebSocket, UI, `/metrics` |
| MockX GLIMPSE | disabled | disabled | Optional SoupBinTCP snapshot service |
| Live market | - | - | Two-session FIX market driver |
| QuestDB Web/REST/ILP | 9000 | 9000 | Console, SQL, and ingestion |
| QuestDB PostgreSQL | 8812 | 8812 | SQL wire protocol |
| QuestDB health | 9003 | 9003 | Process health |

Compose service discovery provides the driver with `mockx:4001` for FIX and
`http://mockx:8000` for administration. The browser-facing API stays on
`http://localhost:8001`, and browser WebSocket URLs use that current origin.
QuestDB state is stored in the named `questdb-data` volume.

## 12. Configuration

Configuration is validated once after environment overrides are merged. Models
are frozen and reject unknown fields. The following complete example is also
executed by the test suite:

<!-- validated-config-example -->
```yaml
exchange_name: MockX-Local
instruments:
  BTC-USDT:
    tick_size: "0.01"
    lot_size: "0.001"
    price_display_precision: 2
    quantity_display_precision: 3
latency:
  network_ms: 0
  engine_ms: 1
  session_delays: {}
execution:
  reject_prob: 0.0
  random_seed: 42
admin_api:
  host: 127.0.0.1
  port: 8001
  enabled: true
fix:
  enabled: true
  delivery_retry_interval_ms: 1000
  delivery_max_attempts_per_cycle: 3
  delivery_terminal_retention_records: 10000
  session_participants:
    "FIX.4.4:MOCKX1->CLIENT1": "participant-1"
scheduler:
  mode: external
  interval_ms: 10
  max_advance_ms: 86400000
  max_events_per_advance: 100000
websocket:
  heartbeat_interval_seconds: 10
  outbound_queue_capacity: 8
  send_timeout_seconds: 5
  heartbeat_stale_seconds: 30
  market_data_stale_seconds: 30
  fallback_poll_seconds: 2
  reconnect_initial_seconds: 1
  reconnect_max_seconds: 30
  staleness_check_seconds: 1
cors_origins: []
prometheus:
  enabled: true
itch:
  enabled: false
  market_category: " "
  financial_status: " "
  round_lot_size: 1
glimpse:
  enabled: false
soupbintcp:
  host: 127.0.0.1
  port: 18001
  session_prefix: GS
  credentials:
    GLIMP: CHANGE_ME
  login_timeout_ms: 30000
  write_timeout_ms: 5000
  max_connections: 64
  replay_ttl_ms: 60000
  max_cached_sessions: 32
  max_cached_bytes: 67108864
questdb:
  enabled: false
```

Validated ranges are:

| Setting | Contract |
|---|---|
| exchange/instruments | Nonempty exchange name; nonempty normalized unique symbols; positive finite tick/lot decimals; display precision 0-18 and at least the increment scale |
| ports/booleans | Ports 1-65535; booleans must be actual YAML booleans |
| latency | Every main, directional, engine, and session delay is an integer 0-2,147,483,647 ms |
| execution | Finite `reject_prob` 0-1; signed 64-bit integer seed; unknown/obsolete fields forbidden |
| scheduler | `external` or `realtime`; interval 1-60,000 ms; advance 1-86,400,000 ms; event limit 1-1,000,000 |
| WebSocket | All timing values finite and in (0, 86,400] seconds; heartbeat/market staleness exceed heartbeat; queue 1-100,000 |
| FIX delivery | Retry 1-2,147,483,647 ms; attempts 1-10,000; retained delivered payloads 1-1,000,000; enabled sessions require exact participant mappings |
| ITCH/MoldUDP64 | ITCH disabled by default; enabling requires MoldUDP64, ASCII symbols up to 8 bytes, Price(4)-compatible ticks, and an IPv4 multicast destination; prefix 1-2 ASCII bytes, heartbeat 100-60,000 ms, payload 512-65,507 bytes, TTL 0-255 |
| GLIMPSE/SoupBinTCP | GLIMPSE disabled by default; enabling requires ITCH/MoldUDP64 and SoupBinTCP; host plus port 1-65,535; case-insensitive ASCII username/password widths 1-6/1-10; session prefix 1-2 ASCII alphanumeric bytes; positive bounded timeouts, connections, replay sessions, TTL, and aggregate bytes |
| QuestDB | Batch 1-100,000; record capacity from batch size to 1,000,000; byte capacity 1-1 GiB; interval/backoff/retry/shutdown/request timeout 1-86,400,000 ms; max retry at least initial backoff; policy `reject_new` or `drop_oldest`; enabled config requires REST/ILP and health URLs; username/password are optional but must be paired |
| admin/CORS | Valid host and port; origins are unique HTTP(S) origins; wildcard is forbidden |

For example, startup fails before any component starts with a deterministic,
field-sorted error such as:

```text
execution.reject_prob: Input should be less than or equal to 1; latency.engine_ms: Input should be greater than or equal to 0
```

### Exact ticks, lots, and serialization

The instrument catalog is immutable. Boundary conversion uses `Decimal` and
rejects off-tick prices and off-lot quantities without rounding. Converted
ticks/lots must fit a signed 64-bit positive unit count. Matching comparisons,
price-level keys, FIFO lookup, fill/cancel accounting, aggregates, and
invariants use integers only. There are no float compatibility fields or
optional min/max instrument settings.

REST, WebSocket, stats, FIX numeric tags, and exact QuestDB display columns use
canonical fixed-scale decimal strings; raw ticks/lots remain internal except
for explicit persistence audit fields. Derived average prices use deterministic
round-half-even display formatting. Values such as
`0.30000000000000004` are never serialized.

Matching consumes all immediately executable eligible liquidity. Partial fills
occur naturally only when the book cannot fill the complete order.

`execution.random_seed` is stored explicitly and must be an integer. Every
successful start restores that seed and the epoch initial simulated time. For
the same configuration and command sequence, matching decisions, fills,
simulated timestamps, event order, and local identifier suffixes repeat across
runs. The generation prefix intentionally changes, and monotonic wall-clock
latency measurements are not part of deterministic replay.

Startup validation rejects missing fields, duplicate/empty instruments, invalid
ports, noninteger random seeds, unsupported scheduler modes, nonpositive
intervals or FIX delivery retry settings, stale timeouts not greater than
heartbeat cadence, invalid WebSocket queue/send/browser timing values,
nonpositive event-processing limits, wildcard CORS, and incomplete enabled
QuestDB, ITCH/MoldUDP64, or GLIMPSE/SoupBinTCP configuration.

Enable the dual real-time and recovery feeds with:

```yaml
itch:
  enabled: true
  market_category: " "
  financial_status: " "
  round_lot_size: 1
moldudp64:
  multicast_group: 239.192.0.1
  multicast_port: 18000
  session_prefix: MX
  heartbeat_interval_ms: 1000
  max_payload_bytes: 1400
  interface_address: 0.0.0.0
  ttl: 1
glimpse:
  enabled: true
soupbintcp:
  host: 127.0.0.1
  port: 18001
  session_prefix: GS
  credentials:
    GLIMP: CHANGE_ME
  login_timeout_ms: 30000
  write_timeout_ms: 5000
  max_connections: 64
  replay_ttl_ms: 60000
  max_cached_sessions: 32
  max_cached_bytes: 67108864
```

`CFG_PATH` and `FIX_CONFIG_FILE` select the YAML and QuickFIX files.
`QUESTDB_URL`, `QUESTDB_HEALTH_URL`, `QUESTDB_USERNAME`, `QUESTDB_PASSWORD`,
`QUESTDB_ENABLED`, `QUESTDB_FLUSH_BATCH_SIZE`,
`QUESTDB_MAX_BUFFER_RECORDS`, `QUESTDB_MAX_BUFFER_BYTES`,
`QUESTDB_FLUSH_INTERVAL_MS`, `QUESTDB_RETRY_BACKOFF_MS`,
`QUESTDB_MAX_RETRY_DELAY_MS`, `QUESTDB_OVERFLOW_RETRY_ATTEMPTS`,
`QUESTDB_SHUTDOWN_TIMEOUT_MS`, `QUESTDB_OVERFLOW_POLICY`, and
`QUESTDB_REQUEST_TIMEOUT_MS` override YAML. The merged result is validated by the
same model before startup. Obsolete YAML buffer keys are rejected and obsolete
environment variable names are not recognized; no compatibility aliases are
maintained.

## 13. Examples

### C++20 two-venue smart order router

[`examples/cpp_sor`](examples/cpp_sor/README.md) contains a one-shot C++20 SOR
that reuses the zero-copy GLIMPSE/ITCH feed handler for two MockX venues,
sweeps displayed depth with deterministic venue priority, sends IOC children
through two QuickFIX sessions, and aggregates terminal execution reports. Its
opt-in live CTest starts two real in-process MockX venues and verifies the
cross-venue fills and unrouted parent remainder.

The example's dedicated `docker-compose.yml` starts both venues, seeds their
books through distinct passive FIX sessions, and relays their MoldUDP64 streams
to a host `mockx_sor.exe`. HTTP is used only for demo administration and book
verification, not order entry. The stack is independent of the repository-root
live-demo Compose stack. QuickFIX C++ remains an installed dependency; the
example never downloads it during CMake configuration. The example README
includes the Docker quick start, the equivalent manual setup,
vcpkg/source-install build commands, and the complete route command.

### C++20 GLIMPSE/ITCH order book

[`examples/cpp_market_feed`](examples/cpp_market_feed/README.md) contains a
dependency-free Windows/Linux C++20 command-line feed handler. It subscribes
to MoldUDP64 before requesting a GLIMPSE snapshot, receives accepted UDP
datagrams directly into a lock-free SPSC ring, parses validated ITCH wire views
in place, maintains the full order-by-order book, and renders configurable
top-N price levels. A gap, new Mold session, malformed datagram, or ring overrun
causes an automatic fresh GLIMPSE cut and sequence restitch.

Build and test it with CMake:

```bash
cmake -S examples/cpp_market_feed -B examples/cpp_market_feed/build
cmake --build examples/cpp_market_feed/build
ctest --test-dir examples/cpp_market_feed/build --output-on-failure
```

The example README includes the required MockX ITCH/GLIMPSE configuration,
simulation-session startup, credentials, platform-specific commands, and all
CLI options.

The bounded local FIX contract-check interface is:

```text
python examples/submit_orders.py --help
python examples/submit_orders.py --fix-config examples/fix_client.cfg
```

It waits for FIX logon, creates unique IDs within the run, submits three
orders, and waits for every expected outcome. The crossing pair intentionally
demonstrates mandatory self-trade prevention; it does not produce a fill. The
client times out with a nonzero exit status on connection or report failure
and always stops the initiator. It does not advance MockX implicitly; use the
time-control API in external mode.

For a continuously moving two-participant market, start the dedicated local
exchange in terminal 1:

```bash
python -m src.mockx.cli \
  --config examples/live_exchange.yml \
  --fix-config examples/live_acceptor.cfg
```

Then start the FIX market driver in terminal 2:

```bash
python examples/live_market.py \
  --config examples/live_exchange.yml \
  --fix-config examples/live_client.cfg
```

Open `http://127.0.0.1:8001/ui/BTC-USDT`. The driver logs on distinct
`liquidity_bot` and `taker_bot` participants, starts its own named simulation
session, maintains an exact eight-level maker ladder, submits taker flow, and
advances the external clock until acknowledgements arrive. It runs until
Ctrl+C by default; `--cycles N` provides a bounded test run. It refuses to
replace an active simulation and ends only the session it started.

The Docker equivalent packages the server and driver configurations into the
image and runs the complete workflow with `docker compose up --build`, as
shown in the Docker quick start. The driver uses the internal service address
for API calls while `--ui-base-url` prints the host URL that can be opened in
a browser.

The page keeps the live 20-level tables and adds a 120-snapshot best-bid/ask
history chart plus cumulative 20-level depth. Charts load Highcharts Core
13.0.0 and its accessibility module from the Highcharts CDN; the tables remain
usable if the CDN is unavailable. Highcharts requires an appropriate license:
the bundled demo is intended only for qualifying personal or educational
non-commercial use. See the
[Highcharts license terms](https://shop.highcharts.com/license-eula).

For an in-process contract check without external services, run:

```bash
python -m pytest -q tests/test_readme_examples.py tests/test_live_demo.py
```

## 14. Known limitations

- No production authentication, TLS termination, or reverse-proxy config is
  included. SoupBinTCP uses only the protocol's simple fixed-field credentials;
  disable admin routes and bind services to a trusted interface or terminate
  TLS externally.
- GLIMPSE replay sessions are bounded and process-local. Restarting MockX loses
  interrupted spins; request a fresh sequence-1 snapshot after reconnecting.
- No guaranteed WebSocket replay or sequence persistence across restarts.
- WebSocket delivery queues are process-local; there is no distributed fan-out
  or message broker.
- The reliable outbound FIX queue is process-local; a process or host failure
  loses it. QuickFIX still owns FIX sequence numbers, message stores, resend
  requests, and protocol-level duplicate behavior. This is not
  production-grade FIX recovery or end-to-end exactly-once delivery.
- No replace workflow, stop orders, or new order types beyond limit/market.
- FIX market-data requests are rejected; use REST/WebSocket or the separately
  configured GLIMPSE/SoupBinTCP recovery service.
- No Prometheus server, Grafana, dashboard assets, OpenTelemetry, or cloud
  manifests are bundled.
- Self-trading is always prevented by canceling both complete remainders; there
  is no policy override.

## 15. Troubleshooting

`/healthz` succeeds but `/readyz` returns 503:

- Inspect `components` in the readiness response.
- Compare `core_ready`, `traffic_ready`, and `persistence_required`. Enabled
  persistence must be `HEALTHY`; a recovering/failed optional sink can make
  traffic not ready while matching remains core-ready.
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
- Inspect `/stats.fix_delivery` and `components.fix_delivery` in `/readyz`.
  A `RETRY_PENDING` head is retried immediately at the next logon; a
  `PERMANENTLY_FAILED` head requires fixing the deterministic serialization
  problem or restarting the process.

The browser says `HTTP fallback` while heartbeats continue:

- This is expected when no valid market-data message arrived within the stale
  timeout.
- REST polling keeps the book current until a newer WebSocket sequence arrives.
- A global sequence gap alone does not enable fallback because updates on other
  symbols and intentional snapshot conflation can both create such gaps.

Session end returns `METRICS_FLUSH_FAILED`:

- The buffered records were retained rather than discarded.
- Restore the configured QuestDB sink and retry session end; the active run
  remains open. Shutdown is different: it seals admission and reports its one
  final attempt and exact unflushed count in persistence diagnostics.

Validate the repository with:

```bash
python -m compileall -q src tests examples
python -m pytest -q
docker compose config
```

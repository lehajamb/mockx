# Native exchange foundation — 2026-10-14

- Required FIX, ITCH, GLIMPSE and QuestDB runtime; native E lists and G replacement.
- Immutable committed effects, one typed exchange_events journal, whole-group transactional writes and bounded pressure handling.
- Shared order-level captures, complete checkpoint markers and shared reconstruction reader.
- External Grafana/projector, fresh UUID run and wire identities; removed embedded UI/WebSockets and legacy durable histories.
- See README for intentional contract changes and fresh schema deployment. CoinAPI replay and new SOR algorithms remain subsequent work.

# MockX Documentation Changelog

## 2026-10-10 - Align current documentation with main

- Verify the README and current guides against `main@0df049c`; application
  behavior, configuration and client examples remain unchanged.
- Correct the six-state exchange lifecycle and five public interface counts,
  preserving the separate five-state FIX delivery lifecycle.
- Explain conditional readiness, permitted clock advancement and coherent
  GLIMPSE snapshots in `RUN_FAILED`, and startup recovery before and after
  destructive reset. Initial metric admission can consume a new generation
  and leave an endable failed run before feed startup.
- Describe retained FIX backlog accurately: stopping does not advance the
  clock automatically, but reconnect can immediately attempt due records.
- Clarify shared-book interactions between FIX sessions, extracted order/header
  fields, and the existing market-data interfaces in current guides.
- Correct persistence coverage and representative columns: six ordinary
  producer streams, plus two pre-created schemas without production writer
  calls. Keep operational gauges separate from persisted rows.
- Include enabled GLIMPSE on TCP 18001 and the MoldUDP64 host relay in the
  Docker topology. Describe WebSocket conflation, protected initial snapshots,
  overflow handling and configurable send timeouts.
- Document the browser's current sequence limitation: duplicate frames refresh
  freshness and disable HTTP fallback; lower WebSocket sequences can replace
  the displayed book. The browser implementation is unchanged.
- Correct intermediate partial-fill reports, cancellation/rejection causes and
  all seven cancellation outcomes. Fix the sweep to three orders across two
  price levels and the unfilled example to SELL 30 @ 99.95 against bid 99.90,
  including its visible resting ask and reset behavior. Reset and scenario
  changes invalidate suspended matching animations so they cannot mutate the
  restored book or a replacement scenario.
- Align cancel-event stages, report admission, immutable event fields and FIX
  deadline ownership. Correct the heartbeat animation to suppress the first
  check at 2000 ms and send at 4000 ms when no further data arrives.
- Keep historical changelog entries and the explicitly historical combined
  engine/scheduler page unchanged. These corrections document existing behavior;
  no application contracts are changed.

## 2026-10-10 - Make FIX the only public order-entry interface

- Remove `POST /api/v1/orders` and its request/response models from the public
  API and OpenAPI schema. The removed route returns HTTP 404; no compatibility
  alias or replacement REST order route is provided.
- Submit new orders and cancellations through the existing FIX 4.4 interface,
  retaining exact numeric validation, rejection codes, and session-configured
  participant ownership.
- Preserve REST administration, book snapshots, stats, health, and metrics,
  plus WebSocket streaming, the browser viewer, and existing examples.

## 2026-10-09 - Prevent scheduler helper clock reversal

- Make `run_until_idle()` advance only forward. Overdue events execute at the
  current simulated time with their original scheduled timestamps retained;
  newly scheduled work uses actual execution time. An empty queue preserves
  the clock.
- Preserve deterministic FIFO, the shared event budget, inclusive processing
  and fail-stop behavior. Intentional `set_time()` and generation resets remain
  unchanged. No API fields or configuration options change; administrative and
  realtime advancement continue to use `run_until()`.

## 2026-10-09 - Honor persistence retry deadlines

- Prevent producer and overflow wakeups from retrying a failed sink before its
  real monotonic deadline or extending the existing wait. Measure deadlines
  from failure completion, starting at the configured base delay and doubling
  consecutive delays to the cap without jitter.
- Apply the same automatic recovery deadline after startup health-check or
  explicit-flush failure. Successful writes clear recovery; successful health
  checks clear it only when no queued or in-flight records remain.
- Preserve immediate explicit drains, sink replacement and final shutdown
  flushing with existing timeouts. Retain FIFO, metric identities and buffered
  records across failure or cancellation; sink recovery cannot repair run-level
  metric loss. Public fields and configuration options remain unchanged.

## 2026-10-09 - Exact numeric validation and display formatting

- Reject decimal tails that are off the instrument grid instead of rounding
  them into accepted ticks or lots. Keep positive signed-64-bit unit bounds and
  stable validation codes, including for extreme decimal exponents.
- Make numeric parsing, increment scale, ITCH conversion, display formatting,
  and exact quantity/notional totals independent of ambient decimal context.
  Enforce the existing 0-18 display-precision limit when precision is inferred.
- Round displayed average prices once from the exact fill-total ratio with
  round-half-even. Keep derived internal `Decimal` averages at a fixed
  28-significant-digit, round-half-even context and document their approximation.
- Preserve unrounded UI midpoints and configured-precision persisted midpoints.
  Existing public fields, FIX tags, validation codes and configuration options
  remain unchanged.

## 2026-10-09 - Ignore obsolete FIX delivery events

- Give each armed delivery deadline an immutable command and unique schedule
  version owned by the delivery queue. Reconnect and scheduler reset invalidate
  old commands so they cannot retry early or bypass the attempt limit.
- Ignore duplicate, terminal and pruned-record commands without changing FIX
  delivery metrics or failing the scheduler. Enforce the cycle limit again at
  sender-attempt admission.
- Preserve immediate reconnect retries, configured timing, stable-session FIFO,
  immutable report identities and delivery during `RUN_FAILED`.
- Document retry deadline ownership and obsolete-event accounting. Existing FIX
  wire fields, REST schemas and configuration options remain unchanged.

## 2026-10-09 - Complete accepted lifecycle transitions despite caller cancellation

- Once `STARTING` or `ENDING` is published, keep the lifecycle lock until the
  owned transition task settles, then propagate caller cancellation. Repeated
  cancellation cannot leave a transition detached or permanently stuck.
- Preserve final broadcasts if an end request is canceled before acceptance.
- Restore interrupted persistence batches in FIFO order with unchanged metric
  identities and capacity accounting, without treating cancellation as loss.
- Apply phase-appropriate recovery if a dependency cancels the transition
  itself, preserving `RUN_FAILED` and its first integrity fault on end retries.
- Document cancellation timing, timeout implications and inspection/recovery;
  existing HTTP response schemas and error codes remain unchanged.

## 2026-10-09 - Persistence isolation and failed simulation runs

- Isolate metric admission failures so the current command completes its fills,
  execution reports, latency bookkeeping and market-data output.
- Add `RUN_FAILED`: halt further trading, reject pending commands, preserve FIX
  delivery and permit inspection until the run is ended. This state does not
  require a process restart.
- Expose sticky `integrity_complete` and a typed first-failure record. A failed
  run remains invalid after the sink recovers; session end returns HTTP 503 with
  failed final statistics and a rerun-required message after draining accepted
  rows. An unsuccessful drain keeps the run open and failed for retry.
- Reserve metric identities before admission; failed runs may contain sequence
  gaps. Preserve every-update book persistence and existing buffer limits.
- Document the new FIX cancel rejection outcome, lifecycle/API responses and
  recovery procedure in the README.

## 2026-08-02 - GLIMPSE 5.0 / SoupBinTCP snapshot recovery

- Added an optional process-lifetime GLIMPSE 5.0 listener over SoupBinTCP 4.1.
  Each fresh sequence-1 login captures the authoritative resting book and the
  current Mold next-message sequence atomically, then streams `R`, `H`, `A`,
  and final `G` messages without holding the lifecycle lock during TCP I/O.
- Added strict Soup framing, fixed-width authentication, wall-clock heartbeats,
  login/client/write timeouts, clean End of Session, and bounded interrupted-
  spin replay by named session and next expected Soup sequence.
- Added readiness, typed `/stats`, and unlabeled Prometheus observability;
  listener failure degrades only GLIMPSE and is retried at the next run start.
- Documented the dual-feed recovery algorithm and tightened Price(4) to the
  specified maximum display value `200000.0000`.

## 2026-08-02 — ITCH/MoldUDP64 architectural rework

- Added the experimental downstream-only ITCH 5.0 feed as a run-owned
  `MockExchange` subsystem. The feed publishes non-attributed Add Order (`A`),
  Order Executed (`E`), Order Executed With Price (`C`), and Delete Order (`D`)
  messages from authoritative displayed-book events.
- Documented the exact conversion contract: one internal lot is one ITCH share,
  and Price(4) is `price_ticks × tick_size × 10,000`. ITCH-enabled instruments
  must have an exactly representable tick size and all encoded values must fit
  their unsigned wire fields.
- Documented run-local Mold sessions, message-count sequence progression,
  scheduler-driven idle heartbeats, end-of-session behavior, configuration,
  readiness degradation, diagnostics, and bounded Prometheus metrics.
- Updated the scheduler animation to show `fix_delivery_due`, WebSocket
  publication after the engine update, displayed-order publication after a
  successful book mutation, and generation-qualified `itch_heartbeat` events.
- Added deterministic wire-level book-equivalence, transport, lifecycle,
  degradation, bounds, and event-isolation coverage in
  `tests/test_itch_integration.py`, `tests/test_moldudp64_transport.py`, and
  `tests/test_displayed_order_events.py`.
- Kept `doc/mockx-engine-scheduler.html` unchanged as explicitly labelled
  historical content.

## Files created

| File | Purpose |
|---|---|
| `doc/index.html` | Documentation landing page with navigation to all pages |
| `doc/mockx-architecture.html` | System architecture: component map, lifecycle, clock modes, ingress, identifiers, API surface |
| `doc/mockx-engine.html` | Matching engine: order state machine, submit→process, matching demo (3 scenarios), remainder rules, STP, cancel resolution, book accounting |
| `doc/mockx-scheduler.html` | Scheduler & delivery: event queue, order/cancel scheduling, FIX delivery queue (5 states), fail-stop, WebSocket streaming, QuestDB persistence |
| `doc/mockx-docs.css` | Shared CSS (extracted and extended from the old page) |
| `doc/mockx-docs.js` | Shared JavaScript utilities |

## Components documented

- **Exchange lifecycle**: STOPPED → STARTING → ACTIVE → ENDING → FAILED, with async-lock serialization (`lifecycle.py`)
- **LatencyScheduler**: Min-heap discrete-event queue, (timestamp, sequence) ordering (`latency.py`)
- **MatchingEngine**: Price-time FIFO matching, self-trade prevention, remainder finalization (`engine.py`)
- **OrderBook / BookLevel**: Book structure with strategy/market accounting, invariant checks (`models.py`)
- **Order**: 6-state lifecycle (PENDING_NEW, NEW, PARTIAL, FILLED, REJECTED, CANCELLED), version-based cancel protection (`models.py`)
- **FIX gateway**: FIXApplication, ingress pipeline, 4-key ordering, ingress barrier (`gateway.py`)
- **FIX delivery queue**: 5 states (PENDING, DUE, DELIVERED, RETRY_PENDING, PERMANENTLY_FAILED), FIFO per session, retry cycles (`delivery.py`)
- **WebSocket market data**: Per-symbol streaming, conflation, heartbeat, slow-client protection (`websocket_delivery.py`)
- **QuestDB persistence**: 8 tables, deduplication, coalesced worker, flush semantics (`questdb_writer.py`)
- **Metrics**: CPU durations (microseconds), simulated latencies (milliseconds), Prometheus collectors (`metrics.py`)
- **Instruments**: Integer tick/lot conversion boundary (`instruments.py`)
- **Run identification**: G{gen}-O/E/T/R/D format IDs (`identifiers.py`)
- **Configuration**: YAML with frozen models, environment overrides (`configuration.py`)

## Important differences from the old page

The old page (`doc/mockx-engine-scheduler.html`) describes an earlier version. Key differences:

### New behavior documented (not in old page)
1. **Self-Trade Prevention (STP)** — Every order carries `participant_id`. Same-participant cross → both remainders cancelled, no trade recorded, passive report first. Source: `MatchingEngine._match_order`.
2. **Cancel pipeline** — Old page describes version-based cancel protection only. Real pipeline has 4 stages: `cancel_received` → `cancel_arrived_at_exchange` → `cancel_engine_process` → `fix_delivery_due`, each with configurable latency. Source: `LatencyScheduler.register_handler`, README.
3. **FIX ingress sequencing** — Deterministic 4-key ordering: `(simulated_receive_timestamp, SessionID, MsgSeqNum, ingress_sequence)`. Source: `FIXIngressCommand.ordering_key`.
4. **FIX reliable delivery queue** — 5 explicit states, bounded retry cycles, per-session FIFO, logon-triggered drain, terminal retention pruning. Source: `delivery.py`, README.
5. **Exchange lifecycle state machine** — STOPPED/STARTING/ACTIVE/ENDING/FAILED with async-lock serialization. Old page only mentions scheduler FAILED state. Source: `lifecycle.py`.
6. **Run generation identifiers** — `G{generation:08d}-O/E/T/R/D{counter:08d}` format. Old page uses simple numeric IDs like "ORD-1042". Source: `identifiers.py`.
7. **Order book strategy/market accounting** — `BookLevel` tracks `strategy_qty_lots` and `market_qty_lots` separately via `LiquidityCategory`. Source: `BookLevel` and `OrderBook`.
8. **QuestDB persistence** — 8 tables, deduplication, daily WAL partitions, flush/retry semantics. Entirely new subsystem. Source: `questdb_writer.py`.
9. **WebSocket market data** — Per-symbol streaming, immutable snapshots, conflation, heartbeat, slow-client protection. Entirely new subsystem. Source: `websocket_delivery.py`.
10. **REST API** — `POST /api/v1/orders`, `GET /book/{symbol}`, `POST /api/v1/session/*`, `POST /admin/advance-time`, `GET /stats`, `GET /metrics`. Not in old page.
11. **Cancel resolution details** — Six `CancellationOutcome` values, eight cancel-reject conditions, session-mismatch detection. Source: `MatchingEngine.cancel_order`, `CancellationOutcome`.
12. **Rejection pipeline** — Simulated RNG rejection during `process_order()`. Source: `MatchingEngine.process_order`.
13. **Instrument validation boundary** — `OFF_TICK_PRICE`, `OFF_LOT_QUANTITY`, `PRICE_NOT_ALLOWED`, `UNKNOWN_SYMBOL` errors. Source: `validation.py`, `instruments.py`.
14. **Two-clock strict separation** — Simulated clock (`LatencyScheduler.current_time`) vs. monotonic real clock. Separate metric axes (ms vs. µs).
15. **Event processing budget** — `max_events_per_advance` (default 100,000) forms a per-advancement event budget. Exceeding it generates a dead-letter with `EVENT_PROCESSING_LIMIT_EXCEEDED`. Source: `LatencyScheduler._process_until`.

### Behavior confirmed consistent with old page
1. Two-event scheduling: `order_arrived` + `order_process` with inbound and engine latency.
2. Inbound = `network_ms // 2` (or explicit `inbound_ms`), engine latency added to second event only.
3. Passive-first execution report ordering per fill.
4. FIFO matching walk through price levels at the best price first.
5. Three remainder rules: market/IOC → cancel remainder; unfilled GTC → NEW report and rest; partial GTC → rest silently.
6. Fail-stop scheduler behavior on handler exception.
7. Sequence-based tie-breaking for equal-timestamp events.

## Key implementation assumptions

1. **All arithmetic is integer-based**: Ticks for price, lots for quantity. Decimal conversion only at the API boundary.
2. **Deterministic replay**: Same config, same seed, same arrival order → same fills.
3. **No automatic retry**: Scheduler events are never retried. Only FIX delivery records have application-level retry.
4. **Process-local**: FIX delivery queue, scheduler state, and engine state are all process-local. They do not survive process restart.
5. **Simulated time never calls sleep()**: Time advances only by explicit calls or a background asyncio task polling at `scheduler.interval_ms`.
6. **Mutable order + frozen commands**: Orders are mutable (slots-based), all commands and reports are frozen dataclasses.

## Areas verified against test suite

- Order state transitions: `tests/test_order_state.py`
- Matching engine: `tests/test_engine.py`
- Order book invariants: `tests/test_order_book_invariants.py`, `tests/test_order_book.py`
- Time in force: `tests/test_time_in_force.py`
- Cancellations: `tests/test_cancellations.py`
- Market data snapshots: `tests/test_market_data_snapshots.py`
- FIX execution reports: `tests/test_fix_execution_reports.py`
- Scheduler failures: `tests/test_scheduler_failures.py`
- Metrics: `tests/test_metrics.py`, `tests/test_latency_metrics.py`
- WebSocket delivery: `tests/test_websocket_delivery.py`
- Self-trade prevention: `tests/test_pr8_self_trade_prevention.py`
- Exact arithmetic: `tests/test_pr8_exact_arithmetic.py`

## Viewing the documentation

Open any of the HTML files directly in a browser. No build process or network dependency is required (Google Fonts will load but the layout is usable without them thanks to the system font fallbacks).

```
open doc/index.html        # macOS
start doc/index.html       # Windows
xdg-open doc/index.html    # Linux
```

All pages link to each other via the top navigation bar. The old page (`mockx-engine-scheduler.html`) is linked from the index with a note that it is a legacy reference.

## Existing documentation not modified

`doc/mockx-engine-scheduler.html` — preserved unchanged as a legacy reference.

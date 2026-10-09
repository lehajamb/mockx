# MockX Documentation Changelog

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

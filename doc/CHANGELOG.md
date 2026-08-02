# MockX Documentation Changelog

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
1. **Self-Trade Prevention (STP)** — Every order carries `participant_id`. Same-participant cross → both remainders cancelled, no trade recorded, passive report first. Source: `engine.py:274-329`.
2. **Cancel pipeline** — Old page describes version-based cancel protection only. Real pipeline has 4 stages: `cancel_received` → `cancel_arrived_at_exchange` → `cancel_engine_process` → `fix_delivery_due`, each with configurable latency. Source: `latency.py:347-374`, README.
3. **FIX ingress sequencing** — Deterministic 4-key ordering: `(simulated_receive_timestamp, SessionID, MsgSeqNum, ingress_sequence)`. Source: `models.py:336-343`.
4. **FIX reliable delivery queue** — 5 explicit states, bounded retry cycles, per-session FIFO, logon-triggered drain, terminal retention pruning. Source: `delivery.py`, README.
5. **Exchange lifecycle state machine** — STOPPED/STARTING/ACTIVE/ENDING/FAILED with async-lock serialization. Old page only mentions scheduler FAILED state. Source: `lifecycle.py`.
6. **Run generation identifiers** — `G{generation:08d}-O/E/T/R/D{counter:08d}` format. Old page uses simple numeric IDs like "ORD-1042". Source: `identifiers.py`.
7. **Order book strategy/market accounting** — `BookLevel` tracks `strategy_qty_lots` and `market_qty_lots` separately via `LiquidityCategory`. Source: `models.py:774-993`.
8. **QuestDB persistence** — 8 tables, deduplication, daily WAL partitions, flush/retry semantics. Entirely new subsystem. Source: `questdb_writer.py`.
9. **WebSocket market data** — Per-symbol streaming, immutable snapshots, conflation, heartbeat, slow-client protection. Entirely new subsystem. Source: `websocket_delivery.py`.
10. **REST API** — `POST /api/v1/orders`, `GET /book/{symbol}`, `POST /api/v1/session/*`, `POST /admin/advance-time`, `GET /stats`, `GET /metrics`. Not in old page.
11. **Cancel resolution details** — Six `CancellationOutcome` values, eight cancel-reject conditions, session-mismatch detection. Source: `engine.py:670-862`, `models.py:80-88`.
12. **Rejection pipeline** — Simulated RNG rejection during `process_order()`. Source: `engine.py:207-214`.
13. **Instrument validation boundary** — `OFF_TICK_PRICE`, `OFF_LOT_QUANTITY`, `PRICE_NOT_ALLOWED`, `UNKNOWN_SYMBOL` errors. Source: `validation.py`, `instruments.py`.
14. **Two-clock strict separation** — Simulated clock (`latency.py:181`) vs. monotonic real clock (`latency.py:124`). Separate metric axes (ms vs. µs).
15. **Event processing budget** — `max_events_per_advance` (default 100,000) forms a per-advancement event budget. Exceeding it generates a dead-letter with `EVENT_PROCESSING_LIMIT_EXCEEDED`. Source: `latency.py:37, 399-411`.

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
# REST API

MockX serves REST administration, inspection, health checks, Prometheus metrics and the browser UI on the same HTTP port as its WebSocket endpoint. The local configuration uses `http://127.0.0.1:8001`. Live orders, cancels and replacements are accepted only through [FIX Trading](https://github.com/lehajamb/mockx/wiki/FIX-Trading).

## Routes

| Method | Route | Result |
|---|---|---|
| GET | `/` | Exchange name and process status |
| GET | `/healthz` | Process liveness |
| GET | `/readyz` | Readiness and component health; 200 or 503 |
| GET | `/symbols` | Configured symbols |
| GET | `/book/{symbol}` | Immutable current symbol book |
| GET | `/stats` | Typed current-run and process-lifetime statistics |
| GET | `/metrics` | Prometheus text; 404 `METRICS_DISABLED` if disabled |
| GET | `/ui/{symbol}` | Browser book viewer |
| GET | `/openapi.json` | Generated REST schema |
| POST | `/api/v1/session/start` | Start a named simulation and reset run-local state |
| POST | `/api/v1/session/end` | Flush and end the current simulation |
| POST | `/admin/advance-time` | Advance an external simulated clock inclusively |
| WS | `/ws/{symbol}` | [WebSocket Market Data](https://github.com/lehajamb/mockx/wiki/WebSocket-Market-Data) |

The three POST routes exist only when `admin_api.enabled` is true. Disabling them does not remove read-only REST or WebSockets. No REST order-submission route exists in the API or schema.

These endpoints are intended for a trusted local/simulation network. CORS defaults to no allowed cross-origin callers. The project does not bundle production authentication, TLS termination or reverse-proxy configuration.

## Inspect a book

```bash
curl -f http://127.0.0.1:8001/book/BTC-USDT
```

An illustrative response is:

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

Prices and quantities are exact fixed-scale decimal strings. The timestamp is simulated UTC. Sequence belongs to the entire exchange, so activity on another symbol can create gaps between two observations of this symbol. Reading the book does not increment sequence or mutate counters. Unlike a WebSocket message, this response has no `type` or `trades` fields.

`GET /symbols` returns an object with a `symbols` array. Unknown symbols requested through the book or viewer routes return HTTP 404:

```json
{
  "detail": {
    "code": "UNKNOWN_SYMBOL",
    "message": "Unknown symbol: INVALID"
  }
}
```

## Start a simulation

The following shell examples use Bash quoting. PowerShell users can send the same JSON with `Invoke-RestMethod -ContentType 'application/json' -Body '<JSON>' -Method Post -Uri '<URL>'`.

```bash
curl -f -X POST http://127.0.0.1:8001/api/v1/session/start \
  -H "Content-Type: application/json" \
  -d '{"run_id":"wiki-example"}'
```

```json
{
  "status": "session_started",
  "session_id": "wiki-example",
  "run_generation": 1,
  "starting_time": "1970-01-01T00:00:00+00:00"
}
```

The actual generation is allocated by the process and need not be 1. `run_id` is the canonical request field, with length 1–128; the published implementation also accepts `session_id` as an input alias. Responses use `session_id`. Unknown request fields are rejected. Start resets the engine, scheduler, market-data sequence and random seed after flushing prior metrics; the process-lifetime FIX delivery backlog survives. Starting while already active returns `409 SESSION_ALREADY_ACTIVE`.

See [Sessions and Simulated Time](https://github.com/lehajamb/mockx/wiki/Sessions-and-Simulated-Time) for lifecycle transitions and failure recovery.

## Advance simulated time

Supply exactly one of a relative positive millisecond delta or an absolute timezone-aware timestamp:

```bash
curl -f -X POST http://127.0.0.1:8001/admin/advance-time \
  -H "Content-Type: application/json" \
  -d '{"milliseconds":10}'
```

The absolute equivalent from the initial epoch is:

```json
{"target_time":"1970-01-01T01:00:00.010000+01:00"}
```

Any aware offset is normalized to UTC. The implementation also accepts `timestamp` as an input alias for `target_time`. Relative deltas must be 1–86,400,000 ms and must not exceed `scheduler.max_advance_ms`. Manual advancement is available only in external mode; realtime mode returns `409 CLOCK_MODE_CONFLICT`.

```json
{
  "previous_time": "1970-01-01T00:00:00+00:00",
  "current_time": "1970-01-01T00:00:00.010000+00:00",
  "events_processed": 6,
  "pending_events": 0
}
```

Event counts depend on scheduled work. Events exactly at the target execute. Naive datetimes, ambiguous bodies, zero/negative deltas and extra fields fail validation. Backward absolute targets return `409 TIME_REVERSAL` without changing scheduler state. This published API does not take a run ID, generation guard or checkpoint request.

If a handler fails, advancement stops at its execution time and returns `503 SCHEDULER_FAILED`. Inspect `/stats.scheduler.dead_letter`, then end and restart the simulation. Do not retry the failed event: handler mutations are not transactionally rolled back.

## End a simulation

```bash
curl -f -X POST http://127.0.0.1:8001/api/v1/session/end
```

A successful response contains `status: "session_ended"`, `session_id`, `run_generation` and `final_stats`. Final statistics belong only to the ended run; lifetime delivery/WebSocket/persistence totals remain available through `/stats`.

Ending again is idempotent:

```json
{
  "status": "no_active_session",
  "session_id": null,
  "run_generation": null,
  "final_stats": null
}
```

A flush failure returns `503 METRICS_FLUSH_FAILED` and preserves buffered data and the active session for a retry after restoring the sink. A run that lost metrics ends with HTTP 503, `status: "session_failed"`, a structured `detail`, and final statistics including the failure. Inspect these bodies instead of assuming that every HTTP 503 means the same recovery action.

## Errors and readiness

Application errors use a structured envelope:

```json
{
  "detail": {
    "code": "SCHEDULER_FAILED",
    "message": "Scheduler is failed; end the current simulation session and start a new session to reset it"
  }
}
```

| HTTP | Common codes |
|---:|---|
| 404 | `UNKNOWN_SYMBOL`, `METRICS_DISABLED` |
| 409 | `SESSION_ALREADY_ACTIVE`, `SESSION_NOT_ACTIVE`, `TIME_REVERSAL`, `CLOCK_MODE_CONFLICT`, `RUN_REQUIRES_END` |
| 422 | `REQUEST_VALIDATION_ERROR`, `ADVANCE_TOO_LARGE` |
| 503 | `ADMIN_UNAVAILABLE`, `LIFECYCLE_TRANSITION_IN_PROGRESS`, `LIFECYCLE_FAILED`, `SESSION_START_FAILED`, `SESSION_END_FAILED`, `METRICS_FLUSH_FAILED`, `METRICS_INCOMPLETE`, `SCHEDULER_FAILED` |

Validation errors contain a field-sorted message inside `detail`; they do not expose FastAPI's default list-shaped validation body. Readiness has its own typed response rather than the generic error envelope. Compare `core_ready`, `traffic_ready`, `persistence_required` and `components`: liveness alone does not establish traffic readiness. An optional feed failure can produce HTTP 200 with degraded status when the core and required persistence remain healthy.

During `STARTING`/`ENDING`, simulation-state reads such as book, symbols, stats and UI return `503 LIFECYCLE_TRANSITION_IN_PROGRESS`. `/healthz` and `/metrics` remain available. Consult [Metrics and Persistence](https://github.com/lehajamb/mockx/wiki/Metrics-and-Persistence) for statistics scopes and diagnostic fields.

## Source references

- [Routes, request models and response models](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/src/mockx/api.py)
- [Time-control contract tests](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_time_control_api.py)
- [Session API tests](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_api_lifecycle.py)
- [Readiness tests](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_readiness.py)

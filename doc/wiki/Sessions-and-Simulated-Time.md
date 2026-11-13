# Sessions and Simulated Time

A simulation session owns the book, simulated clock, run-local metrics, and generation-qualified public identifiers. A FIX connection has its own lifetime and can reconnect across simulation runs. Use REST to manage the simulation; submit orders, cancellations, and replacements through FIX.

Administrative routes require `admin_api.enabled: true` and are intended for a trusted local or simulation network. The default bind address is `127.0.0.1`; Docker configurations explicitly bind within their container network.

## Start and end a run

Start an explicitly named run:

```http
POST /api/v1/session/start
Content-Type: application/json

{"run_id":"quickstart"}
```

A representative response is:

```json
{
  "status": "session_started",
  "session_id": "quickstart",
  "run_generation": 1,
  "starting_time": "1970-01-01T00:00:00+00:00"
}
```

Use `run_id` in new requests. Its length must be 1-128 characters; the implementation also accepts `session_id` as an input alias. Responses identify the run with `session_id`. Unknown request fields are rejected. This version has no session-start input for loading resting orders; seed liquidity through FIX.

A successful start flushes inactive metrics, closes existing WebSockets, resets engine/scheduler/run-local metrics, reseeds the RNG from `execution.random_seed`, allocates a new run generation, and initializes enabled feeds. Each symbol begins with an empty book, and an initial empty book snapshot is admitted to metrics persistence.

Use a distinct run ID for a distinct logical run. Replaying identical input under the same run ID intentionally reproduces persistence identities; it is not a way to retain two separate database runs.

End the run with an empty request body:

```http
POST /api/v1/session/end
```

Successful completion returns `status="session_ended"`, the ended identifiers, and `final_stats`. Those statistics contain the ended run's business metrics, run persistence outcomes, and scheduler diagnostics. Process-lifetime totals remain available from `/stats` and are not included in `final_stats`.

Ending when no run is open is idempotent: `status="no_active_session"` and `session_id`, `run_generation`, and `final_stats` are all null.

## Lifecycle states

| State | Allowed behavior and recovery |
|---|---|
| `STOPPED` | Start a run. New FIX application ingress and time advancement are rejected; reconnect can deliver already-due FIX backlog. |
| `STARTING` | Ingress is gated while metrics flush and state resets. Wait for completion. |
| `ACTIVE` | FIX ingress and the configured clock mode are available. |
| `RUN_FAILED` | Permanent metrics loss halted trading. Inspect, recover persistence, end the run, then rerun under a fresh session. |
| `ENDING` | Ingress is gated while persistence and final statistics complete. |
| `FAILED` | A destructive reset failed. Restart the process; mutating routes and traffic readiness remain unavailable. |

`/readyz` reports lifecycle health and returns 503 during transitions or in either failure state. Simulation-state reads also return `503 LIFECYCLE_TRANSITION_IN_PROGRESS` during `STARTING` and `ENDING`; `/healthz` and `/metrics` remain available.

A second start in `ACTIVE` returns `409 SESSION_ALREADY_ACTIVE`. A failed run must be ended before starting another (`409 RUN_REQUIRES_END`). FIX messages rejected by lifecycle gating do not claim their client IDs or enter run metrics.

## External clock mode

The shipped local configuration selects:

```yaml
scheduler:
  mode: external
  max_advance_ms: 86400000
  max_events_per_advance: 100000
```

Order submission queues work; it does not move time. Advance through a relative positive millisecond delta:

```http
POST /admin/advance-time
Content-Type: application/json

{"milliseconds":10}
```

Or through an absolute, timezone-aware target:

```json
{"target_time":"1970-01-01T01:00:00.010000+01:00"}
```

That absolute value normalizes to `1970-01-01T00:00:00.010000+00:00`. Provide exactly one of `target_time` or `milliseconds`; `timestamp` is an accepted input alias for `target_time`. Relative deltas are positive integers up to 86,400,000 and must also obey the configured `max_advance_ms`. Naive datetimes, missing/ambiguous targets, and invalid deltas return 422. Targets earlier than current time return `409 TIME_REVERSAL` without changing scheduler state.

A representative successful response is:

```json
{
  "previous_time": "1970-01-01T00:00:00+00:00",
  "current_time": "1970-01-01T00:00:00.010000+00:00",
  "events_processed": 6,
  "pending_events": 0
}
```

The event count depends on the workload. Advancement is inclusive: an event exactly at the target executes. Equal-time scheduled events retain FIFO ordering. Market-data timestamps, fills, reports, and targets use simulated UTC, not the machine's wall clock.

Administrative advancement captures and flushes pending FIX ingress before running scheduled work. In-process test clients can explicitly await `exchange.fix_app.wait_for_ingress()` to capture the active generation and ingress high-water mark. There is no public HTTP ingress-barrier endpoint.

## Realtime mode

```yaml
scheduler:
  mode: realtime
  interval_ms: 10
```

While lifecycle is `ACTIVE` or `RUN_FAILED`, each non-busy asyncio interval advances simulated time by `interval_ms`. Ordering at a given simulated timestamp is deterministic, but its relationship to host scheduling is not. Manual advancement returns `409 CLOCK_MODE_CONFLICT` in this mode. The two-venue SOR fixtures use realtime mode.

## Failure and recovery

| Condition | What to do |
|---|---|
| FIX client waits for reports in external mode | Advance simulated time after submission; check configured latency and delivery state. |
| `SCHEDULER_FAILED` | Inspect `/stats.scheduler` and its immutable dead letter. End the run, fix the cause, and start a new one. |
| `METRICS_FLUSH_FAILED` while ending | Restore the sink and retry end. The open run remains `ACTIVE` or `RUN_FAILED`. |
| `METRICS_INCOMPLETE` / `RUN_FAILED` | Restore persistence, end the failed run, and rerun input. Successful flushing cannot restore already-lost records. |
| `SESSION_START_FAILED` after destructive reset / `FAILED` | Restart the process. |
| Client canceled or timed out a lifecycle request | Inspect `/readyz` and `/stats` before retrying; the accepted transition may have completed. |

An unexpected scheduler handler exception is fail-stop: the failed event becomes a dead letter, later events remain queued, and new scheduling or time movement is rejected. Engine changes made before the exception are not rolled back. The shared advancement event budget also fails the scheduler if another eligible event remains after the configured limit; its code is `EVENT_PROCESSING_LIMIT_EXCEEDED`.

Metrics-loss `RUN_FAILED` differs from scheduler failure. If the scheduler is still running, clock advancement can drain delivery and feed work while trading stays blocked; the response is HTTP 503 with the failed-run details and actual clock position. Ending that failed run can successfully close it to `STOPPED` while returning HTTP 503, `status="session_failed"`, `METRICS_INCOMPLETE`, and its final statistics. Check the response body instead of treating every 503 as an uncompleted transition.

Once `STARTING` or `ENDING` is published, cancellation of the HTTP caller does not abandon the accepted transition. Its owned task completes under the lifecycle lock before cancellation reaches the caller. A request timeout therefore does not prove the operation was canceled.

## What survives a run boundary

The process-local FIX delivery queue, its per-session ordering, and latency traces needed by undelivered records survive resets. Session end does not drain or abandon that backlog. A reconnect can attempt already-due records while stopped; the next start reschedules outstanding work, retaining older-generation delivery order ahead of new records for the same FIX session.

The process-lifetime, stable-session-scoped `ClOrdID` namespace also survives logout/relogon and simulation reset. Reuse requires process restart, not just a new run. Run-local book state, public ID counters, market-data sequence, and RNG state reset on successful start. Existing WebSockets close and must reconnect.

Sources: [lifecycle and clock documentation](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/README.md#9-simulation-session-control), [API models and routes](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/src/mockx/api.py), [lifecycle implementation](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/src/mockx/lifecycle.py). See also [Metrics and Persistence](https://github.com/lehajamb/mockx/wiki/Metrics-and-Persistence) and [Matching Engine and Scheduler](https://github.com/lehajamb/mockx/wiki/Matching-Engine-and-Scheduler).

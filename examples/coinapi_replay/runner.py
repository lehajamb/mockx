"""One paced replay driver; FIX sockets remain the order-entry boundary."""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import asdict, dataclass
import json
import time

import httpx

from .controller import ControllerConfig, DepthLevel, MidController
from .feed import FeedObserver, TimingHistogram, timestamp_ns
from .tracker import FixIntent, PendingCommandError, ReplayLedger, TERMINAL
from .transport import FixTransport


@dataclass(frozen=True)
class ClockProjection:
    wall_ns: int
    simulated_ns: int
    rate: float

    def now_ns(self, wall_ns=None):
        now = time.monotonic_ns() if wall_ns is None else wall_ns
        return self.simulated_ns + int((now - self.wall_ns) * self.rate)

    def deadline_ns(self, simulated_ns):
        return self.wall_ns + int((simulated_ns - self.simulated_ns) / self.rate)


@dataclass(frozen=True)
class ReplayOptions:
    rate: float = 1.0
    duration: float | None = None
    timeout: float = 15.0
    max_pending: int = 4096
    max_lag_seconds: float = 1.0
    controller: bool = True
    probe: bool = False
    controller_order_quantity: str = "0.1"
    controller_total_quantity: str = "100"

    def __post_init__(self):
        import math

        if not math.isfinite(self.rate) or not 0 < self.rate <= 1:
            raise ValueError("Replay rate must be finite and in (0, 1]")
        for name in ("timeout", "max_lag_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.duration is not None and (
            not math.isfinite(self.duration) or self.duration <= 0
        ):
            raise ValueError("duration must be positive and finite")
        if (
            isinstance(self.max_pending, bool)
            or not isinstance(self.max_pending, int)
            or self.max_pending < 1
        ):
            raise ValueError("max_pending must be a positive integer")


class ReplayRunner:
    def __init__(
        self,
        source,
        instrument,
        base_url,
        fix_config,
        run_id,
        options,
        *,
        audit=None,
        native_probe=None,
        memory_samplers=None,
    ):
        self.source, self.instrument = source, instrument
        self.base_url, self.run_id, self.options = base_url, run_id, options
        self.ledger = ReplayLedger(instrument.symbol, instrument, run_id[:20] + "-R")
        roles = ["replay_buy", "replay_sell"]
        if options.controller:
            roles.append("controller")
        if options.probe:
            roles.append("strategy_bot")
        self.transport = FixTransport(fix_config, instrument, roles)
        self.feed = FeedObserver(base_url, instrument)
        self.controller = MidController(
            instrument.symbol,
            ControllerConfig(
                enabled=options.controller,
                id_prefix=run_id[:20] + "-C",
                max_order_qty_lots=instrument.quantity_to_lots(
                    options.controller_order_quantity
                ),
                max_total_qty_lots=instrument.quantity_to_lots(
                    options.controller_total_quantity
                ),
            ),
        )
        self.admin = httpx.AsyncClient(base_url=base_url, timeout=options.timeout)
        self.pending_since = {}
        self.external = {}
        self.operations = Counter()
        self.report_counts = Counter()
        self.actor_fills = Counter()
        self.dispatch_lag = TimingHistogram()
        self.release_lag = TimingHistogram()
        self.scheduler_lag = TimingHistogram()
        self.frame_lag = TimingHistogram()
        self.stats_round_trip = TimingHistogram()
        self.audit = audit
        self.native_probe = native_probe
        self.native_recovery = None
        self.memory_samplers = memory_samplers or {}
        self.generation = None
        self.processed = self.sent = self.max_pending_observed = 0
        self.last_event = None
        self.reference = source.reference_depth()
        self.accuracy = {
            "samples": 0,
            "max_mid_error_ticks": 0.0,
            "sum_mid_error_ticks": 0.0,
        }
        self.last_stats = None
        self.clock = None
        self.source_anchor = 0
        self._feed_sequence = -1
        self._probe_counter = 0
        self._probe_next = 0
        self._probe_cancel = None
        self.result = {"status": "starting", "run_id": run_id}

    async def _json(self, method, path, **kwargs):
        response = await self.admin.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def _audit(self, event_type, data):
        if self.audit is not None:
            self.audit.write(
                json.dumps({"event": event_type, **asdict(data)}, default=str) + "\n"
            )

    def _send(self, intent, *, external=False, due_ns=None):
        if len(self.pending_since) >= self.options.max_pending:
            raise RuntimeError("Replay pending FIX limit reached")
        if external:
            self.external[intent.cl_ord_id] = (
                self.external[intent.orig_cl_ord_id]
                if intent.kind == "CANCEL" and intent.orig_cl_ord_id in self.external
                else [intent, 0]
            )
        self.pending_since[intent.cl_ord_id] = time.monotonic_ns()
        self.transport.send(intent)
        if due_ns is not None:
            lag_ms = (time.monotonic_ns() - self.clock.deadline_ns(due_ns)) / 1e6
            self.dispatch_lag.add_ms(lag_ms)
            if lag_ms > self.options.max_lag_seconds * 1000:
                raise RuntimeError("FIX dispatch exceeded maximum pacing lag")
        self.sent += 1
        self.max_pending_observed = max(
            self.max_pending_observed, len(self.pending_since)
        )
        self._audit("submit", intent)

    def _reports(self):
        for report in self.transport.take_reports():
            self.report_counts[report.sender_comp_id] += 1
            self._audit("report", report)
            external = self.external.get(report.cl_ord_id)
            if external is None and report.orig_cl_ord_id:
                external = self.external.get(report.orig_cl_ord_id)
            if external is not None:
                intent, previous = external
                cumulative = self.ledger._lots(report.cum_qty, previous)
                self.actor_fills[intent.role] += max(0, cumulative - previous)
                external[1] = max(previous, cumulative)
                if report.status == "REJECTED":
                    raise RuntimeError(f"{intent.role} order rejected: {report.text}")
                if (
                    report.message_type == "OrderCancelReject"
                    and report.status not in {"FILLED", "CANCELLED"}
                ):
                    raise RuntimeError(f"{intent.role} cancel rejected: {report.text}")
                settles = report.status in TERMINAL or (
                    intent.tif != "IOC" and report.exec_type in {"0", "5", "F"}
                )
                if settles:
                    self.pending_since.pop(report.cl_ord_id, None)
                if report.status in TERMINAL:
                    self.external.pop(report.cl_ord_id, None)
                    if report.orig_cl_ord_id:
                        self.external.pop(report.orig_cl_ord_id, None)
                        self.pending_since.pop(report.orig_cl_ord_id, None)
                    if intent.role == "controller":
                        self.controller.complete(intent.cl_ord_id)
                continue
            outcome = self.ledger.record_report(report)
            if outcome is not None:
                self.pending_since.pop(outcome.intent.cl_ord_id, None)
                for previous in outcome.also_completed:
                    self.pending_since.pop(previous.cl_ord_id, None)
                expected_race = outcome.intent.kind in {
                    "CANCEL",
                    "REPLACE",
                } and outcome.status in {"FILLED", "CANCELLED"}
                if not outcome.accepted and not expected_race:
                    raise RuntimeError(
                        f"Historical FIX {outcome.intent.kind} rejected: {outcome.text}"
                    )
        if self.pending_since:
            oldest = min(self.pending_since.values())
            if time.monotonic_ns() - oldest > self.options.timeout * 1e9:
                raise TimeoutError("Timed out waiting for FIX request outcome")

    def _observe_feed(self):
        self.feed.check()
        if self.feed.sequence != self._feed_sequence:
            self._feed_sequence = self.feed.sequence
            self.controller.observe_book(
                tuple(DepthLevel(*level) for level in self.feed.bids),
                tuple(DepthLevel(*level) for level in self.feed.asks),
                self.feed.market_time_ns,
                received_ns=(
                    self.clock.now_ns() if self.clock else self.feed.market_time_ns
                ),
            )
            if self.clock is not None:
                self.frame_lag.add_ms(
                    (
                        self.feed.received_ns
                        - self.clock.deadline_ns(self.feed.market_time_ns)
                    )
                    / 1e6
                )

    def _quality_sample(self):
        bids, asks = self.reference
        if bids and asks and self.feed.bids and self.feed.asks:
            target = bids[0][0] + asks[0][0]
            actual = self.feed.bids[0][0] + self.feed.asks[0][0]
            error = abs(target - actual) / 2
            self.accuracy["samples"] += 1
            self.accuracy["sum_mid_error_ticks"] += error
            self.accuracy["max_mid_error_ticks"] = max(
                self.accuracy["max_mid_error_ticks"], error
            )
            self.accuracy["latest"] = {
                "source_time_ns": (
                    self.last_event.time_ns if self.last_event else self.source.start_ns
                ),
                "observed_simulation_time_ns": self.feed.market_time_ns,
                "target_mid_twice_ticks": target,
                "actual_mid_twice_ticks": actual,
                "source_spread_ticks": asks[0][0] - bids[0][0],
                "actual_spread_ticks": self.feed.asks[0][0] - self.feed.bids[0][0],
                "source_top10_bid_lots": sum(q for _, q in bids[:10]),
                "source_top10_ask_lots": sum(q for _, q in asks[:10]),
                "actual_top10_bid_lots": sum(q for _, q in self.feed.bids[:10]),
                "actual_top10_ask_lots": sum(q for _, q in self.feed.asks[:10]),
            }

    async def _stats(self):
        started = time.monotonic_ns()
        try:
            stats = await self._json("GET", "/stats")
        finally:
            self.stats_round_trip.add_ms((time.monotonic_ns() - started) / 1e6)
        for sampler in self.memory_samplers.values():
            sampler.sample()
        if stats["run_generation"] != self.generation or not stats["run_active"]:
            raise RuntimeError("Replay run ownership changed")
        if stats["run_failure"] or stats["scheduler"]["state"] != "RUNNING":
            raise RuntimeError(
                f"Exchange run failed: {stats['run_failure'] or stats['scheduler']}"
            )
        lag = stats["scheduler"]["realtime_lag_ms"]
        self.scheduler_lag.add_ms(lag)
        if lag > self.options.max_lag_seconds * 1000:
            raise RuntimeError(f"Exchange scheduler fell behind by {lag:.1f}ms")
        self.last_stats = stats
        return stats

    def _cancel_probe(self):
        if self._probe_cancel is None:
            return
        _, order = self._probe_cancel
        self._probe_cancel = None
        if order.cl_ord_id in self.external:
            cancel = FixIntent(
                "CANCEL",
                "strategy_bot",
                order.cl_ord_id + "-F",
                order.cl_ord_id,
                order.symbol,
                order.side,
                order.quantity_lots,
                order.price_ticks,
                "GTC",
                None,
                None,
            )
            self._send(cancel, external=True)

    def _probe(self, now):
        if not self.options.probe or not self.feed.bids or not self.feed.asks:
            return
        if self._probe_cancel and now >= self._probe_cancel[0]:
            self._cancel_probe()
        if now < self._probe_next or self._probe_cancel:
            return
        self._probe_counter += 1
        prefix = self.run_id[:16] + f"-P{self._probe_counter}"
        quantity = self.instrument.quantity_to_lots("0.0001")
        sweep = FixIntent(
            "NEW",
            "strategy_bot",
            prefix + "-IOC",
            None,
            self.instrument.symbol,
            "BUY",
            quantity,
            self.feed.asks[0][0],
            "IOC",
            None,
            None,
        )
        self._send(sweep, external=True)
        passive = FixIntent(
            "NEW",
            "strategy_bot",
            prefix + "-GTC",
            None,
            self.instrument.symbol,
            "BUY",
            quantity,
            max(1, self.feed.bids[0][0] - 100),
            "GTC",
            None,
            None,
        )
        self._send(passive, external=True)
        self._probe_cancel = (now + 500_000_000, passive)
        self._probe_next = now + 1_000_000_000

    async def run(self):
        began = time.monotonic()
        stats_task = None
        seed = []
        for index, order in enumerate(self.source.snapshot):
            clid = f"{self.run_id[:20]}-S{index}"
            self.ledger.register_seed(order, clid)
            seed.append(
                {
                    "symbol": self.instrument.symbol,
                    "side": order.side.value,
                    "price": self.instrument.format_price(order.price_ticks),
                    "quantity": self.instrument.format_quantity(order.quantity_lots),
                    "session_id": f"FIX.4.4:MOCKX_COINAPI->replay_{order.side.value.lower()}",
                    "cl_ord_id": clid,
                }
            )
        try:
            logon_started = time.monotonic()
            await self.transport.start(self.options.timeout)
            self.result["fix_logon_seconds"] = time.monotonic() - logon_started
            seed_started = time.monotonic()
            started = await self._json(
                "POST",
                "/api/v1/session/start",
                json={
                    "run_id": self.run_id,
                    "initial_orders": seed,
                },
            )
            self.result["seed_seconds"] = time.monotonic() - seed_started
            self.generation = started["run_generation"]
            seed_ids = {
                row["cl_ord_id"]: row["order_id"] for row in started["seed_orders"]
            }
            for order in self.ledger.orders.values():
                order.order_id = seed_ids[order.cl_ord_id]
            feed_started = time.monotonic()
            await self.feed.start(self.options.timeout)
            self.result["feed_snapshot_seconds"] = time.monotonic() - feed_started
            first_book_seconds = time.monotonic() - began
            if self.native_probe is not None:
                await self.native_probe.start()
                self.native_recovery = asyncio.create_task(
                    self.native_probe.request_recovery(), name="coinapi-native-recovery"
                )
            clock_deadline = time.monotonic() + self.options.timeout
            while True:
                before = time.monotonic_ns()
                stats = await self._stats()
                after = time.monotonic_ns()
                if stats["scheduler"]["realtime_lag_ms"] <= 20:
                    break
                if time.monotonic() > clock_deadline:
                    raise TimeoutError(
                        "Realtime clock did not catch up after bootstrap"
                    )
                await asyncio.sleep(0.02)
            self.scheduler_lag = TimingHistogram()
            sim = timestamp_ns(stats["scheduler"]["current_time"])
            self.clock = ClockProjection((before + after) // 2, sim, self.options.rate)
            self.source_anchor = self.clock.now_ns()
            self.result.update(
                {
                    "status": "running",
                    "run_generation": self.generation,
                    "seed_orders": len(seed),
                    "first_book_seconds": first_book_seconds,
                    "ready_seconds": time.monotonic() - began,
                    "source_start_ns": self.source.start_ns,
                    "simulation_anchor_ns": self.source_anchor,
                    "clock_sample_round_trip_ms": (after - before) / 1e6,
                }
            )
            print(
                f"Replay ready: {len(seed)} seed orders; viewer {self.base_url}/ui/{self.instrument.symbol}",
                flush=True,
            )
            self._observe_feed()
            bids, asks = self.reference
            self.controller.observe_target(
                bids[0][0] + asks[0][0] if bids and asks else None, self.source_anchor
            )
            events = iter(self.source.events())
            event = next(events, None)
            deferred = deque()
            stop_ns = (
                None
                if self.options.duration is None
                else self.source.start_ns + int(self.options.duration * 1e9)
            )
            next_stats = time.monotonic() + 0.5
            next_quality = self.source_anchor
            playback_start = self.clock.deadline_ns(self.source_anchor) / 1e9
            slice_until_ns = None
            while (
                event is not None
                or deferred
                or (slice_until_ns is not None and self.clock.now_ns() < slice_until_ns)
            ):
                if stats_task is not None and stats_task.done():
                    stats_task.result()
                    stats_task = None
                self._reports()
                self._observe_feed()
                if self.native_probe is not None:
                    self.native_probe.check()
                    if self.native_recovery.done():
                        self.native_recovery.result()
                now = self.clock.now_ns()
                # Resolve only per-order replacement dependencies; other orders continue.
                for _ in range(len(deferred)):
                    previous = deferred.popleft()
                    try:
                        intent = self.ledger.translate(previous)
                    except PendingCommandError:
                        deferred.append(previous)
                    else:
                        if intent:
                            self._send(
                                intent,
                                due_ns=self.source_anchor
                                + previous.time_ns
                                - self.source.start_ns,
                            )
                released = 0
                lag_ns = 0
                while event is not None and released < 128:
                    if stop_ns is not None and event.time_ns > stop_ns:
                        # A known later row proves this quiet interval belongs
                        # to the capture. Keep clients active until its cutoff.
                        slice_until_ns = (
                            self.source_anchor + stop_ns - self.source.start_ns
                        )
                        event = None
                        break
                    due = self.source_anchor + event.time_ns - self.source.start_ns
                    now = self.clock.now_ns()
                    if due > now:
                        break
                    lag_ns = max(0, now - due)
                    self.release_lag.add_ms(lag_ns / self.options.rate / 1e6)
                    if lag_ns / self.options.rate > self.options.max_lag_seconds * 1e9:
                        raise RuntimeError(
                            "Historical event dispatch exceeded maximum pacing lag"
                        )
                    try:
                        intent = self.ledger.translate(event)
                    except PendingCommandError:
                        deferred.append(event)
                    else:
                        if intent:
                            self._send(intent, due_ns=due)
                    self.processed += 1
                    self.operations[event.operation] += 1
                    self.last_event = event
                    self.reference = self.source.reference_depth()
                    self.controller.observe_target(event.target_mid_twice_ticks, due)
                    self._audit("source", event)
                    if len(deferred) > self.options.max_pending:
                        raise RuntimeError(
                            "Replay per-order dependency backlog exceeded its limit"
                        )
                    event = next(events, None)
                    released += 1
                now = self.clock.now_ns()
                oldest_pending_time = min(
                    (previous.time_ns for previous in deferred), default=None
                )
                if event is not None:
                    oldest_pending_time = (
                        event.time_ns
                        if oldest_pending_time is None
                        else min(oldest_pending_time, event.time_ns)
                    )
                lag_ns = (
                    max(
                        0,
                        now
                        - self.source_anchor
                        - oldest_pending_time
                        + self.source.start_ns,
                    )
                    if oldest_pending_time is not None
                    else 0
                )
                source_finished = event is None and (
                    slice_until_ns is None or now >= slice_until_ns
                )
                if source_finished:
                    self.controller.stop()
                else:
                    decision = self.controller.decide(now, replay_lag_ns=lag_ns)
                    if decision is not None:
                        self._send(decision.intent, external=True)
                    self._probe(now)
                if now >= next_quality:
                    self._quality_sample()
                    next_quality = now + 100_000_000
                if stats_task is None and time.monotonic() >= next_stats:
                    # Diagnostics observe playback without delaying order entry.
                    # Keep one bounded request and surface its result next loop.
                    stats_task = asyncio.create_task(
                        self._stats(), name="coinapi-stats"
                    )
                    print(
                        f"Replay: {self.processed} events, {len(self.pending_since)} pending, "
                        f"max dispatch lag {self.dispatch_lag.maximum_ms:.1f}ms",
                        flush=True,
                    )
                    next_stats = time.monotonic() + 1
                source_due = event is not None and (
                    self.source_anchor + event.time_ns - self.source.start_ns
                    <= self.clock.now_ns()
                )
                await asyncio.sleep(0 if released == 128 or source_due else 0.001)
            self.controller.stop()
            self.result["playback_seconds"] = time.monotonic() - playback_start
            drain_start = time.monotonic()
            if stats_task is not None:
                await stats_task
                stats_task = None
            self._cancel_probe()
            while True:
                self._reports()
                stats = await self._stats()
                if not self.pending_since and stats["fix_delivery"]["queue_depth"] == 0:
                    break
                if time.monotonic() - drain_start > self.options.timeout:
                    raise TimeoutError("Final FIX/report drain did not complete")
                await asyncio.sleep(0.01)
            await self.transport.fence(self.options.timeout)
            self._reports()
            final_book = await self._json("GET", f"/book/{self.instrument.symbol}")
            await self.feed.wait_for_snapshot(final_book, self.options.timeout)
            if self.native_probe is not None:
                await self.native_recovery
                stats = await self._stats()
                await self.native_probe.wait_for_sequence(
                    stats["itch"]["next_sequence_number"]
                )
                self.native_probe.assert_depth(self.feed.bids, self.feed.asks)
                self.result["native_feed_verified"] = True
            self._observe_feed()
            self._quality_sample()
            self.result.update(
                {
                    "status": "completed",
                    "drain_seconds": time.monotonic() - drain_start,
                    "final_book_sequence": final_book["sequence"],
                    "final_feed_verified": True,
                }
            )
        except BaseException as exc:
            self.result.update(
                {
                    "status": (
                        "interrupted"
                        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
                        else "failed"
                    ),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        finally:
            self.controller.stop()
            if stats_task is not None:
                stats_task.cancel()
                await asyncio.gather(stats_task, return_exceptions=True)
            self.result.update(
                {
                    "processed_events": self.processed,
                    "sent_requests": self.sent,
                    "source_operations": dict(self.operations),
                    "reports_by_actor": dict(self.report_counts),
                    "participating_fill_lots_by_actor": {
                        **self.ledger.filled_by_role,
                        **self.actor_fills,
                    },
                    "controller_requested_lots": self.controller.quantity_used,
                    "max_pending": self.max_pending_observed,
                    "dispatch_lag": self.dispatch_lag.summary(),
                    "release_lag": self.release_lag.summary(),
                    "scheduler_lag": self.scheduler_lag.summary(),
                    "frame_lag": self.frame_lag.summary(),
                    "stats_http_round_trip": self.stats_round_trip.summary(),
                    "feed_frames": self.feed.frames,
                    "websocket_conflated_sequences": self.feed.conflated_sequences,
                    "accuracy": self.accuracy,
                    "final_stats": self.last_stats,
                    "elapsed_seconds": time.monotonic() - began,
                    "memory": {
                        name: sampler.summary()
                        for name, sampler in self.memory_samplers.items()
                    },
                }
            )
            cleanup_errors = []
            try:
                if self.generation is not None:
                    ended = await self._json(
                        "POST",
                        "/api/v1/session/end",
                        json={"expected_run_generation": self.generation},
                    )
                    self.result["session_end"] = ended["status"]
                    self.result["ended_stats"] = ended.get("final_stats")
                    if (
                        self.native_probe is not None
                        and self.result["status"] == "completed"
                    ):
                        await self.native_probe.wait_for_end()
            except Exception as exc:
                cleanup_errors.append(f"Session end: {type(exc).__name__}: {exc}")
            finally:
                if self.native_recovery is not None:
                    self.native_recovery.cancel()
                    await asyncio.gather(self.native_recovery, return_exceptions=True)
                closers = [
                    ("WebSocket", self.feed.close),
                    ("FIX", self.transport.stop),
                    ("HTTP", self.admin.aclose),
                ]
                if self.native_probe is not None:
                    self.result["native_feed"] = self.native_probe.result()
                    closers.append(("Native observer", self.native_probe.close))
                for name, close in closers:
                    try:
                        await close()
                    except Exception as exc:
                        cleanup_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                if cleanup_errors:
                    self.result["cleanup_error"] = "; ".join(cleanup_errors)
                    if self.result["status"] == "completed":
                        self.result["status"] = "failed"
        return self.result

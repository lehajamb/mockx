"""External CSV replay orchestration using only public MockX administration/FIX."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
import heapq
import math
from pathlib import Path
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
import uuid

import httpx

from examples.fix_support import FixReport, TERMINAL_STATUSES
from src.mockx.configuration import ExchangeConfig, execution_config_fingerprint
from src.mockx.instruments import Instrument
from src.mockx.models import Side

from .controller import ControllerConfig, DepthLevel, MidController
from .source import PreparedReplay, SourceOrder, source_digest
from .tracker import FixIntent, PendingCommandError, ReplayLedger


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplayOptions:
    profile: str = "exact"
    seed_mode: str = "bulk"
    speed: float = 1.0
    timeout: float = 15.0
    max_pending: int = 10_000
    run_id: str = "coinapi-replay"
    id_prefix: str = ""

    def __post_init__(self):
        if self.profile not in {"exact", "interactive"}:
            raise ValueError("profile must be exact or interactive")
        if self.seed_mode not in {"bulk", "fix"}:
            raise ValueError("seed mode must be bulk or fix")
        if not math.isfinite(self.speed) or self.speed <= 0:
            raise ValueError("speed must be positive and finite")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if isinstance(self.max_pending, bool) or self.max_pending <= 0:
            raise ValueError("max_pending must be positive")


class AuditLog:
    """Stream run evidence instead of retaining every report in the runner."""

    def __init__(self, path: str | Path | None = None):
        self._file = Path(path).open("w", encoding="utf-8") if path else None

    def write(self, kind: str, **fields: Any) -> None:
        if self._file:
            self._file.write(json.dumps(
                {"kind": kind, "wall_ns": time.monotonic_ns(), **fields},
                default=str, separators=(",", ":"),
            ) + "\n")
            self._file.flush()

    def close(self):
        if self._file:
            self._file.close()


class ReplayRunner:
    """One application with independently scheduled source and controller work.

    The supplied HTTP client may use real sockets or an ASGI test transport.
    A transport provides async start(callback, roles, timeout), send(intent),
    and close(). Production uses NativeFixTransport, never engine calls.
    """

    def __init__(self, prepared: PreparedReplay, instrument: Instrument,
                 exchange_config: ExchangeConfig, admin: httpx.AsyncClient,
                 transport: Any, *, options: ReplayOptions | None = None,
                 controller_config: ControllerConfig | None = None,
                 audit: AuditLog | None = None):
        self.prepared, self.instrument = prepared, instrument
        self.config, self.admin, self.transport = exchange_config, admin, transport
        self.options = options or ReplayOptions()
        self.prefix = self.options.id_prefix or f"RP-{uuid.uuid4().hex[:12]}"
        self.ledger = ReplayLedger(instrument.symbol, instrument, self.prefix)
        controller_settings = controller_config or ControllerConfig()
        # FIX ClOrdID claims survive run resets in the same exchange process.
        # Controller IDs need their own run identity even with an explicit
        # source replay prefix, which is often reused when comparing scenarios.
        controller_settings = replace(
            controller_settings,
            id_prefix=f"{controller_settings.id_prefix}-{uuid.uuid4().hex[:12]}",
        )
        self.controller = MidController(instrument.symbol, controller_settings)
        self.audit = audit or AuditLog()
        self.pending: dict[str, tuple[FixIntent, int]] = {}
        self.queued: dict[str, deque] = defaultdict(deque)
        self.failures: list[str] = []
        self.tasks: list[asyncio.Task] = []
        self._clock_lock = asyncio.Lock()
        self._closed = False
        self._started_run = False
        self._generation: int | None = None
        self._base_time: datetime | None = None
        self._current_time: datetime | None = None
        self._wall_start = 0
        self._eof = False
        self._last_lag_ns = 0
        self._last_source_event: int | None = None
        self._next_due_ns: int | None = None
        self._source_due: dict[int, int] = {}
        self._samples: deque[int] = deque(maxlen=4096)
        self._all_rtt_count = 0
        self._delivery_failures_at_start = 0
        self.summary: dict[str, Any] = {
            "profile": self.options.profile, "source_sha256": prepared.source_sha256,
            "source_symbol": prepared.source_symbol, "symbol": instrument.symbol,
            "source_events": 0, "historical_requests": 0, "controller_requests": 0,
            "skipped_events": 0, "validated_checkpoints": 0, "queue_high_water": 0,
            "max_replay_lag_ns": 0, "controller_fill_qty_lots": 0,
            "controller_fill_notional_ticks_lots": 0,
            "feed_snapshots": 0, "max_feed_delay_ns": 0,
        }
        self._controller_execs: set[tuple[str, str]] = set()
        self._controller_ids: set[str] = set()

    async def _request(self, method: str, path: str, **kwargs):
        response = await self.admin.request(method, path, **kwargs)
        if not response.is_success:
            raise ReplayError(f"{method} {path}: HTTP {response.status_code}: {response.text}")
        return response.json()

    def _preflight(self):
        if self.config.scheduler.mode != "external":
            raise ReplayError("Replay owns clock advancement; configure scheduler.mode=external")
        if not self.config.fix.enabled:
            raise ReplayError("Replay requires FIX to be enabled")
        if self.options.profile == "exact":
            latency = self.config.latency
            if (self.controller.config.enabled or self.config.execution.reject_prob
                    or any((latency.network_ms, latency.inbound_ms, latency.outbound_ms,
                            latency.engine_ms, *latency.session_delays.values()))):
                raise ReplayError("Exact baseline requires controller off, zero delays and zero rejections")
            self.prepared.assert_exact_runtime_precision()

    def receive_report(self, report: FixReport):
        """Native callbacks are marshalled onto this loop by the transport."""
        try:
            self.audit.write("fix_report", report=asdict(report))
            if report.cl_ord_id in self._controller_ids:
                key = (report.sender_comp_id, report.exec_id or "")
                if report.exec_id and key in self._controller_execs:
                    return
                if report.exec_id:
                    self._controller_execs.add(key)
                if report.exec_type == "F" and report.last_qty:
                    quantity = self.instrument.quantity_to_lots(Decimal(report.last_qty))
                    price = self.instrument.price_to_ticks(Decimal(report.last_price))
                    self.summary["controller_fill_qty_lots"] += quantity
                    self.summary["controller_fill_notional_ticks_lots"] += quantity * price
                if report.status in TERMINAL_STATUSES:
                    self.controller.complete(report.cl_ord_id, now_ns=time.monotonic_ns())
                    self._completed(report.cl_ord_id, report)
                return
            self.ledger.record_report(report)
            # One source request may finish on this report. A passive fill does
            # not acknowledge its outstanding cancel/replace request.
            outcome = self.ledger.outcome(report.cl_ord_id)
            if outcome and report.cl_ord_id in self.pending:
                self._completed(report.cl_ord_id, report)
                if not outcome.accepted:
                    terminal_race = (
                        self.options.profile == "interactive"
                        and outcome.intent.kind in {"CANCEL", "REPLACE"}
                        and outcome.status in TERMINAL_STATUSES
                    )
                    if not terminal_race:
                        self.failures.append(f"{report.cl_ord_id}: {outcome.text or outcome.status}")
        except Exception as exc:
            self.failures.append(f"Report handling failed: {exc}")

    def _completed(self, clid, report):
        entry = self.pending.pop(clid, None)
        if entry:
            elapsed = max(0, (report.received_ns or time.monotonic_ns()) - entry[1])
            self._samples.append(elapsed)
            self._all_rtt_count += 1
            self.audit.write("request_complete", cl_ord_id=clid, rtt_ns=elapsed)

    def _check(self):
        if self.failures:
            raise ReplayError(self.failures[0])
        now = time.monotonic_ns()
        for intent, sent in self.pending.values():
            if now - sent > self.options.timeout * 1_000_000_000:
                raise ReplayError(f"FIX response timeout: {intent.cl_ord_id}")

    async def _send(self, intent: FixIntent):
        self._check()
        if len(self.pending) >= self.options.max_pending:
            raise ReplayError("Pending FIX request limit reached; replay cannot silently drop input")
        self.pending[intent.cl_ord_id] = (intent, time.monotonic_ns())
        if intent.role == "controller":
            self._controller_ids.add(intent.cl_ord_id)
        self.summary["controller_requests" if intent.role == "controller" else "historical_requests"] += 1
        due = self._source_due.pop(intent.event_index, None)
        drift = max(0, time.monotonic_ns() - due) if due is not None else None
        if drift is not None:
            self.summary["max_replay_lag_ns"] = max(self.summary["max_replay_lag_ns"], drift)
        self.audit.write("fix_send", intent=asdict(intent), source_cursor=intent.event_index,
                         target_cursor=self._last_source_event, due_wall_ns=due,
                         replay_drift_ns=drift, simulated_time=str(self._current_time))
        await self.transport.send(intent)

    async def _advance(self, target: datetime):
        async with self._clock_lock:
            target = max(target, self._current_time)
            step = timedelta(milliseconds=self.config.scheduler.max_advance_ms)
            while True:
                if self._generation is not None:
                    stats = await self._request("GET", "/stats")
                    if (not stats.get("run_active")
                            or stats.get("run_generation") != self._generation
                            or stats.get("run_id") != self.options.run_id):
                        raise ReplayError("Replay run generation changed or ended during clock control")
                next_time = min(target, self._current_time + step)
                result = await self._request("POST", "/admin/advance-time",
                                             json={"target_time": next_time.isoformat()})
                self._current_time = datetime.fromisoformat(result["current_time"].replace("Z", "+00:00"))
                if self._current_time >= target:
                    return
                await asyncio.sleep(0)

    def _event_time(self, event):
        return self._base_time + timedelta(microseconds=(event.time_ns - self.prepared.start_ns) // 1000)

    async def _wait_command(self, clid, target, *, advance_for_delays=False):
        deadline = time.monotonic() + self.options.timeout
        while clid in self.pending:
            self._check()
            if time.monotonic() > deadline:
                raise ReplayError(f"Timed out waiting for {clid}")
            if advance_for_delays:
                target = self._current_time + timedelta(milliseconds=min(
                    self.config.scheduler.interval_ms, self.config.scheduler.max_advance_ms,
                ))
            await self._advance(target)
            await asyncio.sleep(0.001)
        self._check()

    def _ledger_source_orders(self):
        return tuple(SourceOrder(o.source_id, Side(o.side), o.price_ticks, o.leaves_qty_lots)
                     for o in self.ledger.live_orders())

    def _book_matches_ledger(self, book):
        expected: dict[tuple[str, int], list[int]] = {}
        for order in self.ledger.live_orders():
            level = expected.setdefault((order.side, order.price_ticks), [0, 0])
            level[0] += order.leaves_qty_lots
            level[1] += 1
        actual = {}
        for side, key in (("BUY", "bids"), ("SELL", "asks")):
            for level in book[key]:
                actual[side, self.instrument.price_to_ticks(Decimal(level["price"]))] = [
                    self.instrument.quantity_to_lots(Decimal(level["qty"])), level["order_count"],
                ]
        return actual == expected

    async def _qualify(self, expected_digest: str, target):
        # Reports on distinct TCP sessions can arrive after the aggressor's
        # terminal report. Allow them to catch up without advancing source time.
        deadline = time.monotonic() + self.options.timeout
        actual = None
        while time.monotonic() < deadline:
            self._check()
            actual = source_digest(self._ledger_source_orders())
            if actual == expected_digest:
                book = await self._request("GET", f"/book/{quote(self.instrument.symbol, safe='')}")
                if self._book_matches_ledger(book):
                    self.summary["validated_checkpoints"] += 1
                    self.audit.write("checkpoint", source_cursor=self._last_source_event,
                                     digest=actual, market_sequence=book["sequence"])
                    return
            await self._advance(target)
            await asyncio.sleep(0.001)
        raise ReplayError(
            f"Exact replay mismatch at source row {self._last_source_event}: "
            f"expected {expected_digest}, observed {actual}; interval is not qualified"
        )

    async def _initialize(self, sessions):
        stats = await self._request("GET", "/stats")
        if stats.get("run_active"):
            raise ReplayError(
                f"Cannot start replay while an existing run is active: {stats.get('run_id')}"
            )
        delivery = stats.get("fix_delivery", {})
        self._delivery_failures_at_start = (delivery.get("permanently_failed", 0)
                                            + delivery.get("abandoned", 0))
        seeds = []
        for index, order in enumerate(self.prepared.snapshot):
            side = order.side.value
            seeds.append({
                "symbol": self.instrument.symbol, "side": side,
                "price": self.instrument.format_price(order.price_ticks),
                "quantity": self.instrument.format_quantity(order.quantity_lots),
                "cl_ord_id": f"{self.prefix}-S{index:012d}",
                "session_id": sessions[f"replay_{side.lower()}"],
            })
        body = {"run_id": self.options.run_id}
        if self.options.seed_mode == "bulk":
            body["snapshot"] = seeds
        result = await self._request("POST", "/api/v1/session/start", json=body)
        self._started_run, self._generation = True, result["run_generation"]
        fingerprint = execution_config_fingerprint(self.config)
        if result.get("execution_config_fingerprint") != fingerprint:
            raise ReplayError("Server execution configuration differs from the replay configuration")
        self.summary["execution_config_fingerprint"] = fingerprint
        self.audit.write("run_started", run_id=self.options.run_id,
                         generation=self._generation, execution_config_fingerprint=fingerprint,
                         seed_mapping=result.get("seeded_orders", []))
        self._base_time = datetime.fromisoformat(result["starting_time"].replace("Z", "+00:00"))
        self._current_time = self._base_time
        mapping = {row["cl_ord_id"]: row["order_id"] for row in result.get("seeded_orders", [])}
        if self.options.seed_mode == "bulk":
            if set(mapping) != {s["cl_ord_id"] for s in seeds}:
                raise ReplayError("Snapshot response did not acknowledge every seed identity")
            for order, seed in zip(self.prepared.snapshot, seeds):
                self.ledger.register_seed(order, seed["cl_ord_id"], mapping[seed["cl_ord_id"]])
        else:
            from types import SimpleNamespace
            for index, order in enumerate(self.prepared.snapshot):
                event = SimpleNamespace(index=-(index + 1), operation="ADD", source_id=order.source_id,
                                        side=order.side, price_ticks=order.price_ticks,
                                        quantity_lots=order.quantity_lots)
                intent = self.ledger.translate(event)
                await self._send(intent)
                await self._wait_command(
                    intent.cl_ord_id, self._base_time,
                    advance_for_delays=self.options.profile == "interactive",
                )
        # FIX bootstrap may have consumed simulated latency. Historical elapsed
        # time starts only after the initial book has been acknowledged.
        self._base_time = self._current_time
        await self._qualify(self.prepared.snapshot_digest, self._base_time)

    async def _exact(self):
        for event in self.prepared.events():
            self._last_source_event = event.index
            self.summary["source_events"] += 1
            self.audit.write("source_event", event=asdict(event))
            target = self._event_time(event)
            await self._advance(target)
            intent = self.ledger.translate(event)
            if intent:
                await self._send(intent)
                await self._wait_command(intent.cl_ord_id, target)
            else:
                self.summary["skipped_events"] += 1
            await self._qualify(event.expected_digest, target)

    async def _clock(self):
        while not self._closed:
            elapsed = int((time.monotonic_ns() - self._wall_start) * self.options.speed)
            await self._advance(self._base_time + timedelta(microseconds=elapsed // 1000))
            await asyncio.sleep(0.001)

    async def _flush_queued(self):
        ready = [(events[0].index, source_id) for source_id, events in self.queued.items()
                 if events]
        heapq.heapify(ready)
        while ready:
            _, source_id = heapq.heappop(ready)
            events = self.queued[source_id]
            event = events[0]
            try:
                intent = self.ledger.translate(event)
            except PendingCommandError:
                # This source awaits its own acknowledgement. Other ready
                # sources can proceed, but preserve their original row order.
                continue
            events.popleft()
            if intent:
                await self._send(intent)
            else:
                self.summary["skipped_events"] += 1
                self._source_due.pop(event.index, None)
            if events:
                heapq.heappush(ready, (events[0].index, source_id))
            else:
                del self.queued[source_id]

    async def _dispatch(self):
        while not self._closed:
            self._check()
            await self._flush_queued()
            await asyncio.sleep(0.001)

    async def _control(self):
        while not self._closed and not self._eof:
            self._check()
            historical_pending = any(i.role != "controller" for i, _ in self.pending.values())
            now = time.monotonic_ns()
            historical_due = self._next_due_ns is not None and now >= self._next_due_ns
            if not self.queued and not historical_pending and not historical_due:
                decision = self.controller.decide(now, replay_lag_ns=self._last_lag_ns)
                if decision:
                    self.audit.write("controller_decision", **asdict(decision))
                    await self._send(decision.intent)
            await asyncio.sleep(0.005)

    async def _feed(self):
        import websockets
        parsed = urlsplit(str(self.admin.base_url))
        url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                          f"/ws/{quote(self.instrument.symbol, safe='')}", "", ""))
        while not self._closed and not self._eof:
            try:
                async with websockets.connect(url, open_timeout=self.options.timeout,
                                              close_timeout=1, max_size=64 * 1024 * 1024) as socket:
                    async for raw in socket:
                        message = json.loads(raw)
                        if message.get("type") != "market_data":
                            continue
                        def levels(key):
                            return tuple(DepthLevel(
                                self.instrument.price_to_ticks(Decimal(l["price"])),
                                self.instrument.quantity_to_lots(Decimal(l["qty"])),
                            ) for l in message[key])
                        observed_ns = time.monotonic_ns()
                        stamp = datetime.fromisoformat(message["timestamp"].replace("Z", "+00:00"))
                        elapsed = stamp - self._base_time
                        simulated_ns = ((elapsed.days * 86400 + elapsed.seconds) * 1_000_000
                                        + elapsed.microseconds) * 1000
                        intended_ns = self._wall_start + int(simulated_ns / self.options.speed)
                        delay = max(0, observed_ns - intended_ns)
                        self.summary["feed_snapshots"] += 1
                        self.summary["max_feed_delay_ns"] = max(self.summary["max_feed_delay_ns"], delay)
                        self.audit.write("market_data", source_cursor=self._last_source_event,
                                         feed_delay_ns=delay, snapshot=message)
                        # Transport backlog is stale even if received just now.
                        self.controller.observe_book(levels("bids"), levels("asks"),
                                                     observed_ns - delay,
                                                     received_ns=observed_ns)
            except (OSError, websockets.exceptions.ConnectionClosed, TimeoutError) as exc:
                self.controller.observe_book((), (), time.monotonic_ns())
                self.audit.write("feed_disconnect", error=str(exc))
                await asyncio.sleep(0.1)
            else:
                # A clean WebSocket close ends iteration without raising.
                self.controller.observe_book((), (), time.monotonic_ns())
                self.audit.write("feed_disconnect", error="Connection closed")
                if not self._eof and not self._closed:
                    await asyncio.sleep(0.1)

    def _task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        def done(result):
            if not result.cancelled() and result.exception() is not None:
                self.failures.append(str(result.exception()))
        task.add_done_callback(done)

    async def _interactive(self):
        self._wall_start = time.monotonic_ns()
        bids = [o.price_ticks for o in self.prepared.snapshot if o.side == Side.BUY]
        asks = [o.price_ticks for o in self.prepared.snapshot if o.side == Side.SELL]
        self.controller.observe_target(max(bids) + min(asks) if bids and asks else None,
                                       self._wall_start)
        self._task(self._clock())
        self._task(self._dispatch())
        if self.controller.config.enabled:
            self._task(self._feed())
            self._task(self._control())
        for event in self.prepared.events():
            due = self._wall_start + int((event.time_ns - self.prepared.start_ns) / self.options.speed)
            self._next_due_ns = due
            while time.monotonic_ns() < due:
                self._check()
                await asyncio.sleep(min(0.02, max(0, (due - time.monotonic_ns()) / 1e9)))
            self._check()
            now = time.monotonic_ns()
            self._last_lag_ns = max(0, now - due)
            self.summary["max_replay_lag_ns"] = max(self.summary["max_replay_lag_ns"], self._last_lag_ns)
            self._last_source_event = event.index
            self.summary["source_events"] += 1
            self._source_due[event.index] = due
            self.audit.write("source_event", event=asdict(event), due_wall_ns=due)
            self.controller.observe_target(event.target_mid_twice_ticks, now)
            self.queued[event.source_id].append(event)
            depth = sum(map(len, self.queued.values()))
            self.summary["queue_high_water"] = max(self.summary["queue_high_water"], depth)
            if depth > self.options.max_pending:
                raise ReplayError("Replay input backlog limit reached; input was not dropped")
            # Always yield, including equal-time bursts: sockets and reports
            # must continue processing while the producer reads ahead.
            await asyncio.sleep(0)
        self._eof = True
        self.controller.stop()
        deadline = time.monotonic() + self.options.timeout
        while self.pending or self.queued:
            self._check()
            if time.monotonic() > deadline:
                raise ReplayError("End-of-file drain timed out")
            await asyncio.sleep(0.001)

    async def _drain_reports(self):
        """Drain outbound latency, then fence each owned FIX receive stream.

        An aggressive terminal report doesn't prove the other session has
        received its passive fill. Standard TestRequest/Heartbeat barriers,
        after the server queue empties, establish that delivery ordering.
        """
        deadline = time.monotonic() + self.options.timeout
        while True:
            self._check()
            stats = await self._request("GET", "/stats")
            if (not stats.get("run_active") or stats.get("run_generation") != self._generation
                    or stats.get("run_id") != self.options.run_id):
                raise ReplayError("Replay run generation changed during report drain")
            delivery = stats.get("fix_delivery", {})
            failed = delivery.get("permanently_failed", 0) + delivery.get("abandoned", 0)
            if failed > self._delivery_failures_at_start:
                raise ReplayError("FIX delivery failed during replay; results are incomplete")
            if not delivery.get("queue_depth", 0):
                break
            if time.monotonic() >= deadline:
                raise ReplayError("FIX outbound delivery drain timed out")
            await self._advance(self._current_time)
            await asyncio.sleep(0.001)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReplayError("FIX report drain timed out")
        await asyncio.wait_for(self.transport.drain(remaining), remaining)
        self._check()
        self.audit.write("reports_drained", generation=self._generation)

    async def run(self):
        self._preflight()
        roles = ["replay_buy", "replay_sell"]
        if self.controller.config.enabled:
            roles.append("controller")
        self.audit.write("manifest", options=asdict(self.options),
                         controller=asdict(self.controller.config),
                         source_sha256=self.prepared.source_sha256,
                         source_symbol=self.prepared.source_symbol,
                         source_start_ns=self.prepared.start_ns,
                         instrument=asdict(self.instrument),
                         latency=self.config.latency.model_dump(mode="json"))
        try:
            sessions = await self.transport.start(self.receive_report, roles, self.options.timeout)
            participants = [self.config.fix.session_participants.get(sessions[r]) for r in roles]
            if None in participants or len(set(participants)) != len(participants):
                raise ReplayError("Replay BUY, SELL and controller require configured distinct participants")
            await self._initialize(sessions)
            if self.options.profile == "exact":
                await self._exact()
            else:
                await self._interactive()
            await self._drain_reports()
            self._check()
            samples = sorted(self._samples)
            self.summary["round_trip_sample_count"] = len(samples)
            self.summary["completed_requests"] = self._all_rtt_count
            self.summary["round_trip_ns_last_4096"] = {
                f"p{p}": samples[min(len(samples) - 1, math.ceil(len(samples) * p / 100) - 1)]
                for p in (50, 95, 99) if samples
            }
            self.summary["status"] = "qualified" if self.options.profile == "exact" else "completed"
            self.audit.write("summary", **self.summary)
            return dict(self.summary)
        except BaseException as exc:
            self.audit.write("failure", source_cursor=self._last_source_event, error=str(exc))
            raise
        finally:
            self._closed = self._eof = True
            self.controller.stop()
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            try:
                if self._started_run:
                    stats = await self._request("GET", "/stats")
                    if (stats.get("run_id") == self.options.run_id and stats.get("run_active")
                            and stats.get("run_generation") == self._generation):
                        await self._request("POST", "/api/v1/session/end")
            finally:
                await self.transport.close()

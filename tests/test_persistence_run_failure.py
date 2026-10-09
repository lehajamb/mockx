"""Exchange-level persistence loss must preserve output and fail the run."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from httpx import ASGITransport, AsyncClient
import pytest

from src.mockx.gateway import fix
from src.mockx.configuration import validate_config
from src.mockx.main import MockExchange
from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricRecord,
    MetricsSinkError,
    PersistenceAddOutcome,
)
from src.mockx.models import (
    CancelReject,
    ExecutionReport,
    ExecutionType,
    OrderStatus,
)

from .test_fix_ingress_pipeline import _cancel, _exchange, _new_order, _start
from .api_helpers import api_config
from .test_itch_integration import (
    CapturingMoldTransport,
    authoritative_wire_book,
    decode_packets,
    itch_config,
    replay_displayed_book,
)


def _attach_writer(exchange, *, capacity=1, sink=None):
    writer = BufferedMetricsWriter(
        sink or InMemoryMetricsSink(),
        flush_batch_size=capacity,
        max_buffer_records=capacity,
        max_buffer_bytes=1_000_000,
    )
    exchange.metrics.attach_persistence(writer)
    return writer


def _fill(writer):
    for sequence in range(1, writer.max_buffer_records - writer.buffered_count + 1):
        writer.add_nowait(
            MetricRecord.create(
                "capacity_fixture", {}, {"value": sequence},
                run_id="fixture", metric_event_sequence=sequence,
                event_timestamp_ns=sequence,
            )
        )


async def _submit(exchange, session, *messages):
    for message in messages:
        exchange.fix_app.fromApp(message, session)
    await exchange.fix_app.wait_for_ingress()


def _orders(exchange):
    return {order.cl_ord_id: order for order in exchange.engine.orders.values()}


def _saturate_on_report(exchange, monkeypatch, exec_type=ExecutionType.NEW):
    writer = _attach_writer(exchange, capacity=100)
    original = exchange.metrics.record_order
    triggered = False

    def record_report(report):
        nonlocal triggered
        if report.exec_type == exec_type and not triggered:
            triggered = True
            _fill(writer)
        return original(report)

    monkeypatch.setattr(exchange.metrics, "record_order", record_report)
    return writer


@pytest.mark.asyncio
async def test_loss_during_ack_preserves_fix_and_final_book_broadcast(monkeypatch):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    broadcasts = []

    async def broadcast(symbol, message, *, sequence):
        broadcasts.append((symbol, message, sequence))

    monkeypatch.setattr(exchange.api.registry, "enqueue_market_data", broadcast)
    try:
        await _submit(exchange, sessions["CLIENT"], _new_order("ACK", 1))
        _saturate_on_report(exchange, monkeypatch)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        await asyncio.sleep(0)

        assert [report.exec_type for report in delivered] == [ExecutionType.NEW]
        assert _orders(exchange)["ACK"].status == OrderStatus.NEW
        assert len(broadcasts) == 1
        assert broadcasts[0][2] == 1
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert exchange.scheduler.state.value == "RUNNING"
        outcomes = exchange.api._stats_response().persistence.current_run_outcomes
        assert outcomes.integrity_complete is False
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_lost_metrics_end_reports_failed_run_and_allows_clean_next_run(monkeypatch):
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    client = AsyncClient(transport=ASGITransport(app=exchange.app), base_url="http://test")
    try:
        await _submit(exchange, sessions["CLIENT"], _new_order("LOSS", 1))
        _saturate_on_report(exchange, monkeypatch)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        response = await client.post("/api/v1/session/end")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "session_failed"
        assert body["detail"]["code"] == "METRICS_INCOMPLETE"
        assert body["final_stats"]["persistence"]["integrity_complete"] is False
        assert body["final_stats"]["run_failure"]["run_id"] == "pr5"
        assert exchange.lifecycle.state.value == "STOPPED"

        started = await client.post("/api/v1/session/start", json={"run_id": "clean"})
        assert started.status_code == 200
        assert started.json()["run_generation"] == 2
        stats = exchange.api._stats_response().model_dump(mode="json")
        assert stats["run_failure"] is None
        assert stats["persistence"]["current_run_outcomes"]["integrity_complete"] is True
    finally:
        await client.aclose()
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_loss_in_first_fill_completes_command_then_rejects_same_time_requests(monkeypatch):
    raw = itch_config()
    raw["fix"]["session_participants"] = {
        "FIX.4.4:MOCKX->CLIENT": "buyer",
        "FIX.4.4:MOCKX->CLIENT2": "seller",
    }
    exchange = MockExchange(validate_config(raw, environ={}))
    packets = []
    exchange.itch_feed.transport_factory = lambda config, on_error: CapturingMoldTransport(
        config, on_error=on_error, packets=packets,
    )
    sessions, delivered = await _start(exchange, "CLIENT", "CLIENT2")
    broadcasts = []

    async def capture(symbol, message, *, sequence):
        broadcasts.append((symbol, message, sequence))

    monkeypatch.setattr(exchange.api.registry, "enqueue_market_data", capture)
    try:
        await _submit(
            exchange, sessions["CLIENT2"],
            _new_order("ASK1", 1, side=fix.Side_SELL, qty=2, price=100),
            _new_order("ASK2", 2, side=fix.Side_SELL, qty=2, price=101),
        )
        await _submit(exchange, sessions["CLIENT"], _new_order("REST", 1, price=95))
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        await asyncio.sleep(0)
        delivered.clear()
        broadcasts.clear()
        await _submit(
            exchange, sessions["CLIENT"],
            _new_order("TAKER", 2, qty=4, price=101),
            _new_order("NEXT", 3, qty=1, price=101),
            _cancel("C-REST", "REST", 4),
        )
        _saturate_on_report(exchange, monkeypatch, ExecutionType.TRADE)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        await asyncio.sleep(0)

        orders = _orders(exchange)
        assert [
            orders[name].status for name in ("ASK1", "ASK2", "TAKER")
        ] == [OrderStatus.FILLED] * 3
        assert orders["TAKER"].filled_qty_lots == 4
        assert orders["NEXT"].status == OrderStatus.REJECTED
        assert orders["REST"].status == OrderStatus.NEW
        trades = [
            item for item in delivered
            if isinstance(item, ExecutionReport)
            and item.exec_type == ExecutionType.TRADE
        ]
        assert len(trades) == 4
        assert [item.last_qty_lots for item in trades] == [2, 2, 2, 2]
        rejects = [
            item for item in delivered
            if isinstance(item, ExecutionReport)
            and item.exec_type == ExecutionType.REJECTED
        ]
        assert [item.cl_ord_id for item in rejects] == ["NEXT"]
        cancels = [item for item in delivered if isinstance(item, CancelReject)]
        assert len(cancels) == 1
        assert cancels[0].cancel_cl_ord_id == "C-REST"
        assert cancels[0].reason.value == "EXCHANGE_UNAVAILABLE"
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert len(broadcasts) == 1
        assert broadcasts[0][2] == exchange.engine.market_data_sequence
        _, messages = decode_packets(packets)
        book, matches = replay_displayed_book(messages)
        assert book == authoritative_wire_book(exchange)
        assert len(matches) == 2
        assert set(book) == {orders["REST"].id}
        exchange.engine.books["TEST"].assert_invariants()
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_full_buffer_during_fix_ingress_rejects_before_matching(monkeypatch):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    wire_rejects = []
    monkeypatch.setattr(
        exchange.fix_app, "send_business_reject",
        lambda *args, **kwargs: wire_rejects.append((args, kwargs)),
    )
    try:
        writer = _attach_writer(exchange)
        _fill(writer)
        await _submit(exchange, sessions["CLIENT"], _new_order("NEVER-MATCH", 1))
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert exchange.engine.resting_order_count() == 0
        assert exchange.metrics.snapshot().trades == 0
        rejections = [
            item for item in delivered
            if isinstance(item, ExecutionReport)
            and item.exec_type == ExecutionType.REJECTED
        ]
        assert len(rejections) + len(wire_rejects) == 1
        assert all(
            order.status == OrderStatus.REJECTED
            for order in exchange.engine.orders.values()
        )
    finally:
        await exchange.stop_components()


class ToggleSink(InMemoryMetricsSink):
    def __init__(self):
        super().__init__()
        self.fail = False

    async def write(self, records):
        if self.fail:
            raise MetricsSinkError("planned unavailable sink")
        await super().write(records)


@pytest.mark.asyncio
async def test_first_failure_time_survives_delivery_advance_and_end(monkeypatch):
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    try:
        await _submit(exchange, sessions["CLIENT"], _new_order("FIRST-FAULT", 1))
        _saturate_on_report(exchange, monkeypatch)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        first = exchange.lifecycle.run_failure
        await exchange.lifecycle.advance_to(
            exchange.scheduler.current_time + timedelta(milliseconds=10)
        )
        ended = await exchange.lifecycle.end_session()
        assert ended.final_stats.run_failure.failed_at == first.failed_at
        assert ended.final_stats.run_failure.reason == first.reason
        assert ended.final_stats.run_failure.metric_event_sequence == first.metric_event_sequence
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_failed_final_flush_keeps_failed_run_and_retry_preserves_failure(monkeypatch):
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    client = AsyncClient(transport=ASGITransport(app=exchange.app), base_url="http://test")
    try:
        await _submit(exchange, sessions["CLIENT"], _new_order("LOSS", 1))
        writer = _saturate_on_report(exchange, monkeypatch)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        sink = ToggleSink()
        sink.fail = True
        writer.sink = sink
        retained = writer.buffered_count
        ended = await client.post("/api/v1/session/end")
        assert ended.status_code == 503
        assert ended.json()["detail"]["code"] == "METRICS_FLUSH_FAILED"
        assert writer.buffered_count == retained > 0
        assert exchange.lifecycle.state.value == "RUN_FAILED"

        sink.fail = False
        assert (await writer.flush()).successful
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert (await client.get("/readyz")).status_code == 503
        assert exchange.api._stats_response().run_active is False
        start = await client.post("/api/v1/session/start", json={"run_id": "too-early"})
        assert start.status_code >= 400
        assert exchange.lifecycle.run_id == "pr5"
        ended = await client.post("/api/v1/session/end")
        assert ended.status_code == 503
        assert ended.json()["detail"]["code"] == "METRICS_INCOMPLETE"
        assert exchange.lifecycle.state.value == "STOPPED"
    finally:
        await client.aclose()
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_initial_snapshot_loss_is_an_identified_endable_failed_generation(monkeypatch):
    exchange = _exchange()
    await exchange.start_components()
    writer = _attach_writer(exchange)
    original = writer.add_nowait

    def reject_initial_snapshot(record):
        if record.table == "book_snapshot":
            return PersistenceAddOutcome.REJECTED_FULL
        return original(record)

    monkeypatch.setattr(writer, "add_nowait", reject_initial_snapshot)
    client = AsyncClient(transport=ASGITransport(app=exchange.app), base_url="http://test")
    try:
        started = await client.post("/api/v1/session/start", json={"run_id": "initial-loss"})
        assert started.status_code == 503
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert exchange.lifecycle.run_id == "initial-loss"
        assert exchange.lifecycle.run_generation == 1
        stats = exchange.api._stats_response().model_dump(mode="json")
        assert stats["run_id"] == "initial-loss"
        assert stats["run_failure"]["run_generation"] == 1
        assert stats["persistence"]["current_run_outcomes"]["integrity_complete"] is False
        ended = await client.post("/api/v1/session/end")
        assert ended.status_code == 503
        assert ended.json()["status"] == "session_failed"
        assert exchange.lifecycle.state.value == "STOPPED"
    finally:
        await client.aclose()
        await exchange.stop_components()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence", ["disabled", "healthy", "recoverable_outage"])
async def test_no_metric_loss_keeps_simulation_active(persistence):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        if persistence != "disabled":
            sink = ToggleSink()
            writer = _attach_writer(exchange, capacity=100, sink=sink)
        await _submit(exchange, sessions["CLIENT"], _new_order("HEALTHY", 1))
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        if persistence == "recoverable_outage":
            sink.fail = True
            assert not (await writer.flush()).successful
            assert writer.buffered_count > 0
            sink.fail = False
            assert (await writer.flush()).successful
        assert [item.exec_type for item in delivered] == [ExecutionType.NEW]
        assert exchange.lifecycle.state.value == "ACTIVE"
        stats = exchange.api._stats_response().model_dump(mode="json")
        assert stats["run_failure"] is None
        assert stats["persistence"]["current_run_outcomes"]["integrity_complete"] is True
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["external", "realtime"])
async def test_failed_run_retains_outbound_latency_and_transport_retry(mode, monkeypatch):
    raw = api_config()
    raw["fix"]["session_participants"] = {"FIX.4.4:MOCKX->CLIENT": "buyer"}
    raw["scheduler"].update(mode=mode, interval_ms=1)
    raw["latency"] = {"inbound_ms": 0, "engine_ms": 0, "outbound_ms": 7}
    exchange = MockExchange(validate_config(raw, environ={}))
    sessions, delivered = await _start(exchange)
    exchange.fix_app.delivery_queue.retry_interval_ms = 5
    attempts = []
    complete = asyncio.Event()

    def send(_transport, report):
        attempts.append((report, exchange.scheduler.current_time))
        if len(attempts) == 1:
            raise RuntimeError("temporary disconnect")
        delivered.append(report)
        complete.set()

    exchange.fix_app.transport_sender = send
    try:
        _saturate_on_report(exchange, monkeypatch)
        exchange.fix_app.fromApp(_new_order("DELAYED", 1), sessions["CLIENT"])
        if mode == "external":
            await exchange.fix_app.wait_for_ingress()
            start = exchange.scheduler.current_time
            await exchange.lifecycle.advance_to(start)
            assert exchange.lifecycle.state.value == "RUN_FAILED"
            await exchange.lifecycle.advance_to(start + timedelta(milliseconds=6))
            assert attempts == []
            await exchange.lifecycle.advance_to(start + timedelta(milliseconds=7))
            assert len(attempts) == 1 and delivered == []
            await exchange.lifecycle.advance_to(start + timedelta(milliseconds=12))
        else:
            # The realtime runner owns ingress and advancement. It can fail the
            # run before a separate ACTIVE-token ingress barrier acquires the
            # lock, so synchronize on the report this test actually requires.
            await asyncio.wait_for(complete.wait(), timeout=2)
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert len(attempts) == 2
        assert len(delivered) == 1
        assert attempts[0][0] is attempts[1][0]
        assert attempts[0][1] - delivered[0].timestamp == timedelta(milliseconds=7)
        assert attempts[1][1] - attempts[0][1] == timedelta(milliseconds=5)
        assert delivered[0].exec_type == ExecutionType.NEW
        assert exchange.scheduler.state.value == "RUNNING"
        assert exchange.metrics.snapshot().average_sim_outbound_latency_ms == 12.0
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [ExecutionType.CANCELED, ExecutionType.REJECTED])
async def test_persistence_loss_preserves_cancel_or_rejection_report(outcome, monkeypatch):
    raw = api_config()
    raw["fix"]["session_participants"] = {"FIX.4.4:MOCKX->CLIENT": "buyer"}
    if outcome == ExecutionType.REJECTED:
        raw["execution"]["reject_prob"] = 1.0
    exchange = MockExchange(validate_config(raw, environ={}))
    sessions, delivered = await _start(exchange)
    try:
        if outcome == ExecutionType.CANCELED:
            await _submit(exchange, sessions["CLIENT"], _new_order("ORDER", 1))
            await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
            delivered.clear()
            await _submit(exchange, sessions["CLIENT"], _cancel("CANCEL", "ORDER", 2))
        else:
            await _submit(exchange, sessions["CLIENT"], _new_order("ORDER", 1))
        _saturate_on_report(exchange, monkeypatch, outcome)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert len(delivered) == 1
        assert delivered[0].exec_type == outcome
        assert _orders(exchange)["ORDER"].status in {OrderStatus.CANCELLED, OrderStatus.REJECTED}
        assert exchange.engine.resting_order_count() == 0
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        exchange.engine.books["TEST"].assert_invariants()
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_failed_run_advance_reports_time_and_rejects_new_fix(monkeypatch):
    exchange = _exchange(outbound_ms=7)
    sessions, delivered = await _start(exchange)
    client = AsyncClient(transport=ASGITransport(app=exchange.app), base_url="http://test")
    rejects = []
    monkeypatch.setattr(
        exchange.fix_app, "send_business_reject",
        lambda *args, **kwargs: rejects.append((args, kwargs)),
    )
    try:
        await _submit(exchange, sessions["CLIENT"], _new_order("ACK", 1))
        _saturate_on_report(exchange, monkeypatch)
        response = await client.post("/admin/advance-time", json={"milliseconds": 7})
        assert response.status_code == 503
        body = response.json()
        assert body["detail"]["code"] == "METRICS_INCOMPLETE"
        assert body["run_failure"]["run_id"] == "pr5"
        assert body["current_time"] == "1970-01-01T00:00:00.007000Z"
        assert len(delivered) == 1
        assert delivered[0].cl_ord_id == "ACK"
        previous_orders = dict(_orders(exchange))
        exchange.fix_app.fromApp(_new_order("AFTER-LOSS", 2), sessions["CLIENT"])
        await exchange.fix_app._await_lifecycle_actions()
        assert len(rejects) == 1
        assert "METRICS_INCOMPLETE" in str(rejects[0])
        assert _orders(exchange) == previous_orders
        assert (await client.get("/readyz")).status_code == 503
        assert exchange.api._stats_response().run_active is False
    finally:
        await client.aclose()
        await exchange.stop_components()


class BlockingFinalSink(InMemoryMetricsSink):
    def __init__(self, *, fail):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail

    async def write(self, records):
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise MetricsSinkError("final flush unavailable")
        await super().write(records)


@pytest.mark.asyncio
@pytest.mark.parametrize("flush_fails", [False, True])
async def test_reconnect_metric_loss_during_end_flush_cannot_report_success(flush_fails):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    identity = exchange.fix_app.session_registry.resolve_identity(session.toString())
    client = AsyncClient(
        transport=ASGITransport(app=exchange.app), base_url="http://test",
    )
    sink = BlockingFinalSink(fail=flush_fails)
    ending = None
    try:
        await _submit(exchange, session, _new_order("RETAINED", 1))
        exchange.fix_app.unregister_session(session.toString())
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert delivered == []
        writer = _attach_writer(exchange, sink=sink)
        _fill(writer)
        assert exchange.metrics.integrity_complete is True

        ending = asyncio.create_task(client.post("/api/v1/session/end"))
        await asyncio.wait_for(sink.entered.wait(), timeout=2)
        assert exchange.lifecycle.state.value == "ENDING"
        received = asyncio.Event()

        def send(_transport, report):
            delivered.append(report)
            received.set()

        exchange.fix_app.transport_sender = send
        exchange.fix_app.register_session(identity, object())
        await asyncio.wait_for(received.wait(), timeout=2)
        # Delivery-completion latency now competes with the full in-flight batch.
        assert exchange.metrics.integrity_complete is False
        await asyncio.sleep(0)
        sink.release.set()
        response = await ending

        assert response.status_code == 503
        body = response.json()
        if flush_fails:
            assert body["detail"]["code"] == "METRICS_FLUSH_FAILED"
            assert exchange.lifecycle.state.value == "RUN_FAILED"
            assert writer.buffered_count == 1
            assert exchange.lifecycle.run_failure.run_id == "pr5"
        else:
            assert body["status"] == "session_failed"
            assert body["detail"]["code"] == "METRICS_INCOMPLETE"
            assert body["final_stats"]["run_failure"]["run_id"] == "pr5"
            assert body["final_stats"]["persistence"]["integrity_complete"] is False
            assert exchange.lifecycle.state.value == "STOPPED"
    finally:
        sink.fail = False
        sink.release.set()
        if ending is not None:
            await ending
        await client.aclose()
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_reconnect_metric_loss_during_itch_start_cannot_activate_run(monkeypatch):
    raw = itch_config()
    raw["fix"]["session_participants"] = {"FIX.4.4:MOCKX->CLIENT": "buyer"}
    exchange = MockExchange(validate_config(raw, environ={}))
    packets = []
    exchange.itch_feed.transport_factory = lambda config, on_error: CapturingMoldTransport(
        config, on_error=on_error, packets=packets,
    )
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    identity = exchange.fix_app.session_registry.resolve_identity(session.toString())
    entered = asyncio.Event()
    release = asyncio.Event()
    original_start = exchange.itch_feed.start_run

    async def paused_start(generation, timestamp):
        entered.set()
        await release.wait()
        await original_start(generation, timestamp)

    monkeypatch.setattr(exchange.itch_feed, "start_run", paused_start)
    client = AsyncClient(
        transport=ASGITransport(app=exchange.app), base_url="http://test",
    )
    starting = None
    try:
        await _submit(exchange, session, _new_order("PREVIOUS-RUN", 1))
        exchange.fix_app.unregister_session(session.toString())
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        await exchange.lifecycle.end_session()
        writer = _attach_writer(exchange)
        starting = asyncio.create_task(client.post(
            "/api/v1/session/start", json={"run_id": "startup-loss"},
        ))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert exchange.lifecycle.state.value == "STARTING"
        assert writer.buffered_count == 1
        assert exchange.metrics.integrity_complete is True
        received = asyncio.Event()

        def send(_transport, report):
            delivered.append(report)
            received.set()

        exchange.fix_app.transport_sender = send
        exchange.fix_app.register_session(identity, object())
        await asyncio.wait_for(received.wait(), timeout=2)
        assert exchange.metrics.integrity_complete is False
        await asyncio.sleep(0)
        release.set()
        response = await starting

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "METRICS_INCOMPLETE"
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert exchange.lifecycle.run_failure.run_id == "startup-loss"
        assert exchange.lifecycle.run_failure.run_generation == 2
        assert exchange.api._stats_response().run_active is False
    finally:
        release.set()
        if starting is not None:
            await starting
        await client.aclose()
        await exchange.stop_components()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["snapshot", "broadcast"])
async def test_metric_loss_during_websocket_wait_fails_run_on_exit(operation):
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def websocket_action():
        entered.set()
        await release.wait()

    pending = None
    try:
        writer = _attach_writer(exchange)
        _fill(writer)
        action = (
            exchange.lifecycle.websocket_snapshot(websocket_action)
            if operation == "snapshot"
            else exchange.lifecycle.broadcast_if_current(1, websocket_action)
        )
        pending = asyncio.create_task(action)
        await asyncio.wait_for(entered.wait(), timeout=2)
        heartbeat = fix.Message()
        heartbeat.getHeader().setField(fix.MsgType(fix.MsgType_Heartbeat))
        exchange.fix_app.fromAdmin(heartbeat, sessions["CLIENT"])
        assert exchange.metrics.integrity_complete is False
        await asyncio.sleep(0)
        release.set()
        await pending

        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert exchange.lifecycle.run_failure.run_id == "pr5"
        assert exchange.api._readiness()[0] == 503
    finally:
        release.set()
        if pending is not None:
            await pending
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_immediate_failed_run_end_delivers_final_websocket_snapshot(monkeypatch):
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    broadcasts = []

    async def capture(symbol, message, *, sequence):
        broadcasts.append((symbol, message, sequence))

    monkeypatch.setattr(exchange.api.registry, "enqueue_market_data", capture)
    try:
        await _submit(exchange, sessions["CLIENT"], _new_order("FINAL-BOOK", 1))
        _saturate_on_report(exchange, monkeypatch)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        # Do not yield to the queued broadcast before requesting closure.
        ended = await exchange.lifecycle.end_session()

        assert len(broadcasts) == 1
        assert broadcasts[0][0] == "TEST"
        assert broadcasts[0][2] == exchange.engine.market_data_sequence == 1
        assert ended.final_stats.run_failure.run_id == "pr5"
        assert ended.final_stats.persistence.integrity_complete is False
        assert exchange.lifecycle.state.value == "STOPPED"
    finally:
        await exchange.stop_components()

"""An accepted run transition settles before caller cancellation escapes."""

from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient
import pytest

from src.mockx.api import market_data_message
from src.mockx.gateway import fix
from src.mockx.lifecycle import LifecycleError, LifecycleState
from src.mockx.metrics import BufferedMetricsWriter, InMemoryMetricsSink

from .api_helpers import make_exchange


class _Gate:
    def __init__(self, error=None):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.error = error

    async def wait(self):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error


class _Feed:
    def __init__(self):
        self.start_gate = None
        self.started = []
        self.ended = []
        self.active = False

    def validate_generation(self, generation):
        assert generation > 0

    async def start_run(self, generation, timestamp):
        self.started.append(generation)
        self.active = True
        if self.start_gate is not None:
            await self.start_gate.wait()

    def end_run(self, timestamp):
        self.ended.append(timestamp)
        self.active = False


class _Glimpse:
    def __init__(self):
        self.start_gate = None
        self.listener_started = False
        self.generations = []
        self.revoked = 0
        self.stop_calls = 0

    async def start(self):
        self.listener_started = True
        if self.start_gate is not None:
            await self.start_gate.wait()

    def begin_run(self, generation):
        self.generations.append(generation)

    def revoke_all(self):
        self.revoked += 1

    def revoke_run(self, generation):
        self.revoked += 1

    async def stop(self):
        self.stop_calls += 1
        self.listener_started = False


def _start_gate(exchange, monkeypatch, stage, *, error=None):
    gate = _Gate(error)
    feed, glimpse = _Feed(), _Glimpse()
    exchange.lifecycle.itch_feed = feed
    exchange.lifecycle.bind_glimpse_service(glimpse)
    if stage == "flush":
        original = exchange.metrics.flush

        async def flush():
            await gate.wait()
            return await original()

        monkeypatch.setattr(exchange.metrics, "flush", flush)
    elif stage == "websocket":
        exchange.lifecycle._close_websockets = gate.wait
    elif stage == "itch":
        feed.start_gate = gate
    elif stage == "glimpse":
        glimpse.start_gate = gate
    else:
        raise AssertionError(stage)
    return gate, feed, glimpse


async def _entered(gate):
    await asyncio.wait_for(gate.entered.wait(), timeout=3)


async def _loop_barrier():
    """Let already-ready cancellation callbacks run, without a timed sleep."""
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


async def _cleanup(task, gate):
    gate.release.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["flush", "websocket", "itch", "glimpse"])
async def test_cancelled_accepted_start_finishes_once_before_releasing_lock(
    monkeypatch, stage,
):
    exchange = make_exchange()
    gate, feed, glimpse = _start_gate(exchange, monkeypatch, stage)
    existing_tasks = asyncio.all_tasks()
    starting = asyncio.create_task(exchange.lifecycle.start_session("cancel-start"))
    try:
        await _entered(gate)
        starting.cancel("request disconnected")
        await _loop_barrier()
        assert not starting.done()
        assert exchange.lifecycle.lock.locked()
        assert exchange.lifecycle.state == LifecycleState.STARTING

        starting.cancel("second disconnect")
        await _loop_barrier()
        assert not starting.done()
        assert exchange.lifecycle.lock.locked()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="request disconnected"):
            await asyncio.wait_for(starting, timeout=3)

        assert exchange.lifecycle.state == LifecycleState.ACTIVE
        assert exchange.lifecycle.run_id == "cancel-start"
        assert exchange.lifecycle.run_generation == 1
        assert exchange.metrics.run_id == "cancel-start"
        assert gate.calls == 1
        assert feed.started == [1]
        assert glimpse.generations == [1]
        assert not exchange.lifecycle.lock.locked()
        assert asyncio.all_tasks() <= existing_tasks
    finally:
        await _cleanup(starting, gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "end", "end-failed"])
async def test_internal_worker_cancellation_before_first_step_restores_previous_state(
    monkeypatch, operation,
):
    if operation == "start":
        exchange = make_exchange()
    else:
        gate = _Gate()
        gate.release.set()
        exchange, _, _ = await _buffered_run(failed=operation == "end-failed", gate=gate)
    previous_state = exchange.lifecycle.state
    previous_run_id = exchange.lifecycle.run_id
    previous_generation = exchange.lifecycle.run_generation
    previous_failure = exchange.lifecycle.run_failure
    original_create_task = asyncio.create_task
    original_flush = exchange.metrics.flush
    flush_calls = 0
    cancelled_workers = []

    async def count_flush():
        nonlocal flush_calls
        flush_calls += 1
        return await original_flush()

    def cancel_worker_before_start(coro, **kwargs):
        task = original_create_task(coro, **kwargs)
        if kwargs.get("name", "").startswith(("session-start:", "session-end:")):
            task.cancel("worker cancelled before start")
            cancelled_workers.append(task)
        return task

    monkeypatch.setattr(exchange.metrics, "flush", count_flush)
    with monkeypatch.context() as task_patch:
        task_patch.setattr(asyncio, "create_task", cancel_worker_before_start)
        with pytest.raises(asyncio.CancelledError, match="worker cancelled before start"):
            if operation == "start":
                await exchange.lifecycle.start_session("retryable-start")
            else:
                await exchange.lifecycle.end_session()

    assert len(cancelled_workers) == 1
    assert cancelled_workers[0].cancelled()
    assert flush_calls == 0
    assert exchange.lifecycle.state == previous_state
    assert exchange.lifecycle.run_id == previous_run_id
    assert exchange.metrics.run_id == previous_run_id
    assert exchange.lifecycle.run_generation == previous_generation
    assert exchange.lifecycle.run_failure is previous_failure
    assert not exchange.lifecycle.lock.locked()
    if operation == "start":
        result = await exchange.lifecycle.start_session("retryable-start")
        assert result.run_generation == 1
        assert exchange.lifecycle.state == LifecycleState.ACTIVE
    else:
        result = await exchange.lifecycle.end_session()
        assert result.final_stats.persistence.integrity_complete is (operation == "end")
        assert exchange.lifecycle.state == LifecycleState.STOPPED
        assert exchange.lifecycle.run_failure is previous_failure


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["flush", "websocket", "itch", "glimpse"])
async def test_internal_start_cancellation_restores_safe_state(monkeypatch, stage):
    exchange = make_exchange()
    gate, feed, glimpse = _start_gate(
        exchange, monkeypatch, stage, error=asyncio.CancelledError("dependency cancelled"),
    )
    starting = asyncio.create_task(exchange.lifecycle.start_session("internal-start"))
    try:
        await _entered(gate)
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="dependency cancelled"):
            await asyncio.wait_for(starting, timeout=3)

        destructive = stage in {"itch", "glimpse"}
        assert exchange.lifecycle.state == (
            LifecycleState.FAILED if destructive else LifecycleState.STOPPED
        )
        assert exchange.lifecycle.run_id is None
        assert exchange.metrics.run_id is None
        assert not exchange.lifecycle.lock.locked()
        assert not feed.active
        assert glimpse.generations == []
        assert glimpse.stop_calls == 0
        if destructive:
            assert len(feed.ended) == 1
            assert glimpse.revoked >= 2
        if stage == "glimpse":
            assert glimpse.listener_started
    finally:
        await _cleanup(starting, gate)


class _PausedSink(InMemoryMetricsSink):
    def __init__(self, gate):
        super().__init__()
        self.gate = gate

    async def write(self, records):
        await self.gate.wait()
        await super().write(records)


async def _buffered_run(*, failed, gate):
    exchange = make_exchange()
    await exchange.lifecycle.start_session("cancel-end")
    sink = _PausedSink(gate)
    writer = BufferedMetricsWriter(
        sink, flush_batch_size=2, max_buffer_records=2, max_buffer_bytes=1_000_000,
    )
    exchange.metrics.attach_persistence(writer)
    snapshot = exchange.engine.get_market_data_snapshot("TEST")
    exchange.metrics.update_book_metrics(snapshot)
    if failed:
        exchange.metrics.update_book_metrics(snapshot)
        exchange.metrics.update_book_metrics(snapshot)
        assert exchange.lifecycle.check_persistence_failure()
    return exchange, writer, sink


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True], ids=["active", "run-failed"])
@pytest.mark.parametrize("flush_fails", [False, True], ids=["flush-ok", "flush-fails"])
async def test_cancelled_end_settles_real_buffered_write(failed, flush_fails, caplog):
    gate = _Gate(RuntimeError("planned sink failure") if flush_fails else None)
    exchange, writer, sink = await _buffered_run(failed=failed, gate=gate)
    failure = exchange.lifecycle.run_failure
    accepted = writer.buffered_count
    ending = asyncio.create_task(exchange.lifecycle.end_session())
    try:
        await _entered(gate)
        ending.cancel("end disconnected")
        await _loop_barrier()
        assert not ending.done()
        assert exchange.lifecycle.lock.locked()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="end disconnected"):
            await asyncio.wait_for(ending, timeout=3)

        assert not exchange.lifecycle.lock.locked()
        assert exchange.lifecycle.run_failure is failure
        if flush_fails:
            assert exchange.lifecycle.state == (
                LifecycleState.RUN_FAILED if failed else LifecycleState.ACTIVE
            )
            assert exchange.lifecycle.run_id == "cancel-end"
            assert exchange.metrics.run_id == "cancel-end"
            assert writer.buffered_count == accepted
            assert sink.writes == []
            assert any(
                getattr(record, "operation", None) == "end"
                and getattr(record, "simulation_session_id", None) == "cancel-end"
                and getattr(record, "run_generation", None) == 1
                for record in caplog.records
            )
            gate.error = None
            result = await exchange.lifecycle.end_session()
            assert result.final_stats.persistence.integrity_complete is (not failed)
        else:
            assert writer.buffered_count == 0
        assert sum(len(batch) for batch in sink.writes) == accepted
        assert exchange.lifecycle.state == LifecycleState.STOPPED
        assert exchange.metrics.run_id is None
        assert exchange.lifecycle.run_id is None
    finally:
        await _cleanup(ending, gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True], ids=["active", "run-failed"])
async def test_internal_end_cancellation_retains_run_and_buffer_for_retry(failed):
    gate = _Gate(asyncio.CancelledError("sink cancelled"))
    exchange, writer, sink = await _buffered_run(failed=failed, gate=gate)
    failure = exchange.lifecycle.run_failure
    accepted = writer.buffered_count
    ending = asyncio.create_task(exchange.lifecycle.end_session())
    try:
        await _entered(gate)
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="sink cancelled"):
            await asyncio.wait_for(ending, timeout=3)
        assert exchange.lifecycle.state == (
            LifecycleState.RUN_FAILED if failed else LifecycleState.ACTIVE
        )
        assert exchange.lifecycle.run_failure is failure
        assert exchange.lifecycle.run_id == exchange.metrics.run_id == "cancel-end"
        assert not exchange.lifecycle.lock.locked()
        assert writer.buffered_count == accepted
        gate.error = None
        result = await exchange.lifecycle.end_session()
        assert result.final_stats.persistence.integrity_complete is (not failed)
        assert sum(len(batch) for batch in sink.writes) == accepted
        assert exchange.lifecycle.state == LifecycleState.STOPPED
    finally:
        await _cleanup(ending, gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "end"])
async def test_cancellation_before_lock_acquisition_does_not_accept_transition(
    monkeypatch, operation,
):
    exchange = make_exchange()
    if operation == "end":
        await exchange.lifecycle.start_session("existing")
    state = exchange.lifecycle.state
    generation = exchange.lifecycle.run_generation
    gate = _Gate()
    monkeypatch.setattr(exchange.metrics, "flush", gate.wait)
    await exchange.lifecycle.lock.acquire()
    request = asyncio.create_task(
        exchange.lifecycle.start_session("waiting")
        if operation == "start" else exchange.lifecycle.end_session()
    )
    try:
        await _loop_barrier()
        request.cancel("before acceptance")
        with pytest.raises(asyncio.CancelledError, match="before acceptance"):
            await asyncio.wait_for(request, timeout=3)
        assert exchange.lifecycle.state == state
        assert exchange.lifecycle.run_generation == generation
        assert gate.calls == 0
    finally:
        exchange.lifecycle.lock.release()
        await _cleanup(request, gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "end"])
async def test_cancelled_transition_keeps_api_and_fix_ingress_gated(
    monkeypatch, operation,
):
    exchange = make_exchange()
    await exchange.start_components()
    if operation == "end":
        await exchange.lifecycle.start_session("gated")
    gate, _, _ = _start_gate(exchange, monkeypatch, "flush")
    rejected = []
    monkeypatch.setattr(
        exchange.fix_app, "send_business_reject",
        lambda session, text, **kwargs: rejected.append(text),
    )
    client = AsyncClient(transport=ASGITransport(app=exchange.app), base_url="http://test")
    request = asyncio.create_task(
        client.post("/api/v1/session/start", json={"run_id": "gated"})
        if operation == "start" else client.post("/api/v1/session/end")
    )
    try:
        await _entered(gate)
        request.cancel("HTTP disconnected")
        await _loop_barrier()
        response = await client.post("/admin/advance-time", json={"milliseconds": 1})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "LIFECYCLE_TRANSITION_IN_PROGRESS"
        response = await client.post("/api/v1/session/start", json={"run_id": "duplicate"})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "LIFECYCLE_TRANSITION_IN_PROGRESS"
        message = fix.Message()
        message.getHeader().setField(fix.MsgType(fix.MsgType_NewOrderSingle))
        exchange.fix_app.fromApp(message, fix.SessionID("FIX.4.4", "CLIENT", "MOCKX"))
        assert rejected == ["LIFECYCLE_TRANSITION_IN_PROGRESS"]
        assert exchange.engine.orders == {}
        assert exchange.metrics.snapshot().orders_submitted == 0
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="HTTP disconnected"):
            await asyncio.wait_for(request, timeout=3)
        assert exchange.lifecycle.run_generation == 1
        assert exchange.lifecycle.state == (
            LifecycleState.ACTIVE if operation == "start" else LifecycleState.STOPPED
        )
    finally:
        await _cleanup(request, gate)
        await client.aclose()
        await exchange.stop_components()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_first", [False, True])
@pytest.mark.parametrize("outcome", ["success", "error", "internal-cancel"])
async def test_completion_cancellation_race_preserves_caller_cancellation(
    monkeypatch, caplog, completion_first, outcome,
):
    exchange = make_exchange()
    error = {
        "success": None,
        "error": RuntimeError("transition failed"),
        "internal-cancel": asyncio.CancelledError("child cancelled"),
    }[outcome]
    gate, _, _ = _start_gate(exchange, monkeypatch, "flush", error=error)
    starting = asyncio.create_task(exchange.lifecycle.start_session("racing"))
    try:
        await _entered(gate)
        if completion_first:
            gate.release.set()
        starting.cancel("original caller cancellation")
        if not completion_first:
            gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="original caller cancellation"):
            await asyncio.wait_for(starting, timeout=3)
        assert exchange.lifecycle.state == (
            LifecycleState.ACTIVE if outcome == "success" else LifecycleState.STOPPED
        )
        assert not exchange.lifecycle.lock.locked()
        assert gate.calls == 1
        if outcome != "success":
            assert any(
                getattr(record, "operation", None) == "start"
                and getattr(record, "simulation_session_id", None) == "racing"
                and hasattr(record, "run_generation")
                for record in caplog.records
            )
    finally:
        await _cleanup(starting, gate)


@pytest.mark.asyncio
async def test_cancelled_start_holds_lock_until_competing_start_can_be_rejected(monkeypatch):
    exchange = make_exchange()
    gate, feed, _ = _start_gate(exchange, monkeypatch, "itch")
    first = asyncio.create_task(exchange.lifecycle.start_session("accepted"))
    second = None
    try:
        await _entered(gate)
        first.cancel("request disconnected")
        await _loop_barrier()
        second = asyncio.create_task(exchange.lifecycle.start_session("competing"))
        await _loop_barrier()
        assert not second.done()
        assert gate.calls == 1
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="request disconnected"):
            await asyncio.wait_for(first, timeout=3)
        with pytest.raises(LifecycleError) as rejected:
            await asyncio.wait_for(second, timeout=3)
        assert rejected.value.code == "SESSION_ALREADY_ACTIVE"
        assert exchange.lifecycle.run_id == "accepted"
        assert exchange.lifecycle.run_generation == 1
        assert feed.started == [1]
    finally:
        await _cleanup(first, gate)
        if second is not None:
            await _cleanup(second, gate)


class _ClosingWebSocket:
    def __init__(self):
        self.close_gate = _Gate()
        self.closed = False
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self, code=1000):
        await self.close_gate.wait()
        self.closed = True


@pytest.mark.asyncio
async def test_cancelled_start_finishes_real_registry_cleanup_for_every_socket():
    exchange = make_exchange()
    sockets = [_ClosingWebSocket(), _ClosingWebSocket()]
    snapshot = exchange.engine.get_market_data_snapshot("TEST")
    connections = [
        await exchange.api.registry.register(
            "TEST", socket, market_data_message(snapshot).model_dump(mode="json"),
            initial_sequence=snapshot.sequence,
        )
        for socket in sockets
    ]
    starting = asyncio.create_task(exchange.lifecycle.start_session("close-all"))
    try:
        for socket in sockets:
            await _entered(socket.close_gate)
        # close_all has detached both clients, but their actual close operations
        # are still in flight and must retain an owner after request cancellation.
        assert await exchange.api.registry.connection_count() == 0
        assert all(connection.sender_task.done() for connection in connections)
        starting.cancel("request disconnected")
        await _loop_barrier()
        assert not starting.done()
        assert exchange.lifecycle.lock.locked()
        assert all(not socket.closed for socket in sockets)
        sockets[0].close_gate.release.set()
        await _loop_barrier()
        assert not starting.done()
        sockets[1].close_gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="request disconnected"):
            await asyncio.wait_for(starting, timeout=3)
        assert exchange.lifecycle.state == LifecycleState.ACTIVE
        assert exchange.lifecycle.run_generation == 1
        assert all(socket.closed for socket in sockets)
        assert all(socket.close_gate.calls == 1 for socket in sockets)
        assert all(connection.sender_task.done() for connection in connections)
        assert all(connection.state.value == "CLOSED" for connection in connections)
        assert await exchange.api.registry.connection_count() == 0
        stats = exchange.metrics.websocket_metrics_snapshot()
        assert stats["active_clients"] == 0
        assert stats["queue_depth"] == 0
        assert not exchange.lifecycle.lock.locked()
    finally:
        for socket in sockets:
            socket.close_gate.release.set()
        await _cleanup(starting, sockets[0].close_gate)
        for connection in connections:
            await exchange.api.registry.disconnect(connection)


@pytest.mark.asyncio
async def test_internal_end_cancellation_reconciles_new_metric_loss():
    gate = _Gate(asyncio.CancelledError("sink cancelled"))
    exchange, writer, sink = await _buffered_run(failed=False, gate=gate)
    ending = asyncio.create_task(exchange.lifecycle.end_session())
    try:
        await _entered(gate)
        snapshot = exchange.engine.get_market_data_snapshot("TEST")
        exchange.metrics.update_book_metrics(snapshot)
        exchange.metrics.update_book_metrics(snapshot)
        assert not exchange.metrics.integrity_complete
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="sink cancelled"):
            await asyncio.wait_for(ending, timeout=3)
        assert exchange.lifecycle.state == LifecycleState.RUN_FAILED
        failure = exchange.lifecycle.run_failure
        assert failure.run_id == "cancel-end"
        assert writer.buffered_count == 2
        gate.error = None
        result = await exchange.lifecycle.end_session()
        assert result.final_stats.run_failure.run_id == "cancel-end"
        assert result.final_stats.persistence.integrity_complete is False
        assert exchange.lifecycle.run_failure is failure
        assert sum(len(batch) for batch in sink.writes) == 2
    finally:
        await _cleanup(ending, gate)


@pytest.mark.asyncio
async def test_cancelled_start_preserves_metric_loss_as_endable_failed_run(monkeypatch, caplog):
    exchange = make_exchange()
    writer = BufferedMetricsWriter(
        InMemoryMetricsSink(), flush_batch_size=1, max_buffer_records=1,
        max_buffer_bytes=1_000_000,
    )
    exchange.metrics.attach_persistence(writer)
    gate, feed, _ = _start_gate(exchange, monkeypatch, "itch")
    starting = asyncio.create_task(exchange.lifecycle.start_session("lost-while-starting"))
    try:
        await _entered(gate)
        starting.cancel("request disconnected")
        await _loop_barrier()
        exchange.metrics.update_book_metrics(exchange.engine.get_market_data_snapshot("TEST"))
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="request disconnected"):
            await asyncio.wait_for(starting, timeout=3)
        assert exchange.lifecycle.state == LifecycleState.RUN_FAILED
        failure = exchange.lifecycle.run_failure
        assert failure.run_id == "lost-while-starting"
        assert feed.active
        assert any(
            getattr(record, "operation", None) == "start"
            and getattr(record, "simulation_session_id", None) == "lost-while-starting"
            for record in caplog.records
        )
        result = await exchange.lifecycle.end_session()
        assert result.final_stats.persistence.integrity_complete is False
        assert exchange.lifecycle.run_failure is failure
        assert not feed.active
        assert exchange.lifecycle.state == LifecycleState.STOPPED
    finally:
        await _cleanup(starting, gate)

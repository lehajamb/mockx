"""Per-client WebSocket queue, sender, and isolation regressions."""

import asyncio
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

from src.mockx.metrics import MetricsCollector
from src.mockx.models import Side
from src.mockx.websocket_delivery import (
    ConnectionState,
    EnqueueDisposition,
    OutboundBuffer,
    OutboundKind,
    WebSocketRegistry,
)

from .api_helpers import make_exchange, process_order


def market_data(sequence: int, symbol: str = "TEST") -> dict:
    return {
        "type": "market_data",
        "symbol": symbol,
        "sequence": sequence,
        "timestamp": "1970-01-01T00:00:00Z",
        "bids": [],
        "asks": [],
        "trades": [],
    }


def heartbeat(sequence: int, symbol: str = "TEST") -> dict:
    return {
        "type": "heartbeat",
        "symbol": symbol,
        "timestamp": "1970-01-01T00:00:00Z",
        "last_sequence": sequence,
    }


async def wait_until(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


class ControlledWebSocket:
    def __init__(self, *, blocked: bool = False, fail: bool = False):
        self.blocked = blocked
        self.fail = fail
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()
        self.messages: list[dict] = []
        self.closed = False
        self.close_code = None
        self.active_sends = 0
        self.max_active_sends = 0

    async def send_json(self, message):
        self.active_sends += 1
        self.max_active_sends = max(self.max_active_sends, self.active_sends)
        self.send_started.set()
        try:
            if self.fail:
                raise RuntimeError("planned send failure")
            if self.blocked:
                await self.release_send.wait()
            self.messages.append(message)
        finally:
            self.active_sends -= 1

    async def close(self, code=1000):
        self.closed = True
        self.close_code = code
        self.release_send.set()


@pytest.mark.asyncio
async def test_initial_snapshot_is_pinned_before_racing_broadcast():
    metrics = MetricsCollector("TEST")
    registry = WebSocketRegistry(
        metrics,
        queue_capacity=8,
        send_timeout_seconds=1,
    )
    socket = ControlledWebSocket(blocked=True)

    connection = await registry.register(
        "TEST",
        socket,
        market_data(5),
        initial_sequence=5,
    )
    await socket.send_started.wait()
    await registry.enqueue_market_data("TEST", market_data(6), sequence=6)
    await registry.enqueue_market_data("TEST", market_data(7), sequence=7)

    socket.release_send.set()
    await wait_until(lambda: len(socket.messages) == 2)

    assert [message["sequence"] for message in socket.messages] == [5, 7]
    assert connection.conflated_snapshots == 1
    await registry.disconnect(connection)


@pytest.mark.asyncio
async def test_blocked_client_does_not_block_healthy_client_same_symbol():
    metrics = MetricsCollector("TEST")
    registry = WebSocketRegistry(
        metrics,
        queue_capacity=8,
        send_timeout_seconds=1,
    )
    blocked_socket = ControlledWebSocket(blocked=True)
    healthy_socket = ControlledWebSocket()
    blocked = await registry.register(
        "TEST",
        blocked_socket,
        market_data(0),
        initial_sequence=0,
    )
    healthy = await registry.register(
        "TEST",
        healthy_socket,
        market_data(0),
        initial_sequence=0,
    )
    await wait_until(lambda: len(healthy_socket.messages) == 1)

    await asyncio.wait_for(
        registry.enqueue_market_data("TEST", market_data(1), sequence=1),
        timeout=0.05,
    )
    await wait_until(lambda: len(healthy_socket.messages) == 2)

    assert healthy_socket.messages[-1]["sequence"] == 1
    assert blocked_socket.messages == []
    blocked_socket.release_send.set()
    await registry.disconnect(blocked)
    await registry.disconnect(healthy)


@pytest.mark.asyncio
async def test_blocked_client_does_not_delay_rest_scheduler_or_publication():
    exchange = make_exchange(send_timeout_seconds=1)
    await exchange.start_components()
    client = AsyncClient(
        transport=ASGITransport(app=exchange.app),
        base_url="http://test",
    )
    blocked_socket = ControlledWebSocket(blocked=True)
    healthy_socket = ControlledWebSocket()
    try:
        started = await client.post(
            "/api/v1/session/start",
            json={"run_id": "websocket-isolation"},
        )
        assert started.status_code == 200
        snapshot = exchange.engine.get_market_data_snapshot("TEST")
        initial = market_data(snapshot.sequence)
        blocked = await exchange.api.registry.register(
            "TEST",
            blocked_socket,
            initial,
            initial_sequence=snapshot.sequence,
        )
        healthy = await exchange.api.registry.register(
            "TEST",
            healthy_socket,
            initial,
            initial_sequence=snapshot.sequence,
        )
        await wait_until(lambda: len(healthy_socket.messages) == 1)

        process_order(exchange, "ISOLATED", Side.BUY, 1.0, 100.0)
        await wait_until(lambda: len(healthy_socket.messages) == 2)
        book = await asyncio.wait_for(client.get("/book/TEST"), timeout=0.1)

        handled: list[str] = []
        exchange.scheduler.register_handler("ws_probe", handled.append)
        exchange.scheduler.schedule_event("ws_probe", "advanced", 1)
        advanced = await asyncio.wait_for(
            client.post("/admin/advance-time", json={"milliseconds": 1}),
            timeout=0.1,
        )

        assert book.status_code == 200
        assert book.json()["bids"][0]["price"] == 100.0
        assert advanced.status_code == 200
        assert handled == ["advanced"]
        assert blocked_socket.messages == []
        await exchange.api.registry.disconnect(blocked)
        await exchange.api.registry.disconnect(healthy)
    finally:
        await client.aclose()
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_blocked_client_on_one_symbol_does_not_block_another_symbol():
    metrics = MetricsCollector("TEST")
    registry = WebSocketRegistry(
        metrics,
        queue_capacity=8,
        send_timeout_seconds=1,
    )
    blocked_socket = ControlledWebSocket(blocked=True)
    healthy_socket = ControlledWebSocket()
    blocked = await registry.register(
        "BTC-USDT",
        blocked_socket,
        market_data(0, "BTC-USDT"),
        initial_sequence=0,
    )
    healthy = await registry.register(
        "ETH-USDT",
        healthy_socket,
        market_data(0, "ETH-USDT"),
        initial_sequence=0,
    )
    await wait_until(lambda: len(healthy_socket.messages) == 1)

    await registry.enqueue_market_data(
        "BTC-USDT",
        market_data(1, "BTC-USDT"),
        sequence=1,
    )
    await registry.enqueue_market_data(
        "ETH-USDT",
        market_data(2, "ETH-USDT"),
        sequence=2,
    )
    await wait_until(lambda: len(healthy_socket.messages) == 2)

    assert healthy_socket.messages[-1]["sequence"] == 2
    blocked_socket.release_send.set()
    await registry.disconnect(blocked)
    await registry.disconnect(healthy)


@pytest.mark.asyncio
async def test_buffer_is_bounded_and_conflates_to_newest_snapshot():
    depth_changes: list[int] = []
    buffer = OutboundBuffer(8, depth_changes.append)
    buffer.enqueue_initial(market_data(0), sequence=0)

    for sequence in range(1, 101):
        buffer.enqueue_heartbeat(heartbeat(sequence))
        buffer.enqueue_snapshot(market_data(sequence), sequence=sequence)
        assert buffer.qsize <= 8

    initial = await buffer.get()
    latest = await buffer.get()
    queued_heartbeat = await buffer.get()

    assert initial.payload["sequence"] == 0
    assert latest.payload["sequence"] == 100
    assert queued_heartbeat.payload["last_sequence"] == 100
    assert buffer.qsize == 0
    assert sum(depth_changes) == 0


@pytest.mark.asyncio
async def test_obsolete_snapshots_are_dropped_deterministically():
    buffer = OutboundBuffer(8)
    buffer.enqueue_initial(market_data(10), sequence=10)

    assert (
        buffer.enqueue_snapshot(market_data(12), sequence=12)
        == EnqueueDisposition.ENQUEUED
    )
    assert (
        buffer.enqueue_snapshot(market_data(11), sequence=11)
        == EnqueueDisposition.DROPPED
    )
    assert (
        buffer.enqueue_snapshot(market_data(12), sequence=12)
        == EnqueueDisposition.DROPPED
    )
    assert (
        buffer.enqueue_snapshot(market_data(13), sequence=13)
        == EnqueueDisposition.CONFLATED
    )

    assert (await buffer.get()).payload["sequence"] == 10
    assert (await buffer.get()).payload["sequence"] == 13


@pytest.mark.asyncio
async def test_heartbeat_never_overtakes_initial_and_is_coalesced():
    buffer = OutboundBuffer(8)
    buffer.enqueue_initial(market_data(3), sequence=3)
    assert buffer.enqueue_heartbeat(heartbeat(3)) == EnqueueDisposition.ENQUEUED
    assert buffer.enqueue_heartbeat(heartbeat(4)) == EnqueueDisposition.CONFLATED

    assert (await buffer.get()).payload["type"] == "market_data"
    assert (await buffer.get()).payload["last_sequence"] == 4


@pytest.mark.asyncio
async def test_fatal_error_and_close_retain_priority_after_initial():
    buffer = OutboundBuffer(8)
    buffer.enqueue_initial(market_data(0), sequence=0)
    buffer.enqueue_snapshot(market_data(1), sequence=1)
    buffer.enqueue_heartbeat(heartbeat(1))
    buffer.enqueue_error_and_close(
        {"type": "error", "code": "FAILED", "message": "failed"},
        close_code=1011,
    )

    frames = [await buffer.get(), await buffer.get(), await buffer.get()]

    assert [frame.kind for frame in frames] == [
        OutboundKind.MARKET_DATA,
        OutboundKind.ERROR,
        OutboundKind.CLOSE,
    ]
    assert frames[0].initial
    assert frames[2].close_code == 1011


@pytest.mark.asyncio
async def test_send_timeout_disconnects_slow_client_and_records_metrics():
    metrics = MetricsCollector("TEST")
    registry = WebSocketRegistry(
        metrics,
        queue_capacity=8,
        send_timeout_seconds=0.01,
    )
    socket = ControlledWebSocket(blocked=True)

    connection = await registry.register(
        "TEST",
        socket,
        market_data(0),
        initial_sequence=0,
    )
    await wait_until(lambda: connection.state == ConnectionState.CLOSED)

    values = metrics.websocket_metrics_snapshot()
    assert socket.closed
    assert socket.close_code == 1013
    assert connection.sender_task is not None
    assert connection.sender_task.cancelled()
    assert await registry.connection_count() == 0
    assert values["active_clients"] == 0
    assert values["slow_client_disconnects"] == 1
    assert values["send_failures"] == 1


@pytest.mark.asyncio
async def test_explicit_disconnect_cancels_sender_and_clears_queue():
    metrics = MetricsCollector("TEST")
    registry = WebSocketRegistry(
        metrics,
        queue_capacity=8,
        send_timeout_seconds=1,
    )
    socket = ControlledWebSocket(blocked=True)
    connection = await registry.register(
        "TEST",
        socket,
        market_data(0),
        initial_sequence=0,
    )
    await socket.send_started.wait()
    await registry.enqueue_market_data("TEST", market_data(1), sequence=1)

    await registry.disconnect(connection, code=1001)

    assert connection.state == ConnectionState.CLOSED
    assert connection.sender_task is not None
    assert connection.sender_task.cancelled()
    assert connection.outbound.qsize == 0
    assert metrics.websocket_metrics_snapshot()["queue_depth"] == 0


@pytest.mark.asyncio
async def test_one_sender_serializes_concurrent_snapshot_and_heartbeat_enqueues():
    metrics = MetricsCollector("TEST")
    registry = WebSocketRegistry(
        metrics,
        queue_capacity=8,
        send_timeout_seconds=1,
    )
    socket = ControlledWebSocket()
    connection = await registry.register(
        "TEST",
        socket,
        market_data(0),
        initial_sequence=0,
    )

    await asyncio.gather(
        *(
            registry.enqueue_market_data(
                "TEST",
                market_data(sequence),
                sequence=sequence,
            )
            for sequence in range(1, 51)
        )
    )
    for sequence in range(1, 51):
        registry.enqueue_heartbeat(connection, heartbeat(sequence))
    await wait_until(lambda: connection.outbound.qsize == 0)

    assert socket.max_active_sends == 1
    assert connection.last_successful_send_at is not None
    assert isinstance(connection.last_successful_send_at, datetime)
    await registry.disconnect(connection)


@pytest.mark.asyncio
async def test_websocket_prometheus_metrics_are_unlabeled_and_cleanup_to_zero():
    metrics = MetricsCollector("TEST")
    registry = WebSocketRegistry(
        metrics,
        queue_capacity=8,
        send_timeout_seconds=1,
    )
    socket = ControlledWebSocket(blocked=True)
    connection = await registry.register(
        "TEST",
        socket,
        market_data(0),
        initial_sequence=0,
    )
    await socket.send_started.wait()
    await registry.enqueue_market_data("TEST", market_data(1), sequence=1)
    await registry.enqueue_market_data("TEST", market_data(2), sequence=2)
    await registry.enqueue_market_data("TEST", market_data(1), sequence=1)

    before = metrics.prometheus_payload().decode()
    assert "mockx_websocket_active_clients 1.0" in before
    assert "mockx_websocket_outbound_queue_depth 1.0" in before
    assert "mockx_websocket_snapshots_conflated_total 1.0" in before
    assert "mockx_websocket_snapshots_dropped_total 1.0" in before
    assert "connection_id" not in before
    assert connection.connection_id not in before

    socket.release_send.set()
    await registry.disconnect(connection)
    after = metrics.prometheus_payload().decode()
    assert "mockx_websocket_active_clients 0.0" in after
    assert "mockx_websocket_outbound_queue_depth 0.0" in after

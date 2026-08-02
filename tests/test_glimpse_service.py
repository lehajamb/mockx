"""GLIMPSE snapshot, Soup session replay, and lifecycle integration tests."""

import asyncio
from datetime import datetime, timezone
from datetime import timedelta
from decimal import Decimal
import socket
import struct
import threading

import pytest

from src.mockx.configuration import SoupBinTCPTransportConfig, validate_config
from src.mockx.glimpse_encoder import (
    GlimpseEncoder,
    GlimpseOrderSnapshot,
    GlimpseSnapshot,
)
from src.mockx.glimpse_service import (
    GlimpseService,
    GlimpseServiceState,
    GlimpseSessionUnavailable,
)
from src.mockx.instruments import Instrument, InstrumentCatalog
from src.mockx.itch_encoder import ITCHEncoder
from src.mockx.main import MockExchange
from src.mockx.models import Side
from src.mockx.soupbintcp_transport import (
    PACKET_END_SESSION,
    PACKET_LOGIN_ACCEPTED,
    PACKET_LOGIN_REJECTED,
    PACKET_SEQUENCED_DATA,
    SoupPacketParser,
    encode_client_heartbeat,
    encode_debug,
    encode_login_request,
    encode_logout_request,
    encode_unsequenced_data,
    read_packet,
)

from .test_itch_integration import CapturingMoldTransport, itch_config, submit


NOW = datetime(2025, 1, 15, 9, 30, tzinfo=timezone.utc)


class DummyMetrics:
    pass


class FakeWriter:
    def __init__(self, *, fail_on_drain=None):
        self.writes = []
        self.drain_calls = 0
        self.fail_on_drain = fail_on_drain
        self.closed = False

    def write(self, wire):
        self.writes.append(bytes(wire))

    async def drain(self):
        self.drain_calls += 1
        if self.drain_calls == self.fail_on_drain:
            raise ConnectionResetError("forced disconnect")

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class BlockingWriter(FakeWriter):
    def __init__(self):
        super().__init__()
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def drain(self):
        self.drain_calls += 1
        if self.drain_calls == 1:
            self.blocked.set()
            await self.release.wait()


class BlockingSequencedWriter(FakeWriter):
    async def drain(self):
        self.drain_calls += 1
        if self.drain_calls == 2:
            await asyncio.Event().wait()


class BlockingEncoder:
    def __init__(self, delegate):
        self.delegate = delegate
        self.itch_encoder = delegate.itch_encoder
        self.started = threading.Event()
        self.release = threading.Event()

    def encode(self, snapshot):
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test encoder was not released")
        return self.delegate.encode(snapshot)


class FakeServer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def soup_config(**overrides):
    values = {
        "host": "127.0.0.1",
        "port": 18001,
        "session_prefix": "GS",
        "credentials": {"GLIMP": "SECRET"},
        "login_timeout_ms": 30_000,
        "write_timeout_ms": 5_000,
        "max_connections": 4,
        "replay_ttl_ms": 60_000,
        "max_cached_sessions": 4,
        "max_cached_bytes": 1_000_000,
    }
    values.update(overrides)
    return SoupBinTCPTransportConfig.model_validate(values)


def glimpse_encoder():
    instruments = InstrumentCatalog(
        {"TEST": Instrument("TEST", Decimal("1"), Decimal("1"), 2, 0)}
    )
    return GlimpseEncoder(
        ITCHEncoder({"TEST": 1}, instruments),
        market_category=b" ",
        financial_status=b" ",
        round_lot_size=1,
    )


def reader_with(wire):
    reader = asyncio.StreamReader()
    reader.feed_data(wire)
    return reader


def decoded(writes):
    parser = SoupPacketParser()
    packets = parser.feed(b"".join(writes))
    parser.finish()
    return packets


@pytest.mark.asyncio
async def test_interrupted_spin_resumes_exact_cached_messages():
    snapshot = GlimpseSnapshot(
        1,
        NOW,
        77,
        (GlimpseOrderSnapshot(11, "TEST", Side.BUY, 2, 100),),
    )

    async def capture():
        return snapshot

    service = GlimpseService(soup_config(), glimpse_encoder(), capture, DummyMetrics())
    service._set_state(GlimpseServiceState.READY)
    service.begin_run(1)

    first_writer = FakeWriter(fail_on_drain=3)
    await service._handle_client(
        reader_with(encode_login_request("glimp", "secret")),
        first_writer,
    )
    first_packets = decoded(first_writer.writes)
    assert first_packets[0].packet_type == PACKET_LOGIN_ACCEPTED
    session = first_packets[0].payload[:10].decode("ascii").strip()
    assert service.diagnostics()["cached_sessions"] == 1

    entry, latest = service._resume_spin(session, 0)
    assert latest == len(entry.messages)
    with pytest.raises(GlimpseSessionUnavailable, match="unavailable"):
        service._resume_spin(session, 1)
    entry.leased = False
    entry, after_last = service._resume_spin(session, len(entry.messages) + 1)
    assert after_last == len(entry.messages) + 1
    entry.leased = False
    with pytest.raises(GlimpseSessionUnavailable, match="sequence"):
        service._resume_spin(session, len(entry.messages) + 2)

    second_writer = FakeWriter()
    await service._handle_client(
        reader_with(
            encode_login_request(
                "GLIMP",
                "SECRET",
                requested_session=session,
                requested_sequence=2,
            )
        ),
        second_writer,
    )
    second_packets = decoded(second_writer.writes)
    assert second_packets[0].packet_type == PACKET_LOGIN_ACCEPTED
    assert int(second_packets[0].payload[10:30]) == 2
    assert second_packets[-1].packet_type == PACKET_END_SESSION
    assert all(
        packet.packet_type == PACKET_SEQUENCED_DATA
        for packet in second_packets[1:-1]
    )
    diagnostics = service.diagnostics()
    assert diagnostics["resumed_sessions"] == 1
    assert diagnostics["completed_sessions"] == 1
    assert diagnostics["cached_sessions"] == 0


@pytest.mark.asyncio
async def test_interrupted_spin_expires_lazily_by_monotonic_ttl():
    clock = [100.0]

    async def capture():
        return GlimpseSnapshot(1, NOW, 77, ())

    service = GlimpseService(
        soup_config(replay_ttl_ms=10),
        glimpse_encoder(),
        capture,
        DummyMetrics(),
        monotonic=lambda: clock[0],
    )
    service._set_state(GlimpseServiceState.READY)
    service.begin_run(1)
    writer = FakeWriter(fail_on_drain=2)
    await service._handle_client(
        reader_with(encode_login_request("GLIMP", "SECRET")),
        writer,
    )
    assert service.diagnostics()["cached_sessions"] == 1

    clock[0] += 0.011
    assert service.diagnostics()["cached_sessions"] == 0
    assert service.diagnostics()["cached_bytes"] == 0


def test_one_byte_prefix_still_generates_ten_byte_alphanumeric_sessions():
    async def capture():
        return GlimpseSnapshot(1, NOW, 1, ())

    service = GlimpseService(
        soup_config(session_prefix="G"),
        glimpse_encoder(),
        capture,
        DummyMetrics(),
    )
    assert service._next_session_name() == "G000000001"


@pytest.mark.asyncio
async def test_fresh_spin_enforces_session_and_byte_capacity_before_encoding():
    captures = 0

    async def capture():
        nonlocal captures
        captures += 1
        return GlimpseSnapshot(1, NOW, 1, ())

    service = GlimpseService(
        soup_config(max_cached_sessions=1),
        glimpse_encoder(),
        capture,
        DummyMetrics(),
    )
    service._set_state(GlimpseServiceState.READY)
    service.begin_run(1)
    entry = await service._fresh_spin()
    assert entry.wire_bytes == 94
    with pytest.raises(GlimpseSessionUnavailable, match="session capacity"):
        await service._fresh_spin()
    assert captures == 1

    byte_limited = GlimpseService(
        soup_config(max_cached_bytes=93),
        glimpse_encoder(),
        capture,
        DummyMetrics(),
    )
    byte_limited._set_state(GlimpseServiceState.READY)
    byte_limited.begin_run(1)
    with pytest.raises(GlimpseSessionUnavailable, match="exceeds replay capacity"):
        await byte_limited._fresh_spin()


@pytest.mark.asyncio
async def test_auth_and_unavailable_run_use_distinct_reject_codes():
    async def capture():
        return GlimpseSnapshot(1, NOW, 1, ())

    service = GlimpseService(soup_config(), glimpse_encoder(), capture, DummyMetrics())
    service._set_state(GlimpseServiceState.READY)

    unauthorized = FakeWriter()
    await service._handle_client(
        reader_with(encode_login_request("GLIMP", "WRONG")),
        unauthorized,
    )
    packet = decoded(unauthorized.writes)[0]
    assert packet.packet_type == PACKET_LOGIN_REJECTED
    assert packet.payload == b"A"

    unavailable = FakeWriter()
    await service._handle_client(
        reader_with(encode_login_request("GLIMP", "SECRET")),
        unavailable,
    )
    packet = decoded(unavailable.writes)[0]
    assert packet.packet_type == PACKET_LOGIN_REJECTED
    assert packet.payload == b"S"


@pytest.mark.asyncio
async def test_post_login_control_packets_are_accepted_until_logout():
    service = GlimpseService(
        soup_config(),
        glimpse_encoder(),
        lambda: None,
        DummyMetrics(),
    )
    reader = reader_with(
        encode_debug("still here")
        + encode_unsequenced_data(b"ignored")
        + encode_client_heartbeat()
        + encode_logout_request()
    )
    assert await service._receiver(reader, heartbeat_timeout_ms=100) == "logout"


@pytest.mark.asyncio
async def test_absolute_login_and_post_login_idle_timeouts_clean_up():
    async def capture():
        return GlimpseSnapshot(1, NOW, 1, ())

    login_timeout_service = GlimpseService(
        soup_config(login_timeout_ms=1),
        glimpse_encoder(),
        capture,
        DummyMetrics(),
    )
    idle_reader = asyncio.StreamReader()
    idle_writer = FakeWriter()
    await login_timeout_service._handle_client(idle_reader, idle_writer)
    assert idle_writer.closed
    assert login_timeout_service.diagnostics()["timeouts"] == 1

    client_idle_service = GlimpseService(
        soup_config(write_timeout_ms=1_000),
        glimpse_encoder(),
        capture,
        DummyMetrics(),
    )
    client_idle_service._set_state(GlimpseServiceState.READY)
    client_idle_service.begin_run(1)
    writer = BlockingSequencedWriter()
    await client_idle_service._handle_client(
        reader_with(
            encode_login_request(
                "GLIMP",
                "SECRET",
                heartbeat_timeout_ms=1,
            )
        ),
        writer,
    )
    diagnostics = client_idle_service.diagnostics()
    assert diagnostics["timeouts"] == 1
    assert diagnostics["cached_sessions"] == 1
    assert writer.closed


@pytest.mark.asyncio
async def test_listener_bind_failure_degrades_and_next_start_recovers():
    attempts = 0
    fake_server = FakeServer()

    async def server_factory(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("port unavailable")
        return fake_server

    async def capture():
        return GlimpseSnapshot(1, NOW, 1, ())

    service = GlimpseService(
        soup_config(),
        glimpse_encoder(),
        capture,
        DummyMetrics(),
        server_factory=server_factory,
    )
    assert not await service.start()
    assert service.state == GlimpseServiceState.DEGRADED
    assert await service.start()
    assert service.state == GlimpseServiceState.READY
    await service.stop()
    assert fake_server.closed


@pytest.mark.asyncio
async def test_lifecycle_capture_is_book_equivalent_and_marks_next_itch_sequence():
    raw = itch_config()
    raw["glimpse"] = {"enabled": True}
    raw["soupbintcp"] = {
        "host": "127.0.0.1",
        "port": 18001,
        "session_prefix": "GS",
        "credentials": {"GLIMP": "SECRET"},
    }
    exchange = MockExchange(validate_config(raw, environ={}))
    packets = []
    exchange.itch_feed.transport_factory = lambda config, on_error: (
        CapturingMoldTransport(config, on_error=on_error, packets=packets)
    )

    fake_server = FakeServer()

    async def server_factory(*args, **kwargs):
        return fake_server

    exchange.glimpse_service.server_factory = server_factory
    await exchange.start_components()
    try:
        await exchange.lifecycle.start_session("glimpse-cut")
        bid_id = submit(exchange, "B1", Side.BUY, 3, 99, "BIDDER")
        ask_id = submit(exchange, "S1", Side.SELL, 2, 101, "ASKER")
        submit(exchange, "B2", Side.BUY, 1, 101, "TAKER")

        snapshot = await exchange.lifecycle.capture_glimpse_snapshot()
        assert snapshot.run_generation == 1
        assert snapshot.itch_sequence == exchange.itch_feed.sequence_number
        assert snapshot.orders == (
            GlimpseOrderSnapshot(bid_id, "TEST", Side.BUY, 3, 99),
            GlimpseOrderSnapshot(ask_id, "TEST", Side.SELL, 1, 101),
        )

        messages = exchange.glimpse_service.encoder.encode(snapshot)
        assert [message[:1] for message in messages] == [b"R", b"H", b"A", b"A", b"G"]
        assert int(messages[-1][1:]) == snapshot.itch_sequence
        assert struct.unpack(">I", messages[3][20:24])[0] == 1

        stats = exchange.api._stats_response().glimpse
        assert stats.enabled
        assert stats.state == "ready"
        assert exchange.app.state.component_status["glimpse"] == "ready"
        prometheus = exchange.metrics.prometheus_payload().decode("utf-8")
        for metric_name in (
            "mockx_glimpse_connections_accepted_total",
            "mockx_glimpse_fresh_sessions_total",
            "mockx_glimpse_resumed_sessions_total",
            "mockx_glimpse_completed_sessions_total",
            "mockx_glimpse_rejected_sessions_total",
            "mockx_glimpse_sequenced_messages_total",
            "mockx_glimpse_sequenced_bytes_total",
            "mockx_glimpse_heartbeats_sent_total",
            "mockx_glimpse_timeouts_total",
            "mockx_glimpse_failures_total",
            "mockx_glimpse_active_connections",
            "mockx_glimpse_cached_sessions",
            "mockx_glimpse_cached_bytes",
            "mockx_glimpse_ready",
            "mockx_glimpse_degraded",
        ):
            assert metric_name in prometheus
            assert f"{metric_name}{{" not in prometheus
    finally:
        await exchange.stop_components()
    assert fake_server.closed


def available_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.asyncio
async def test_real_tcp_listener_accepts_fragmented_login_and_streams_clean_spin():
    raw = itch_config()
    raw["glimpse"] = {"enabled": True}
    raw["soupbintcp"] = {
        "host": "127.0.0.1",
        "port": available_tcp_port(),
        "session_prefix": "GS",
        "credentials": {"GLIMP": "SECRET"},
    }
    exchange = MockExchange(validate_config(raw, environ={}))
    packets = []
    exchange.itch_feed.transport_factory = lambda config, on_error: (
        CapturingMoldTransport(config, on_error=on_error, packets=packets)
    )
    await exchange.start_components()
    try:
        await exchange.lifecycle.start_session("tcp-spin")
        order_id = submit(exchange, "B1", Side.BUY, 2, 99, "BIDDER")
        expected_marker = exchange.itch_feed.sequence_number

        reader, writer = await asyncio.open_connection(
            raw["soupbintcp"]["host"], raw["soupbintcp"]["port"]
        )
        login = encode_debug("probe") + encode_login_request("GLIMP", "SECRET")
        for offset in range(0, len(login), 3):
            writer.write(login[offset : offset + 3])
            await writer.drain()

        login_accepted = await read_packet(reader)
        assert login_accepted.packet_type == PACKET_LOGIN_ACCEPTED
        assert int(login_accepted.payload[10:30]) == 1
        messages = []
        while True:
            packet = await asyncio.wait_for(read_packet(reader), timeout=1)
            if packet.packet_type == PACKET_END_SESSION:
                break
            assert packet.packet_type == PACKET_SEQUENCED_DATA
            messages.append(packet.payload)
        assert [message[:1] for message in messages] == [b"R", b"H", b"A", b"G"]
        assert struct.unpack(">Q", messages[2][11:19])[0] == order_id
        assert int(messages[-1][1:]) == expected_marker
        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_slow_tcp_writer_does_not_hold_exchange_lifecycle_lock():
    raw = itch_config()
    raw["glimpse"] = {"enabled": True}
    raw["soupbintcp"] = {
        "host": "127.0.0.1",
        "port": 18001,
        "session_prefix": "GS",
        "credentials": {"GLIMP": "SECRET"},
    }
    exchange = MockExchange(validate_config(raw, environ={}))
    exchange.itch_feed.transport_factory = lambda config, on_error: (
        CapturingMoldTransport(config, on_error=on_error, packets=[])
    )

    async def server_factory(*args, **kwargs):
        return FakeServer()

    exchange.glimpse_service.server_factory = server_factory
    await exchange.start_components()
    await exchange.lifecycle.start_session("slow-glimpse")
    writer = BlockingWriter()
    connection_task = asyncio.create_task(
        exchange.glimpse_service._handle_client(
            reader_with(encode_login_request("GLIMP", "SECRET")),
            writer,
        )
    )
    try:
        await asyncio.wait_for(writer.blocked.wait(), timeout=1)
        previous = exchange.scheduler.current_time
        await asyncio.wait_for(
            exchange.lifecycle.advance_to(previous + timedelta(milliseconds=1)),
            timeout=0.25,
        )
        assert exchange.scheduler.current_time == previous + timedelta(milliseconds=1)
    finally:
        writer.release.set()
        await asyncio.wait_for(connection_task, timeout=1)
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_snapshot_encoding_runs_off_loop_and_does_not_delay_lifecycle():
    raw = itch_config()
    raw["glimpse"] = {"enabled": True}
    raw["soupbintcp"] = {
        "host": "127.0.0.1",
        "port": 18001,
        "session_prefix": "GS",
        "credentials": {"GLIMP": "SECRET"},
    }
    exchange = MockExchange(validate_config(raw, environ={}))
    exchange.itch_feed.transport_factory = lambda config, on_error: (
        CapturingMoldTransport(config, on_error=on_error, packets=[])
    )

    async def server_factory(*args, **kwargs):
        return FakeServer()

    exchange.glimpse_service.server_factory = server_factory
    await exchange.start_components()
    await exchange.lifecycle.start_session("slow-encoding")
    blocking_encoder = BlockingEncoder(exchange.glimpse_service.encoder)
    exchange.glimpse_service.encoder = blocking_encoder
    connection_task = asyncio.create_task(
        exchange.glimpse_service._handle_client(
            reader_with(encode_login_request("GLIMP", "SECRET")),
            FakeWriter(),
        )
    )
    try:
        assert await asyncio.to_thread(blocking_encoder.started.wait, 1)
        previous = exchange.scheduler.current_time
        await asyncio.wait_for(
            exchange.lifecycle.advance_to(previous + timedelta(milliseconds=1)),
            timeout=0.25,
        )
        assert exchange.scheduler.current_time == previous + timedelta(milliseconds=1)
    finally:
        blocking_encoder.release.set()
        await asyncio.wait_for(connection_task, timeout=1)
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_connection_capacity_returns_session_unavailable_reject():
    snapshot = GlimpseSnapshot(1, NOW, 1, ())

    async def capture():
        return snapshot

    service = GlimpseService(
        soup_config(max_connections=1),
        glimpse_encoder(),
        capture,
        DummyMetrics(),
    )
    service._set_state(GlimpseServiceState.READY)
    service.begin_run(1)
    blocked_writer = BlockingWriter()
    first = asyncio.create_task(
        service._handle_client(
            reader_with(encode_login_request("GLIMP", "SECRET")),
            blocked_writer,
        )
    )
    await asyncio.wait_for(blocked_writer.blocked.wait(), timeout=1)
    rejected_writer = FakeWriter()
    await service._handle_client(
        reader_with(encode_login_request("GLIMP", "SECRET")),
        rejected_writer,
    )
    rejected = decoded(rejected_writer.writes)
    assert len(rejected) == 1
    assert rejected[0].packet_type == PACKET_LOGIN_REJECTED
    assert rejected[0].payload == b"S"
    blocked_writer.release.set()
    await asyncio.wait_for(first, timeout=1)

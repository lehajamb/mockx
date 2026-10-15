"""Owned ITCH lifecycle, wire semantics, and book-equivalence coverage."""

import asyncio
from datetime import datetime, timedelta, timezone
import struct

from hypothesis import given, settings, strategies as st
import pytest

from src.mockx.configuration import ConfigurationError, validate_config
from src.mockx.lifecycle import LifecycleError
from .api_helpers import make_native_exchange as MockExchange
from src.mockx.instruments import InstrumentCatalog
from src.mockx.moldudp64_transport import MoldUDP64Transport
from src.mockx.models import (
    CancelOrderCommand,
    DisplayedOrderAdded,
    DisplayedOrderExecuted,
    ITCHHeartbeatCommand,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)
from src.mockx.validation import OrderNormalizer, OrderValidationError, RawOrderInput

from .api_helpers import api_config
from .exact_helpers import exact_command


class CapturingDatagram:
    def __init__(self, packets):
        self.packets = packets

    def sendto(self, packet, address):
        self.packets.append(packet)

    def close(self):
        return None


class CapturingMoldTransport(MoldUDP64Transport):
    def __init__(self, config, *, on_error, packets):
        super().__init__(config, on_error=on_error)
        self.packets = packets

    async def start(self):
        self._next_sequence = 1
        self.datagram = CapturingDatagram(self.packets)
        self._transport = self.datagram
        self._session_active = True


def itch_config(**instrument_overrides):
    raw = api_config()
    raw["instruments"]["TEST"].update(instrument_overrides)
    raw["itch"] = {
        "market_category": " ",
        "financial_status": " ",
        "round_lot_size": 1,
    }
    raw["moldudp64"] = {
        "multicast_group": "239.192.0.1",
        "multicast_port": 18000,
        "session_prefix": "MX",
        "heartbeat_interval_ms": 100,
        "max_payload_bytes": 1400,
        "interface_address": "0.0.0.0",
        "ttl": 1,
    }
    return raw


def decode_packets(packets):
    expected_sequence = 1
    messages = []
    headers = []
    for packet in packets:
        sequence, count = struct.unpack(">QH", packet[10:20])
        headers.append((sequence, count))
        assert sequence == expected_sequence
        if count in (0, 0xFFFF):
            continue
        offset = 20
        for _ in range(count):
            size = struct.unpack(">H", packet[offset : offset + 2])[0]
            offset += 2
            messages.append(packet[offset : offset + size])
            offset += size
        assert offset == len(packet)
        expected_sequence += count
    return headers, messages


def replay_displayed_book(messages):
    orders = {}
    matches = []
    for message in messages:
        message_type = message[:1]
        if message_type == b"A":
            order_id = struct.unpack(">Q", message[11:19])[0]
            orders[order_id] = {
                "side": message[19:20],
                "qty": struct.unpack(">I", message[20:24])[0],
                "price": struct.unpack(">I", message[32:36])[0],
            }
        elif message_type in (b"E", b"C"):
            order_id = struct.unpack(">Q", message[11:19])[0]
            executed = struct.unpack(">I", message[19:23])[0]
            match = struct.unpack(">Q", message[23:31])[0]
            matches.append(match)
            orders[order_id]["qty"] -= executed
            if orders[order_id]["qty"] == 0:
                del orders[order_id]
        elif message_type == b"D":
            del orders[struct.unpack(">Q", message[11:19])[0]]
    return orders, matches


def authoritative_wire_book(exchange):
    scaled_tick = int(exchange.instruments.require("TEST").tick_size * 10_000)
    return {
        order.displayed_order_ref: {
            "side": b"B" if order.side == Side.BUY else b"S",
            "qty": order.leaves_qty_lots,
            "price": order.price_ticks * scaled_tick,
        }
        for order in exchange.engine.orders.values()
        if exchange.engine.books["TEST"].is_resting(order.id)
    }


def submit(exchange, cl_ord_id, side, qty, price, participant):
    order_id = exchange.engine.submit_order(
        exact_command(
            cl_ord_id=cl_ord_id,
            symbol="TEST",
            side=side,
            order_qty_lots=qty,
            price_ticks=price,
            tif=TimeInForce.GTC,
            submitted_at=exchange.scheduler.current_time,
            session_id=participant,
            participant_id=participant,
        )
    )
    exchange.engine.on_displayed_order_event = exchange.itch_feed.publish
    try:
        exchange.engine.process_order(ProcessOrderCommand(order_id, 0))
        exchange.itch_feed.flush()
    finally:
        exchange.itch_feed.flush()
    return order_id


@pytest.mark.asyncio
async def test_mock_exchange_feed_is_book_equivalent_and_sequences_messages():
    exchange = MockExchange(validate_config(itch_config(), environ={}))
    packets = []
    assert exchange.itch_feed is not None
    exchange.itch_feed.transport_factory = lambda config, on_error: (
        CapturingMoldTransport(config, on_error=on_error, packets=packets)
    )
    await exchange.start_components()
    await exchange.lifecycle.start_session("itch-run")
    assert exchange.itch_feed.active_session_name.startswith("MX")
    assert len(exchange.itch_feed.active_session_name) == 10

    exchange.itch_feed._tracking_number = 0xFFFF
    passive_id = submit(exchange, "S1", Side.SELL, 2, 100, "SELLER")
    exchange.itch_feed.flush()
    _, messages = decode_packets(packets)
    replayed, matches = replay_displayed_book(messages)
    assert replayed == {passive_id: {"side": b"S", "qty": 2, "price": 1_000_000}}
    assert matches == []
    add_message = next(message for message in messages if message[:1] == b"A")
    assert struct.unpack(">H", add_message[3:5])[0] == 0

    aggressor_id = submit(exchange, "B1", Side.BUY, 3, 100, "BUYER")
    exchange.itch_feed.flush()
    _, messages = decode_packets(packets)
    replayed, matches = replay_displayed_book(messages)
    assert replayed == {aggressor_id: {"side": b"B", "qty": 1, "price": 1_000_000}}
    assert matches == [1]
    assert all(message[:1] != b"P" for message in messages)
    assert {
        order.id: order.leaves_qty_lots
        for order in exchange.engine.orders.values()
        if exchange.engine.books["TEST"].is_resting(order.id)
    } == {aggressor_id: 1}

    exchange.engine.cancel_order(
        CancelOrderCommand(
            session_id="BUYER",
            participant_id="BUYER",
            cancel_cl_ord_id="C1",
            orig_cl_ord_id="B1",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=3,
            requested_at=exchange.scheduler.current_time,
            run_generation=1,
            ingress_sequence=1,
        )
    )
    exchange.itch_feed.flush()
    _, messages = decode_packets(packets)
    replayed, matches = replay_displayed_book(messages)
    assert replayed == authoritative_wire_book(exchange) == {}
    assert matches == [1]
    assert any(message[:1] == b"D" for message in messages)

    await exchange.lifecycle.advance_to(
        exchange.scheduler.current_time + timedelta(milliseconds=200)
    )
    await exchange.lifecycle.end_session()
    headers, _ = decode_packets(packets)
    assert any(count == 0 for _, count in headers)
    assert headers[-1][1] == 0xFFFF

    stats = exchange.api._stats_response().itch
    assert stats.sessions_started == 1
    assert stats.sessions_ended == 1
    assert stats.failures == 0
    prometheus = exchange.metrics.prometheus_payload().decode("utf-8")
    for metric_name in (
        "mockx_itch_sessions_started_total",
        "mockx_itch_messages_sent_total",
        "mockx_moldudp64_packets_sent_total",
        "mockx_moldudp64_heartbeats_sent_total",
        "mockx_itch_session_active",
        "mockx_itch_degraded",
        "mockx_moldudp64_next_sequence_number",
    ):
        assert metric_name in prometheus
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_execution_price_message_is_c_not_e_or_trade():
    exchange = MockExchange(validate_config(itch_config(), environ={}))
    packets = []
    assert exchange.itch_feed is not None
    exchange.itch_feed.transport_factory = lambda config, on_error: (
        CapturingMoldTransport(config, on_error=on_error, packets=packets)
    )
    await exchange.start_components()
    await exchange.lifecycle.start_session("price-message")
    now = exchange.scheduler.current_time
    exchange.itch_feed.publish(
        DisplayedOrderAdded(1, 77, "TEST", Side.SELL, 100, 2, now)
    )
    exchange.itch_feed.publish(
        DisplayedOrderExecuted(1, 77, "TEST", 100, 99, 1, 88, now)
    )
    exchange.itch_feed.flush()
    _, messages = decode_packets(packets)
    order_messages = [
        message for message in messages if message[:1] in {b"A", b"E", b"C", b"P"}
    ]
    assert [message[:1] for message in order_messages] == [b"A", b"C"]
    assert struct.unpack(">Q", order_messages[1][23:31])[0] == 88
    assert struct.unpack(">I", order_messages[1][32:36])[0] == 990_000
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_feed_start_failure_degrades_readiness_but_not_matching():
    exchange = MockExchange(validate_config(itch_config(), environ={}))
    assert exchange.itch_feed is not None

    class FailingMold(CapturingMoldTransport):
        async def start(self):
            raise OSError("multicast unavailable")

    exchange.itch_feed.transport_factory = lambda config, on_error: FailingMold(
        config, on_error=on_error, packets=[]
    )
    await exchange.start_components()
    with pytest.raises(LifecycleError):
        await exchange.lifecycle.start_session("degraded-run")
    assert exchange.lifecycle.state.value == "RUN_FAILED"
    assert exchange.app.state.component_status["itch"] == "degraded"
    status_code, readiness = exchange.api._readiness()
    assert status_code == 503
    assert readiness.status == "not_ready"
    assert not exchange.engine.orders

    await exchange.lifecycle.end_session()
    packets = []
    exchange.itch_feed.transport_factory = lambda config, on_error: (
        CapturingMoldTransport(config, on_error=on_error, packets=packets)
    )
    await exchange.lifecycle.start_session("recovered-run")
    assert exchange.itch_feed.state.value == "ready"
    assert exchange.itch_feed.active_session_name.startswith("MX")
    packet_count = len(packets)
    exchange.itch_feed.handle_heartbeat(ITCHHeartbeatCommand(1))
    assert len(packets) == packet_count
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_runtime_send_failure_degrades_only_feed():
    exchange = MockExchange(validate_config(itch_config(), environ={}))
    assert exchange.itch_feed is not None
    transports = []

    def factory(config, on_error):
        transport = CapturingMoldTransport(config, on_error=on_error, packets=[])
        transports.append(transport)
        return transport

    exchange.itch_feed.transport_factory = factory
    await exchange.start_components()
    await exchange.lifecycle.start_session("runtime-failure")

    def fail_send(_packet, _address):
        raise OSError("runtime multicast failure")

    transports[0].datagram.sendto = fail_send
    order_id = submit(exchange, "B1", Side.BUY, 1, 99, "BUYER")
    assert exchange.engine.books["TEST"].is_resting(order_id)
    assert exchange.itch_feed.state.value == "degraded"
    assert exchange.itch_feed.diagnostics()["failures"] == 1
    status_code, readiness = exchange.api._readiness()
    assert status_code == 503
    assert readiness.status == "not_ready"
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_realtime_scheduler_drives_idle_heartbeats():
    raw = itch_config()
    raw["scheduler"]["mode"] = "realtime"
    raw["scheduler"]["interval_ms"] = 10
    exchange = MockExchange(validate_config(raw, environ={}))
    packets = []
    assert exchange.itch_feed is not None
    exchange.itch_feed.transport_factory = lambda config, on_error: (
        CapturingMoldTransport(config, on_error=on_error, packets=packets)
    )
    await exchange.start_components()
    try:
        await exchange.lifecycle.start_session("realtime-heartbeat")
        for _ in range(100):
            headers, _ = decode_packets(packets)
            if any(count == 0 for _, count in headers):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("realtime scheduler did not emit an idle heartbeat")
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_generation_exhaustion_is_rejected_before_run_reset():
    exchange = MockExchange(validate_config(itch_config(), environ={}))
    await exchange.start_components()
    exchange.lifecycle.run_generation = 99_999_999
    with pytest.raises(LifecycleError, match="cannot form an ITCH session"):
        await exchange.lifecycle.start_session("too-many-runs")
    assert exchange.lifecycle.state.value == "STOPPED"
    assert exchange.engine.identifier_scope.generation == 0
    await exchange.stop_components()


@settings(max_examples=20, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from([Side.BUY, Side.SELL]),
            st.integers(min_value=1, max_value=5),
            st.integers(min_value=95, max_value=105),
        ),
        min_size=1,
        max_size=20,
    )
)
def test_property_based_wire_book_matches_after_every_command(actions):
    async def scenario():
        exchange = MockExchange(validate_config(itch_config(), environ={}))
        packets = []
        assert exchange.itch_feed is not None
        exchange.itch_feed.transport_factory = lambda config, on_error: (
            CapturingMoldTransport(config, on_error=on_error, packets=packets)
        )
        await exchange.start_components()
        try:
            await exchange.lifecycle.start_session("property-run")
            for index, (side, quantity, price) in enumerate(actions, start=1):
                submit(
                    exchange,
                    f"O{index}",
                    side,
                    quantity,
                    price,
                    f"P{index}",
                )
                exchange.itch_feed.flush()
                _, messages = decode_packets(packets)
                replayed, _ = replay_displayed_book(messages)
                assert replayed == authoritative_wire_book(exchange)
        finally:
            await exchange.stop_components()

    asyncio.run(scenario())


def test_itch_configuration_and_ingress_wire_bounds_are_strict():
    missing_transport = api_config()
    del missing_transport["moldudp64"]
    with pytest.raises(ConfigurationError, match="moldudp64"):
        validate_config(missing_transport, environ={})

    with pytest.raises(ConfigurationError, match="not representable"):
        validate_config(
            itch_config(tick_size="0.00001", price_display_precision=5),
            environ={},
        )

    long_symbol = itch_config()
    long_symbol["instruments"]["TOO-LONG-SYMBOL"] = long_symbol["instruments"].pop(
        "TEST"
    )
    with pytest.raises(ConfigurationError, match="exceeds 8 ASCII bytes"):
        validate_config(long_symbol, environ={})

    config = validate_config(itch_config(), environ={})
    normalizer = OrderNormalizer(
        InstrumentCatalog.from_config(config),
        itch_compatible=True,
    )
    common = {
        "session_id": "S",
        "participant_id": "P",
        "cl_ord_id": "1",
        "symbol": "TEST",
        "side": "BUY",
        "order_type": "LIMIT",
        "time_in_force": "GTC",
    }
    with pytest.raises(OrderValidationError, match="32-bit share"):
        normalizer.normalize(
            RawOrderInput(**common, quantity=str(1 << 32), price="1"),
            submitted_at=datetime.now(timezone.utc),
        )
    with pytest.raises(OrderValidationError, match=r"Price\(4\)"):
        normalizer.normalize(
            RawOrderInput(**common, quantity="1", price="500000"),
            submitted_at=datetime.now(timezone.utc),
        )

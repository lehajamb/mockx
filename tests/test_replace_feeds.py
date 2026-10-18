"""Replace preserves authoritative displayed-book and recovery semantics."""

import struct

import pytest
import pytest_asyncio

from src.mockx.configuration import validate_config
from src.mockx.glimpse_encoder import GlimpseEncoder
from src.mockx.main import MockExchange
from src.mockx.models import OrderStatus, OrderType, ReplaceOrderCommand, Side, TimeInForce

from .test_itch_integration import (
    CapturingMoldTransport,
    authoritative_wire_book,
    decode_packets,
    itch_config,
    replay_displayed_book,
    submit,
)


def replace(exchange, order, cl_ord_id, qty, price):
    return exchange.engine.replace_order(
        ReplaceOrderCommand(
            replace_cl_ord_id=cl_ord_id,
            orig_cl_ord_id=order.cl_ord_id,
            symbol=order.symbol,
            side=order.side,
            order_type=OrderType.LIMIT,
            order_qty_lots=qty,
            price_ticks=price,
            tif=TimeInForce.GTC,
            requested_at=exchange.scheduler.current_time,
            session_id=order.session_id,
            participant_id=order.participant_id,
            run_generation=exchange.lifecycle.run_generation,
            ingress_sequence=order.version + 1,
        )
    )


@pytest_asyncio.fixture
async def feed_exchange():
    exchange = MockExchange(validate_config(itch_config(), environ={}))
    packets = []
    exchange.itch_feed.transport_factory = lambda config, on_error: (
        CapturingMoldTransport(config, on_error=on_error, packets=packets)
    )
    await exchange.start_components()
    await exchange.lifecycle.start_session("replace-feed")
    try:
        yield exchange, packets
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_reduction_uses_x_preserving_reference_and_fifo(feed_exchange):
    exchange, packets = feed_exchange
    first = exchange.engine.orders[submit(exchange, "B1", Side.BUY, 10, 100, "P1")]
    second_id = submit(exchange, "B2", Side.BUY, 4, 100, "P2")
    reference = first.displayed_order_ref
    sequence = exchange.engine.market_data_sequence
    _, before = decode_packets(packets)

    replace(exchange, first, "R1", 7, 100)

    _, messages = decode_packets(packets)
    updates = messages[len(before):]
    assert [m[:1] for m in updates] == [b"X"]
    assert struct.unpack(">QI", updates[0][11:23]) == (reference, 3)
    assert first.displayed_order_ref == reference
    assert [o.id for o in exchange.engine.books["TEST"].bids[0].orders] == [first.id, second_id]
    assert exchange.engine.market_data_sequence == sequence + 1
    assert replay_displayed_book(messages)[0] == authoritative_wire_book(exchange)


@pytest.mark.asyncio
async def test_noop_emits_ack_without_book_sequence_or_feed_change(feed_exchange):
    exchange, packets = feed_exchange
    order = exchange.engine.orders[submit(exchange, "B1", Side.BUY, 10, 100, "P1")]
    before = list(packets)
    sequence = exchange.engine.market_data_sequence
    reference = order.displayed_order_ref
    report = replace(exchange, order, "R1", 10, 100)
    assert report.cl_ord_id == "R1"
    assert report.orig_cl_ord_id == "B1"
    assert report.exec_type.value == "REPLACED"
    assert packets == before
    assert exchange.engine.market_data_sequence == sequence
    assert order.displayed_order_ref == reference


@pytest.mark.asyncio
@pytest.mark.parametrize("qty,price", [(12, 100), (10, 99)])
async def test_requeue_uses_fresh_reference_and_glimpse_cut(feed_exchange, qty, price):
    exchange, packets = feed_exchange
    order = exchange.engine.orders[submit(exchange, "B1", Side.BUY, 10, 100, "P1")]
    submit(exchange, "B2", Side.BUY, 4, 100, "P2")
    old_reference = order.displayed_order_ref
    _, before = decode_packets(packets)
    replace(exchange, order, "R1", qty, price)
    _, messages = decode_packets(packets)
    assert [m[:1] for m in messages[len(before):]] == [b"D", b"A"]
    assert order.displayed_order_ref != old_reference
    assert replay_displayed_book(messages)[0] == authoritative_wire_book(exchange)

    cut = await exchange.lifecycle.capture_glimpse_snapshot()
    assert order.displayed_order_ref in {o.order_ref_num for o in cut.orders}
    snapshot = GlimpseEncoder(
        exchange.itch_feed.encoder,
        market_category=b" ",
        financial_status=b" ",
        round_lot_size=1,
    ).encode(cut)
    assert replay_displayed_book(snapshot)[0] == authoritative_wire_book(exchange)
    assert int(snapshot[-1][1:]) == exchange.itch_feed.sequence_number
    replace(exchange, order, "R2", qty - 1, price)
    _, later = decode_packets(packets)
    assert replay_displayed_book(list(snapshot[:-1]) + later[len(messages):])[0] == authoritative_wire_book(exchange)


@pytest.mark.asyncio
async def test_crossing_replace_publishes_delete_passive_execution_and_new_add(feed_exchange):
    exchange, packets = feed_exchange
    order = exchange.engine.orders[submit(exchange, "B1", Side.BUY, 10, 100, "BUYER")]
    submit(exchange, "S1", Side.SELL, 4, 105, "SELLER")
    old_reference = order.displayed_order_ref
    _, before = decode_packets(packets)
    replace(exchange, order, "R1", 10, 106)
    _, messages = decode_packets(packets)
    assert [m[:1] for m in messages[len(before):]] == [b"D", b"E", b"A"]
    assert order.displayed_order_ref != old_reference
    assert order.filled_qty_lots == 4
    assert order.status == OrderStatus.PARTIAL
    assert replay_displayed_book(messages)[0] == authoritative_wire_book(exchange)


@pytest.mark.asyncio
async def test_completion_and_stp_remove_only_existing_display_references(feed_exchange):
    exchange, packets = feed_exchange
    order = exchange.engine.orders[submit(exchange, "B1", Side.BUY, 10, 100, "OWNER")]
    submit(exchange, "S0", Side.SELL, 3, 100, "OTHER")
    _, before = decode_packets(packets)
    replace(exchange, order, "R1", 2, 100)
    _, messages = decode_packets(packets)
    assert [m[:1] for m in messages[len(before):]] == [b"X"]
    assert order.order_qty_lots == order.filled_qty_lots == 3
    assert replay_displayed_book(messages)[0] == authoritative_wire_book(exchange) == {}

    next_order = exchange.engine.orders[submit(exchange, "B2", Side.BUY, 5, 100, "OWNER")]
    submit(exchange, "S1", Side.SELL, 2, 105, "OWNER")
    _, before = decode_packets(packets)
    replace(exchange, next_order, "R2", 5, 105)
    _, messages = decode_packets(packets)
    assert [m[:1] for m in messages[len(before):]] == [b"D", b"D"]
    assert replay_displayed_book(messages)[0] == authoritative_wire_book(exchange) == {}
    references = [struct.unpack(">Q", m[11:19])[0] for m in messages if m[:1] == b"A"]
    assert len(references) == len(set(references))

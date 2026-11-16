"""Bulk bootstrap changes datagrams only, preserving native message semantics."""

from math import ceil

import pytest

from src.mockx.configuration import validate_config
from src.mockx.main import MockExchange
from src.mockx.models import Side
from tests.test_itch_integration import (
    CapturingMoldTransport,
    authoritative_wire_book,
    decode_packets,
    itch_config,
    replay_displayed_book,
    submit,
)


@pytest.mark.asyncio
async def test_large_native_seed_uses_packetizer_without_changing_messages_or_glimpse_cut():
    exchange = MockExchange(validate_config(itch_config(), environ={}))
    packets = []
    exchange.itch_feed.transport_factory = (
        lambda config, on_error: CapturingMoldTransport(
            config,
            on_error=on_error,
            packets=packets,
        )
    )
    rows = tuple(
        dict(
            symbol="TEST",
            side="BUY" if index < 3006 else "SELL",
            price="99" if index < 3006 else "101",
            quantity="1",
            session_id="FIX.4.4:MOCKX1->CLIENT1",
            cl_ord_id=f"seed-{index}",
        )
        for index in range(3007)
    )
    await exchange.start_components()
    try:
        await exchange.lifecycle.start_session("bulk-native", initial_orders=rows)
        # 36 Add Order blocks fit each 1,400-byte Mold datagram, plus bootstrap.
        assert len(packets) == 1 + ceil(len(rows) / 36)
        assert all(len(packet) <= 1400 for packet in packets)
        headers, messages = decode_packets(packets)
        adds = [message for message in messages if message[:1] == b"A"]
        assert len(adds) == len(rows)
        expected = [
            exchange.itch_feed.encoder.add_order_no_mpid(
                order.symbol,
                order.displayed_order_ref,
                order.side,
                order.leaves_qty_lots,
                order.price_ticks,
                exchange.scheduler.current_time,
                tracking_number=6 + index,
            )
            for index, order in enumerate(exchange.engine.orders.values())
        ]
        assert adds == expected
        assert replay_displayed_book(messages) == (
            authoritative_wire_book(exchange),
            [],
        )
        glimpse = await exchange.lifecycle.capture_glimpse_snapshot()
        assert (
            glimpse.itch_sequence
            == exchange.itch_feed.sequence_number
            == len(messages) + 1
        )
        assert len(glimpse.orders) == len(rows)
        before = len(packets)
        submit(exchange, "normal-live", Side.BUY, 2, 98, "live-owner")
        assert len(packets) == before + 1
        assert decode_packets(packets)[0][-1] == (glimpse.itch_sequence, 1)
    finally:
        await exchange.stop_components()

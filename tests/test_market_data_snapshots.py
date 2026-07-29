"""Deep immutability, ordering, sequence, and trade publication tests."""

from dataclasses import FrozenInstanceError

import pytest

from src.mockx.models import Side

from .api_helpers import make_exchange, process_order


def test_snapshot_is_deeply_immutable_after_live_book_changes():
    exchange = make_exchange()
    process_order(exchange, "BID-1", Side.BUY, 5, 100)
    snapshot = exchange.engine.get_market_data_snapshot("TEST")
    original = snapshot.bids

    process_order(exchange, "BID-2", Side.BUY, 3, 100)

    assert snapshot.bids == original
    assert snapshot.bids[0].level_qty_lots == 5
    assert snapshot.bids[0].order_count == 1
    with pytest.raises(FrozenInstanceError):
        snapshot.sequence = 99
    with pytest.raises(FrozenInstanceError):
        snapshot.bids[0].level_qty_lots = 99


def test_levels_are_sorted_complete_and_zero_levels_are_absent():
    exchange = make_exchange()
    process_order(exchange, "B99", Side.BUY, 1, 99)
    process_order(exchange, "B101", Side.BUY, 2, 101)
    process_order(exchange, "B100", Side.BUY, 3, 100)
    process_order(exchange, "A103", Side.SELL, 4, 103)
    process_order(exchange, "A102", Side.SELL, 5, 102)

    snapshot = exchange.engine.get_market_data_snapshot("TEST")
    assert [level.price_ticks for level in snapshot.bids] == [101, 100, 99]
    assert [level.price_ticks for level in snapshot.asks] == [102, 103]
    assert all(
        level.level_qty_lots > 0 and level.order_count > 0
        for level in snapshot.bids
    )
    assert snapshot.bids[1].level_qty_lots == 3
    assert snapshot.bids[1].level_qty_lots == (
        snapshot.bids[1].strategy_qty_lots + snapshot.bids[1].market_qty_lots
    )


def test_empty_and_one_sided_snapshot_shapes_are_valid():
    exchange = make_exchange()
    empty = exchange.engine.get_market_data_snapshot("TEST")
    assert empty.sequence == 0
    assert empty.bids == ()
    assert empty.asks == ()

    process_order(exchange, "BID", Side.BUY, 1, 10)
    bid_only = exchange.engine.get_market_data_snapshot("TEST")
    assert len(bid_only.bids) == 1
    assert bid_only.asks == ()


@pytest.mark.anyio
async def test_global_sequence_is_monotonic_and_resets_with_session_start():
    exchange = make_exchange()
    published = []
    exchange.engine.on_market_data_update = published.append

    process_order(exchange, "ONE", Side.BUY, 1, 10)
    process_order(exchange, "TWO", Side.SELL, 1, 11)
    process_order(exchange, "THREE", Side.BUY, 1, 9)

    assert [snapshot.sequence for snapshot in published] == [1, 2, 3]
    await exchange.lifecycle.start_session("sequence-reset")
    assert exchange.engine.get_market_data_snapshot("TEST").sequence == 0


def test_multiple_fills_are_included_once_then_not_repeated():
    exchange = make_exchange()
    published = []
    original = exchange.engine.on_market_data_update

    def observe(snapshot):
        published.append(snapshot)
        original(snapshot)

    exchange.engine.on_market_data_update = observe
    process_order(exchange, "ASK-1", Side.SELL, 1, 100)
    process_order(exchange, "ASK-2", Side.SELL, 2, 101)
    process_order(exchange, "TAKER", Side.BUY, 3, 101)

    fill_update = published[-1]
    assert [trade.trade_id for trade in fill_update.trades] == [
        "G00000000-T00000001",
        "G00000000-T00000002",
    ]
    assert all(trade.aggressor_side is Side.BUY for trade in fill_update.trades)

    process_order(exchange, "NONTRADE", Side.BUY, 1, 90)
    assert published[-1].trades == ()

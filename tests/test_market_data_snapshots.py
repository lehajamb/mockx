"""On-demand diagnostics, immutable levels and native trade effects."""

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
        level.level_qty_lots > 0 and level.order_count > 0 for level in snapshot.bids
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
    sequences = []
    for name, side, price in (
        ("ONE", Side.BUY, 10),
        ("TWO", Side.SELL, 11),
        ("THREE", Side.BUY, 9),
    ):
        process_order(exchange, name, side, 1, price)
        sequences.append(exchange.engine.get_market_data_snapshot("TEST").sequence)
    assert sequences == [1, 2, 3]
    await exchange.lifecycle.start_session("sequence-reset")
    assert exchange.engine.get_market_data_snapshot("TEST").sequence == 0
    await exchange.stop_components()


def test_hot_matching_creates_native_trades_once_without_full_snapshot(monkeypatch):
    exchange = make_exchange()
    trades = []
    original = exchange.engine.on_trade

    def observe(trade):
        trades.append(trade)
        original(trade)

    def forbidden(*args, **kwargs):
        raise AssertionError("Hot order processing must not construct full snapshots")

    monkeypatch.setattr(exchange.engine, "_create_market_data_snapshot", forbidden)
    exchange.engine.on_trade = observe
    process_order(exchange, "ASK-1", Side.SELL, 1, 100)
    process_order(exchange, "ASK-2", Side.SELL, 2, 101)
    process_order(exchange, "TAKER", Side.BUY, 3, 101)
    assert [trade.exchange_trade_id for trade in trades] == [
        "G00000000-T00000001",
        "G00000000-T00000002",
    ]
    assert all(trade.aggressor_side is Side.BUY for trade in trades)
    process_order(exchange, "NONTRADE", Side.BUY, 1, 90)
    assert len(trades) == 2

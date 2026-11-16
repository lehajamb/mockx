"""Large immutable books reuse unchanged formatting without sharing payloads."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import asyncio
import pytest

from src.mockx.api import market_data_message
from src.mockx.instruments import Instrument
from src.mockx.models import MarketDataSnapshot, PriceLevelSnapshot, Side, TradeSnapshot


def test_unchanged_depth_reuses_exact_formatting_and_updates_only_changed_level(
    monkeypatch,
):
    instrument = Instrument("CACHE-TEST", Decimal(".01"), Decimal(".00001"), 2, 5)
    level = PriceLevelSnapshot(10001, 100, 25, 75, 2)
    snapshot = MarketDataSnapshot(
        "CACHE-TEST",
        instrument,
        1,
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        (level,),
        (),
    )
    calls = []
    format_price, format_quantity = Instrument.format_price, Instrument.format_quantity

    def price(self, value):
        calls.append(("price", value))
        return format_price(self, value)

    def quantity(self, value):
        calls.append(("quantity", value))
        return format_quantity(self, value)

    monkeypatch.setattr(Instrument, "format_price", price)
    monkeypatch.setattr(Instrument, "format_quantity", quantity)
    first = market_data_message(snapshot).model_dump(mode="json")
    assert len(calls) == 4
    first["bids"][0]["qty"] = "corrupted external payload"
    unchanged = market_data_message(replace(snapshot, sequence=2)).model_dump(
        mode="json"
    )
    assert (
        len(calls) == 4
    ), "Unchanged immutable depth should not repeat exact Decimal formatting"
    assert unchanged["bids"] == [
        {
            "price": "100.01",
            "qty": "0.00100",
            "strategy_qty": "0.00025",
            "market_qty": "0.00075",
            "order_count": 2,
        }
    ]
    changed_level = replace(
        level, level_qty_lots=101, market_qty_lots=76, order_count=3
    )
    trade = TradeSnapshot("T1", 10001, 1, Side.BUY, snapshot.timestamp)
    changed = market_data_message(
        replace(snapshot, sequence=3, bids=(changed_level,), trades=(trade,))
    ).model_dump(mode="json")
    assert len(calls) == 10  # One changed level and the real trade.
    assert changed["bids"][0]["qty"] == "0.00101"
    assert changed["bids"][0]["market_qty"] == "0.00076"
    assert changed["bids"][0]["order_count"] == 3
    assert changed["trades"] == [
        {
            "trade_id": "T1",
            "price": "100.01",
            "qty": "0.00001",
            "aggressor_side": "BUY",
            "timestamp": "1970-01-01T00:00:00Z",
        }
    ]

    other_metadata = replace(
        instrument, tick_size=Decimal(".1"), price_display_precision=1
    )
    other = market_data_message(
        replace(snapshot, instrument=other_metadata)
    ).model_dump(mode="json")
    assert other["bids"][0]["price"] == "1000.1"
    assert len(calls) == 14


def test_level_format_cache_has_a_fixed_memory_bound():
    from src.mockx.api import _formatted_level

    assert _formatted_level.cache_info().maxsize == 16_384


def test_validated_level_responses_are_reused_immutable_and_export_independently():
    from pydantic import ValidationError
    from src.mockx.api import _book_payload

    instrument = Instrument("MODEL-CACHE", Decimal(".01"), Decimal(".001"), 2, 3)
    level = PriceLevelSnapshot(10001, 100, 25, 75, 2)
    snapshot = MarketDataSnapshot(
        instrument.symbol,
        instrument,
        1,
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        (level,),
        (),
    )
    first = market_data_message(snapshot)
    second = market_data_message(replace(snapshot, sequence=2))
    assert first.bids[0] is second.bids[0], "Reuse already validated immutable levels"
    with pytest.raises(ValidationError, match="frozen"):
        first.bids[0].qty = "corrupted shared model"
    original = second.model_dump(mode="json")
    exported = first.model_dump(mode="json")
    exported["bids"][0]["qty"] = "corrupted exported dictionary"
    first.bids.clear()
    rest = _book_payload(snapshot)
    rest["bids"][0]["qty"] = "corrupted REST dictionary"
    assert second.model_dump(mode="json") == original
    assert _book_payload(snapshot)["bids"][0]["qty"] == "0.100"
    assert original == {
        "type": "market_data",
        "symbol": "MODEL-CACHE",
        "sequence": 2,
        "timestamp": "1970-01-01T00:00:00Z",
        "bids": [
            {
                "price": "100.01",
                "qty": "0.100",
                "strategy_qty": "0.025",
                "market_qty": "0.075",
                "order_count": 2,
            }
        ],
        "asks": [],
        "trades": [],
    }


@pytest.mark.parametrize("interval", [-1, 60_001, True, 1.5, "50"])
def test_snapshot_interval_requires_bounded_integer_milliseconds(interval):
    from src.mockx.configuration import ConfigurationError, validate_config
    from tests.api_helpers import api_config

    raw = api_config()
    raw["websocket"]["snapshot_interval_ms"] = interval
    with pytest.raises(ConfigurationError):
        validate_config(raw, environ={})


@pytest.mark.asyncio
async def test_bounded_snapshot_conflation_keeps_final_cut_and_immediate_trades(
    monkeypatch,
):
    from src.mockx.configuration import validate_config
    from src.mockx.main import MockExchange
    from tests.api_helpers import api_config

    raw = api_config()
    raw["websocket"]["snapshot_interval_ms"] = 50
    exchange = MockExchange(validate_config(raw, environ={}))
    frames = []

    async def observed(_symbol):
        return 1

    async def record(_symbol, message, *, sequence):
        frames.append(message)

    monkeypatch.setattr(exchange.api.registry, "connection_count", observed)
    monkeypatch.setattr(exchange.api.registry, "enqueue_market_data", record)
    await exchange.start_components()
    try:
        await exchange.lifecycle.start_session("coalesced")
        snapshot = exchange.engine.get_market_data_snapshot("TEST")
        for sequence in range(1, 1001):
            exchange._on_market_data_update(replace(snapshot, sequence=sequence))
        assert len(exchange._latest_snapshots) == 1
        assert len(exchange._pending_broadcasts) == 1
        await asyncio.sleep(0.01)
        assert frames == []
        trade = TradeSnapshot("T1", 100, 2, Side.BUY, snapshot.timestamp)
        exchange._on_market_data_update(
            replace(snapshot, sequence=1001, trades=(trade,))
        )
        exchange._on_market_data_update(replace(snapshot, sequence=1002))
        await asyncio.sleep(0.01)
        assert [frame["sequence"] for frame in frames] == [1001]
        assert frames[0]["trades"][0]["trade_id"] == "T1"
        # Session end wakes the timer and fences the exact latest cut before ENDING.
        await exchange.lifecycle.end_session()
        assert [frame["sequence"] for frame in frames] == [1001, 1002]
        assert not exchange._latest_snapshots
        assert not exchange._has_pending_broadcasts()
        await exchange.lifecycle.start_session("next-generation")
        exchange._on_market_data_update(replace(snapshot, sequence=1003))
        await exchange._drain_broadcasts()
        assert [frame["sequence"] for frame in frames] == [1001, 1002]
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_snapshot_conflation_flushes_before_reset_and_cleans_up_on_shutdown(
    monkeypatch,
):
    from src.mockx.configuration import validate_config
    from src.mockx.main import MockExchange
    from tests.api_helpers import api_config

    raw = api_config()
    raw["websocket"]["snapshot_interval_ms"] = 60_000
    exchange = MockExchange(validate_config(raw, environ={}))
    recorded = []
    original = exchange.api.broadcast_market_data

    async def observe(snapshot):
        recorded.append((snapshot.run_generation, exchange.lifecycle.run_generation))
        await original(snapshot)

    monkeypatch.setattr(exchange.api, "broadcast_market_data", observe)
    await exchange.start_components()
    try:
        await exchange.lifecycle.start_session("first")
        exchange._on_market_data_update(
            exchange.engine.get_market_data_snapshot("TEST")
        )
        await asyncio.wait_for(exchange.lifecycle.end_session(), timeout=1)
        await asyncio.wait_for(exchange.lifecycle.start_session("second"), timeout=1)
        assert recorded == [(1, 1)]
        exchange._on_market_data_update(
            exchange.engine.get_market_data_snapshot("TEST")
        )
    finally:
        await exchange.stop_components()
    assert not exchange._latest_snapshots
    assert not exchange._has_pending_broadcasts()

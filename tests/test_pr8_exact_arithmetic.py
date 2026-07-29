"""PR 8B acceptance tests for exact tick/lot arithmetic and boundaries."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.mockx.api import _book_payload
from src.mockx.configuration import MAX_INSTRUMENT_UNITS, validate_config
from src.mockx.engine import MatchingEngine
from src.mockx.fix_fields import execution_report_fields
from src.mockx.instruments import InstrumentCatalog
from src.mockx.latency import LatencyScheduler
from src.mockx.metrics import MetricsCollector
from src.mockx.models import (
    CancelOrderCommand,
    ExecutionParams,
    ExecutionType,
    NewOrderCommand,
    OrderStatus,
    OrderType,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)
from src.mockx.validation import (
    OrderNormalizer,
    OrderValidationCode,
    OrderValidationError,
    RawOrderInput,
)


NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)


def catalog(
    *,
    tick_size: str = "0.01",
    lot_size: str = "0.001",
    price_precision: int | None = 2,
    quantity_precision: int | None = 3,
) -> InstrumentCatalog:
    instrument = {
        "tick_size": tick_size,
        "lot_size": lot_size,
    }
    if price_precision is not None:
        instrument["price_display_precision"] = price_precision
    if quantity_precision is not None:
        instrument["quantity_display_precision"] = quantity_precision
    config = validate_config(
        {
            "exchange_name": "Exact",
            "instruments": {"TEST": instrument},
            "latency": {"network_ms": 0, "engine_ms": 0},
            "execution": {"reject_prob": 0.0, "random_seed": 42},
            "admin_api": {
                "host": "127.0.0.1",
                "port": 8000,
                "enabled": True,
            },
            "fix": {"enabled": False, "session_participants": {}},
            "scheduler": {"mode": "external"},
            "websocket": {},
            "cors_origins": [],
            "prometheus": {"enabled": False},
        },
        environ={},
    )
    return InstrumentCatalog.from_config(config)


def raw_order(**overrides) -> RawOrderInput:
    values = {
        "session_id": "REST:exact",
        "participant_id": "participant-exact",
        "cl_ord_id": "EXACT-1",
        "symbol": "TEST",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "0.1",
        "price": "0.3",
        "time_in_force": "GTC",
    }
    values.update(overrides)
    return RawOrderInput(**values)


def engine_stack(
    instruments: InstrumentCatalog | None = None,
) -> tuple[MatchingEngine, list, list, list]:
    scheduler = LatencyScheduler()
    engine = MatchingEngine(
        ExecutionParams(
            network_latency_ms=0,
            engine_latency_ms=0,
            reject_prob=0.0,
            rand_seed=42,
        ),
        scheduler,
        instruments or catalog(),
    )
    reports = []
    trades = []
    snapshots = []
    engine.on_execution_report = reports.append
    engine.on_trade = trades.append
    engine.on_market_data_update = snapshots.append
    return engine, reports, trades, snapshots


def submit(
    engine: MatchingEngine,
    *,
    cl_ord_id: str,
    participant_id: str,
    side: Side,
    order_qty_lots: int,
    price_ticks: int | None,
    tif: TimeInForce = TimeInForce.GTC,
) -> int:
    order_id = engine.submit_order(
        NewOrderCommand(
            cl_ord_id=cl_ord_id,
            participant_id=participant_id,
            symbol="TEST",
            side=side,
            order_type=(
                OrderType.MARKET if price_ticks is None else OrderType.LIMIT
            ),
            order_qty_lots=order_qty_lots,
            price_ticks=price_ticks,
            tif=tif,
            submitted_at=NOW,
            session_id=f"SESSION:{participant_id}",
        )
    )
    engine.process_order(ProcessOrderCommand(order_id, 0))
    return order_id


def test_equivalent_exact_decimals_map_to_one_tick_level():
    instruments = catalog(
        tick_size="0.1",
        lot_size="0.1",
        price_precision=1,
        quantity_precision=1,
    )
    normalizer = OrderNormalizer(instruments)

    first = normalizer.normalize(raw_order(price="0.3"), submitted_at=NOW)
    second = normalizer.normalize(
        raw_order(
            cl_ord_id="EXACT-2",
            price=Decimal("0.1") + Decimal("0.2"),
        ),
        submitted_at=NOW,
    )

    assert first.price_ticks == second.price_ticks == 3
    engine, _, _, _ = engine_stack(instruments)
    submit(
        engine,
        cl_ord_id="LEVEL-1",
        participant_id="participant-1",
        side=Side.BUY,
        order_qty_lots=1,
        price_ticks=first.price_ticks,
    )
    submit(
        engine,
        cl_ord_id="LEVEL-2",
        participant_id="participant-2",
        side=Side.BUY,
        order_qty_lots=1,
        price_ticks=second.price_ticks,
    )
    assert len(engine.books["TEST"].bids) == 1
    assert engine.books["TEST"].bids[0].level_qty_lots == 2


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"quantity": "0.1005"}, OrderValidationCode.OFF_LOT_QUANTITY),
        ({"price": "0.305"}, OrderValidationCode.OFF_TICK_PRICE),
    ],
)
def test_off_increment_values_are_rejected_without_rounding(overrides, code):
    normalizer = OrderNormalizer(catalog())
    with pytest.raises(OrderValidationError) as caught:
        normalizer.normalize(raw_order(**overrides), submitted_at=NOW)
    assert caught.value.code == code


def test_very_small_and_signed_64_bit_boundary_values_are_exact():
    small = catalog(
        tick_size="0.00000001",
        lot_size="0.00000001",
        price_precision=None,
        quantity_precision=None,
    ).require("TEST")
    assert small.price_to_ticks("0.00000003") == 3
    assert small.quantity_to_lots("0.00000002") == 2
    assert small.format_price(3) == "0.00000003"
    assert small.format_quantity(2) == "0.00000002"

    large = catalog(
        tick_size="1",
        lot_size="1",
        price_precision=0,
        quantity_precision=0,
    ).require("TEST")
    assert large.price_to_ticks(str(MAX_INSTRUMENT_UNITS)) == (
        MAX_INSTRUMENT_UNITS
    )
    with pytest.raises(Exception, match="signed 64-bit"):
        large.price_to_ticks(str(MAX_INSTRUMENT_UNITS + 1))


def test_partial_fills_and_cancel_preserve_integer_conservation():
    engine, reports, trades, _ = engine_stack()
    passive_id = submit(
        engine,
        cl_ord_id="PASSIVE",
        participant_id="maker",
        side=Side.SELL,
        order_qty_lots=10,
        price_ticks=10_000,
    )
    submit(
        engine,
        cl_ord_id="TAKER-1",
        participant_id="taker-1",
        side=Side.BUY,
        order_qty_lots=3,
        price_ticks=10_000,
    )
    submit(
        engine,
        cl_ord_id="TAKER-2",
        participant_id="taker-2",
        side=Side.BUY,
        order_qty_lots=4,
        price_ticks=10_000,
    )

    passive = engine.orders[passive_id]
    assert [trade.qty_lots for trade in trades] == [3, 4]
    assert passive.filled_qty_lots == 7
    assert passive.leaves_qty_lots == 3
    assert passive.filled_notional_tick_lots == 70_000
    engine.cancel_order(
        CancelOrderCommand(
            cancel_cl_ord_id="CANCEL-PASSIVE",
            orig_cl_ord_id=passive.cl_ord_id,
            symbol=passive.symbol,
            side=passive.side,
            order_qty_lots=passive.order_qty_lots,
            requested_at=NOW,
            session_id="SESSION:maker",
            participant_id="maker",
        )
    )
    assert (
        passive.filled_qty_lots
        + passive.canceled_qty_lots
        + passive.leaves_qty_lots
    ) == passive.order_qty_lots
    assert reports[-1].cum_qty_lots == 7
    assert not engine.books["TEST"].is_resting(passive_id)


def test_fix_rest_websocket_and_influx_values_are_canonical_strings():
    instruments = catalog(
        tick_size="0.1",
        lot_size="0.1",
        price_precision=1,
        quantity_precision=1,
    )
    engine, reports, trades, snapshots = engine_stack(instruments)
    passive_id = submit(
        engine,
        cl_ord_id="ASK",
        participant_id="maker",
        side=Side.SELL,
        order_qty_lots=3,
        price_ticks=3,
    )
    resting_payload = _book_payload(snapshots[-1])
    assert resting_payload["asks"][0]["price"] == "0.3"
    assert resting_payload["asks"][0]["qty"] == "0.3"
    assert "price_ticks" not in resting_payload["asks"][0]

    submit(
        engine,
        cl_ord_id="BUY",
        participant_id="taker",
        side=Side.BUY,
        order_qty_lots=3,
        price_ticks=3,
    )
    passive_fill = next(
        report
        for report in reports
        if report.order_id == passive_id and report.exec_type == ExecutionType.TRADE
    )
    fields = execution_report_fields(passive_fill)
    assert fields["Price"] == "0.3"
    assert fields["LastPx"] == "0.3"
    assert fields["OrderQty"] == "0.3"
    assert fields["AvgPx"] == "0.3"
    assert "0.30000000000000004" not in str(fields)

    metrics = MetricsCollector(
        "Exact",
        instruments=instruments,
        prometheus_enabled=False,
    )
    points = []
    metrics._create_point = lambda measurement, tags, values, timestamp: (
        points.append((measurement, tags, values, timestamp))
    )
    metrics.bind_engine(engine)
    assert metrics.record_trade(trades[0])
    trade_fields = next(
        values
        for measurement, _, values, _ in points
        if measurement == "trades"
    )
    assert trade_fields["price_ticks"] == 3
    assert trade_fields["qty_lots"] == 3
    assert trade_fields["tick_size"] == "0.1"
    assert trade_fields["lot_size"] == "0.1"
    assert trade_fields["price"] == "0.3"
    assert trade_fields["quantity"] == "0.3"
    assert "0.30000000000000004" not in str(trade_fields)

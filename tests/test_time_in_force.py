"""IOC and GTC post-match lifecycle regression tests."""

import pytest

from src.mockx.models import ExecutionType, OrderStatus, Side, TimeInForce
from tests.lifecycle_helpers import make_engine, submit


def reports_for(reports, order_id):
    return [report for report in reports if report.order_id == order_id]


@pytest.mark.parametrize("side", (Side.BUY, Side.SELL))
def test_unmatched_ioc_limit_cancels_and_never_rests(side):
    engine, reports, trades, _ = make_engine()

    order_id = submit(
        engine,
        cl_ord_id=f"IOC-EMPTY-{side.value}",
        participant_id=f"IOC-EMPTY-{side.value}",
        side=side,
        qty=10,
        price=100,
        tif=TimeInForce.IOC,
    )

    order = engine.orders[order_id]
    book = engine.books["TEST"]
    assert order.status == OrderStatus.CANCELLED
    assert order.filled_qty_lots == 0
    assert order.leaves_qty_lots == 0
    assert order.canceled_qty_lots == 10
    assert not book.is_resting(order_id)
    assert not book.bids
    assert not book.asks
    assert trades == []
    assert [
        (report.exec_type, report.ord_status)
        for report in reports_for(reports, order_id)
    ] == [(ExecutionType.CANCELED, OrderStatus.CANCELLED)]
    book.assert_invariants()


@pytest.mark.parametrize(
    ("passive_side", "aggressor_side"),
    (
        (Side.SELL, Side.BUY),
        (Side.BUY, Side.SELL),
    ),
)
def test_partially_filled_ioc_reports_trade_then_cancels_remainder(
    passive_side,
    aggressor_side,
):
    engine, reports, trades, _ = make_engine()
    passive_id = submit(
        engine,
        cl_ord_id="IOC-PASSIVE",
        participant_id="IOC-PASSIVE",
        side=passive_side,
        qty=5,
        price=100,
    )
    aggressor_id = submit(
        engine,
        cl_ord_id="IOC-AGGRESSOR",
        participant_id="IOC-AGGRESSOR",
        side=aggressor_side,
        qty=10,
        price=100,
        tif=TimeInForce.IOC,
    )

    aggressor = engine.orders[aggressor_id]
    passive = engine.orders[passive_id]
    aggressor_reports = reports_for(reports, aggressor_id)

    assert len(trades) == 1
    assert aggressor.status == OrderStatus.CANCELLED
    assert aggressor.filled_qty_lots == 5
    assert aggressor.canceled_qty_lots == 5
    assert aggressor.leaves_qty_lots == 0
    assert aggressor.average_fill_price == 100
    assert not engine.books["TEST"].is_resting(aggressor_id)
    assert [
        (report.exec_type, report.ord_status)
        for report in aggressor_reports
    ] == [
        (ExecutionType.TRADE, OrderStatus.PARTIAL),
        (ExecutionType.CANCELED, OrderStatus.CANCELLED),
    ]
    assert aggressor_reports[0].last_qty_lots == 5
    assert aggressor_reports[0].last_price_ticks == 100
    assert aggressor_reports[0].cum_qty_lots == 5
    assert aggressor_reports[0].leaves_qty_lots == 5
    assert aggressor_reports[1].cum_qty_lots == 5
    assert aggressor_reports[1].leaves_qty_lots == 0
    assert aggressor_reports[1].canceled_qty_lots == 5
    assert passive.status == OrderStatus.FILLED
    passive_trades = [
        report
        for report in reports_for(reports, passive_id)
        if report.exec_type == ExecutionType.TRADE
    ]
    assert len(passive_trades) == 1
    engine.books["TEST"].assert_invariants()


def test_fully_filled_ioc_has_no_cancel_report_and_never_rests():
    engine, reports, _, _ = make_engine()
    submit(
        engine,
        cl_ord_id="IOC-FULL-PASSIVE",
        participant_id="IOC-FULL-PASSIVE",
        side=Side.SELL,
        qty=10,
        price=100,
    )
    order_id = submit(
        engine,
        cl_ord_id="IOC-FULL",
        participant_id="IOC-FULL",
        side=Side.BUY,
        qty=10,
        price=100,
        tif=TimeInForce.IOC,
    )

    order = engine.orders[order_id]
    own_reports = reports_for(reports, order_id)
    assert order.status == OrderStatus.FILLED
    assert order.canceled_qty_lots == 0
    assert not engine.books["TEST"].is_resting(order_id)
    assert [report.exec_type for report in own_reports] == [
        ExecutionType.TRADE
    ]


def test_partial_gtc_limit_rests_without_new_regression():
    engine, reports, _, _ = make_engine()
    submit(
        engine,
        cl_ord_id="GTC-PASSIVE",
        participant_id="GTC-PASSIVE",
        side=Side.SELL,
        qty=4,
        price=100,
    )
    order_id = submit(
        engine,
        cl_ord_id="GTC-PARTIAL",
        participant_id="GTC-PARTIAL",
        side=Side.BUY,
        qty=10,
        price=100,
    )

    order = engine.orders[order_id]
    own_reports = reports_for(reports, order_id)
    assert order.status == OrderStatus.PARTIAL
    assert order.filled_qty_lots == 4
    assert order.leaves_qty_lots == 6
    assert order.canceled_qty_lots == 0
    assert engine.books["TEST"].is_resting(order_id)
    assert [
        (report.exec_type, report.ord_status)
        for report in own_reports
    ] == [(ExecutionType.TRADE, OrderStatus.PARTIAL)]
    assert not any(
        report.exec_type == ExecutionType.NEW
        for report in own_reports
    )
    engine.books["TEST"].assert_invariants()


def test_unfilled_gtc_limit_rests_with_one_new_report():
    engine, reports, _, _ = make_engine()
    order_id = submit(
        engine,
        cl_ord_id="GTC-NEW",
        participant_id="GTC-NEW",
        side=Side.BUY,
        qty=10,
        price=99,
    )

    order = engine.orders[order_id]
    own_reports = reports_for(reports, order_id)
    assert order.status == OrderStatus.NEW
    assert order.filled_qty_lots == 0
    assert order.leaves_qty_lots == 10
    assert order.canceled_qty_lots == 0
    assert engine.books["TEST"].is_resting(order_id)
    assert len(own_reports) == 1
    assert own_reports[0].exec_type == ExecutionType.NEW
    assert own_reports[0].ord_status == OrderStatus.NEW
    assert own_reports[0].cum_qty_lots == 0
    assert own_reports[0].leaves_qty_lots == 10

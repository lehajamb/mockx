"""Market-order immediate execution and remainder tests."""

import pytest

from src.mockx.models import ExecutionType, OrderStatus, Side, TimeInForce
from tests.lifecycle_helpers import make_engine, submit


def trade_reports(reports, order_id):
    return [
        report
        for report in reports
        if (
            report.order_id == order_id
            and report.exec_type == ExecutionType.TRADE
        )
    ]


@pytest.mark.parametrize("side", (Side.BUY, Side.SELL))
def test_empty_book_market_order_cancels_without_fake_price_level(side):
    engine, reports, trades, _ = make_engine()

    order_id = submit(
        engine,
        cl_ord_id=f"MARKET-EMPTY-{side.value}",
        participant_id=f"MARKET-EMPTY-{side.value}",
        side=side,
        qty=8,
        price=None,
        tif=TimeInForce.GTC,
    )

    order = engine.orders[order_id]
    own_reports = [
        report for report in reports if report.order_id == order_id
    ]
    assert order.status == OrderStatus.CANCELLED
    assert order.price_ticks is None
    assert order.filled_qty_lots == 0
    assert order.canceled_qty_lots == 8
    assert order.leaves_qty_lots == 0
    assert trades == []
    assert not engine.books["TEST"].is_resting(order_id)
    assert not engine.books["TEST"].bids
    assert not engine.books["TEST"].asks
    assert len(own_reports) == 1
    assert own_reports[0].exec_type == ExecutionType.CANCELED
    assert own_reports[0].leaves_qty_lots == 0


def test_partially_filled_market_order_cancels_exhausted_remainder():
    engine, reports, trades, _ = make_engine()
    passive_id = submit(
        engine,
        cl_ord_id="MARKET-PARTIAL-PASSIVE",
        participant_id="MARKET-PARTIAL-PASSIVE",
        side=Side.SELL,
        qty=4,
        price=100,
    )
    market_id = submit(
        engine,
        cl_ord_id="MARKET-PARTIAL",
        participant_id="MARKET-PARTIAL",
        side=Side.BUY,
        qty=10,
        price=None,
        tif=TimeInForce.IOC,
    )

    market = engine.orders[market_id]
    own_reports = [
        report for report in reports if report.order_id == market_id
    ]
    assert len(trades) == 1
    assert engine.orders[passive_id].status == OrderStatus.FILLED
    assert market.status == OrderStatus.CANCELLED
    assert market.filled_qty_lots == 4
    assert market.canceled_qty_lots == 6
    assert market.leaves_qty_lots == 0
    assert market.average_fill_price == 100
    assert not engine.books["TEST"].is_resting(market_id)
    assert [
        (report.exec_type, report.ord_status)
        for report in own_reports
    ] == [
        (ExecutionType.TRADE, OrderStatus.PARTIAL),
        (ExecutionType.CANCELED, OrderStatus.CANCELLED),
    ]
    assert own_reports[-1].cum_qty_lots == 4
    assert own_reports[-1].leaves_qty_lots == 0
    assert own_reports[-1].canceled_qty_lots == 6


def test_fully_filled_market_order_reports_every_fill_and_average():
    engine, reports, trades, _ = make_engine()
    passive_ids = [
        submit(
            engine,
            cl_ord_id="MARKET-A",
            participant_id="MARKET-A",
            side=Side.SELL,
            qty=3,
            price=100,
        ),
        submit(
            engine,
            cl_ord_id="MARKET-B",
            participant_id="MARKET-B",
            side=Side.SELL,
            qty=4,
            price=100,
        ),
        submit(
            engine,
            cl_ord_id="MARKET-C",
            participant_id="MARKET-C",
            side=Side.SELL,
            qty=5,
            price=101,
        ),
    ]
    market_id = submit(
        engine,
        cl_ord_id="MARKET-FULL",
        participant_id="MARKET-FULL",
        side=Side.BUY,
        qty=10,
        price=None,
        tif=TimeInForce.GTC,
    )

    market = engine.orders[market_id]
    own_trades = trade_reports(reports, market_id)
    passive_trade_reports = [
        report
        for report in reports
        if (
            report.order_id in passive_ids
            and report.exec_type == ExecutionType.TRADE
        )
    ]
    assert market.status == OrderStatus.FILLED
    assert market.leaves_qty_lots == 0
    assert market.canceled_qty_lots == 0
    assert not engine.books["TEST"].is_resting(market_id)
    assert len(trades) == 3
    assert len(own_trades) == 3
    assert len(passive_trade_reports) == 3
    assert [report.last_qty_lots for report in own_trades] == [3, 4, 3]
    assert [report.last_price_ticks for report in own_trades] == [100, 100, 101]
    assert [report.cum_qty_lots for report in own_trades] == [3, 7, 10]
    assert [report.ord_status for report in own_trades] == [
        OrderStatus.PARTIAL,
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
    ]
    assert [
        float(report.average_fill_price) for report in own_trades
    ] == pytest.approx([100, 100, 100.3])
    assert engine.orders[passive_ids[0]].status == OrderStatus.FILLED
    assert engine.orders[passive_ids[1]].status == OrderStatus.FILLED
    assert engine.orders[passive_ids[2]].status == OrderStatus.PARTIAL
    assert engine.orders[passive_ids[2]].leaves_qty_lots == 2
    assert [trade.qty_lots for trade in trades] == [3, 4, 3]
    assert [trade.price_ticks for trade in trades] == [100, 100, 101]
    engine.books["TEST"].assert_invariants()

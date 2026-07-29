"""PR 8C acceptance tests for unconditional cancel-both prevention."""

from datetime import datetime, timezone

import pytest

from src.mockx.metrics import MetricsCollector
from src.mockx.models import (
    ExecutionParams,
    ExecutionType,
    NewOrderCommand,
    OrderStatus,
    OrderType,
    ProcessOrderCommand,
    SelfTradePrevention,
    Side,
    TimeInForce,
    Trade,
)

from .test_pr8_exact_arithmetic import catalog, engine_stack


NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)
STP_TEXT_2 = (
    "SELF_TRADE_PREVENTION: canceled passive and aggressor; "
    "prevented_qty=0.002"
)


def submit_owned(
    engine,
    *,
    cl_ord_id: str,
    participant_id: str,
    side: Side,
    order_qty_lots: int,
    price_ticks: int | None,
    tif: TimeInForce = TimeInForce.GTC,
    session_id: str | None = None,
    session_alias: str | None = None,
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
            session_id=session_id or f"SESSION:{cl_ord_id}",
            session_alias=session_alias,
        )
    )
    engine.process_order(ProcessOrderCommand(order_id, 0))
    return order_id


def test_same_participant_cancels_complete_remainders_before_trade_id():
    engine, reports, trades, snapshots = engine_stack()
    events = []
    engine.on_self_trade_prevention = events.append

    passive_id = submit_owned(
        engine,
        cl_ord_id="PASSIVE",
        participant_id="owner",
        side=Side.SELL,
        order_qty_lots=5,
        price_ticks=10_000,
    )
    aggressor_id = submit_owned(
        engine,
        cl_ord_id="AGGRESSOR",
        participant_id="owner",
        side=Side.BUY,
        order_qty_lots=2,
        price_ticks=10_000,
    )

    passive = engine.orders[passive_id]
    aggressor = engine.orders[aggressor_id]
    assert passive.status == aggressor.status == OrderStatus.CANCELLED
    assert passive.canceled_qty_lots == 5
    assert aggressor.canceled_qty_lots == 2
    assert passive.leaves_qty_lots == aggressor.leaves_qty_lots == 0
    assert trades == []
    assert engine.next_trade_id == 1
    assert not engine.books["TEST"].asks
    assert not engine.books["TEST"].bids

    cancel_reports = [
        report
        for report in reports
        if report.exec_type == ExecutionType.CANCELED
    ]
    assert [report.order_id for report in cancel_reports] == [
        passive_id,
        aggressor_id,
    ]
    assert [report.text for report in cancel_reports] == [STP_TEXT_2, STP_TEXT_2]
    assert len(events) == 1
    assert events[0] == SelfTradePrevention(
        symbol="TEST",
        participant_id="owner",
        aggressor_order_id=aggressor_id,
        passive_order_id=passive_id,
        prevented_qty_lots=2,
        timestamp=engine.scheduler.current_time,
    )
    assert len(snapshots) == 2
    assert snapshots[-1].asks == snapshots[-1].bids == ()


def test_different_participants_match_even_with_same_display_alias():
    engine, reports, trades, _ = engine_stack()
    passive_id = submit_owned(
        engine,
        cl_ord_id="PASSIVE",
        participant_id="participant-a",
        side=Side.SELL,
        order_qty_lots=3,
        price_ticks=10_000,
        session_alias="shared-alias",
    )
    aggressor_id = submit_owned(
        engine,
        cl_ord_id="AGGRESSOR",
        participant_id="participant-b",
        side=Side.BUY,
        order_qty_lots=3,
        price_ticks=10_000,
        session_alias="shared-alias",
    )

    assert len(trades) == 1
    assert trades[0].buyer_participant_id == "participant-b"
    assert trades[0].seller_participant_id == "participant-a"
    assert engine.orders[passive_id].status == OrderStatus.FILLED
    assert engine.orders[aggressor_id].status == OrderStatus.FILLED
    assert not [report for report in reports if report.text and "SELF_TRADE" in report.text]


def test_two_fix_sessions_for_one_participant_are_common_ownership():
    engine, _, trades, _ = engine_stack()
    passive_id = submit_owned(
        engine,
        cl_ord_id="SESSION-A-ORDER",
        participant_id="one-account",
        side=Side.SELL,
        order_qty_lots=2,
        price_ticks=10_000,
        session_id="FIX.4.4:MOCKX->CLIENT-A",
    )
    aggressor_id = submit_owned(
        engine,
        cl_ord_id="SESSION-B-ORDER",
        participant_id="one-account",
        side=Side.BUY,
        order_qty_lots=2,
        price_ticks=10_000,
        session_id="FIX.4.4:MOCKX->CLIENT-B",
    )

    assert trades == []
    assert engine.orders[passive_id].status == OrderStatus.CANCELLED
    assert engine.orders[aggressor_id].status == OrderStatus.CANCELLED


def test_prior_third_party_fill_is_preserved_when_self_cross_cancels_both():
    engine, reports, trades, _ = engine_stack()
    passive_id = submit_owned(
        engine,
        cl_ord_id="OWNER-ASK",
        participant_id="owner",
        side=Side.SELL,
        order_qty_lots=10,
        price_ticks=10_000,
    )
    submit_owned(
        engine,
        cl_ord_id="THIRD-PARTY",
        participant_id="other",
        side=Side.BUY,
        order_qty_lots=4,
        price_ticks=10_000,
    )
    assert len(trades) == 1
    next_trade_id = engine.next_trade_id

    aggressor_id = submit_owned(
        engine,
        cl_ord_id="OWNER-BUY",
        participant_id="owner",
        side=Side.BUY,
        order_qty_lots=10,
        price_ticks=10_000,
    )

    passive = engine.orders[passive_id]
    aggressor = engine.orders[aggressor_id]
    assert len(trades) == 1
    assert engine.next_trade_id == next_trade_id
    assert passive.filled_qty_lots == 4
    assert passive.canceled_qty_lots == 6
    assert passive.filled_notional_tick_lots == 40_000
    assert aggressor.filled_qty_lots == 0
    assert aggressor.canceled_qty_lots == 10
    cancel_reports = [
        report
        for report in reports
        if report.exec_type == ExecutionType.CANCELED
        and report.order_id in {passive_id, aggressor_id}
    ]
    assert [report.order_id for report in cancel_reports] == [
        passive_id,
        aggressor_id,
    ]
    assert cancel_reports[0].cum_qty_lots == 4
    assert cancel_reports[0].leaves_qty_lots == 0
    assert cancel_reports[0].instrument.format_average_price(
        cancel_reports[0].filled_notional_tick_lots,
        cancel_reports[0].cum_qty_lots,
    ) == "100.00"
    assert cancel_reports[0].text.endswith("prevented_qty=0.006")


def test_self_liquidity_at_best_stops_before_worse_third_party_liquidity():
    engine, _, trades, _ = engine_stack()
    self_id = submit_owned(
        engine,
        cl_ord_id="SELF-BEST",
        participant_id="owner",
        side=Side.SELL,
        order_qty_lots=2,
        price_ticks=10_000,
    )
    third_id = submit_owned(
        engine,
        cl_ord_id="THIRD-WORSE",
        participant_id="other",
        side=Side.SELL,
        order_qty_lots=5,
        price_ticks=10_100,
    )
    aggressor_id = submit_owned(
        engine,
        cl_ord_id="OWNER-BUY",
        participant_id="owner",
        side=Side.BUY,
        order_qty_lots=5,
        price_ticks=10_200,
    )

    assert trades == []
    assert engine.orders[self_id].status == OrderStatus.CANCELLED
    assert engine.orders[aggressor_id].status == OrderStatus.CANCELLED
    assert engine.orders[third_id].status == OrderStatus.NEW
    assert engine.orders[third_id].leaves_qty_lots == 5
    assert engine.books["TEST"].get_best_ask_ticks() == 10_100


@pytest.mark.parametrize(
    ("price_ticks", "tif"),
    [
        (10_000, TimeInForce.IOC),
        (None, TimeInForce.IOC),
    ],
)
def test_ioc_and_market_aggressors_cannot_self_trade(price_ticks, tif):
    engine, reports, trades, _ = engine_stack()
    passive_id = submit_owned(
        engine,
        cl_ord_id="PASSIVE",
        participant_id="owner",
        side=Side.SELL,
        order_qty_lots=2,
        price_ticks=10_000,
    )
    aggressor_id = submit_owned(
        engine,
        cl_ord_id="AGGRESSOR",
        participant_id="owner",
        side=Side.BUY,
        order_qty_lots=5,
        price_ticks=price_ticks,
        tif=tif,
    )

    assert trades == []
    assert engine.orders[passive_id].status == OrderStatus.CANCELLED
    assert engine.orders[aggressor_id].status == OrderStatus.CANCELLED
    assert [
        report.order_id
        for report in reports
        if report.exec_type == ExecutionType.CANCELED
    ] == [passive_id, aggressor_id]


def test_prevention_metrics_and_persistence_are_exact_and_bounded_label():
    instruments = catalog()
    engine, reports, _, _ = engine_stack(instruments)
    metrics = MetricsCollector(
        "Exact",
        instruments=instruments,
        prometheus_enabled=True,
    )
    points = []
    metrics._create_point = lambda measurement, tags, values, timestamp: (
        points.append((measurement, tags, values, timestamp))
    )
    metrics.bind_engine(engine)
    engine.on_execution_report = lambda report: (
        reports.append(report),
        metrics.record_order(report),
    )
    engine.on_trade = metrics.record_trade
    engine.on_self_trade_prevention = metrics.record_self_trade_prevention

    passive_id = submit_owned(
        engine,
        cl_ord_id="PASSIVE",
        participant_id="private-owner-id",
        side=Side.SELL,
        order_qty_lots=5,
        price_ticks=10_000,
    )
    aggressor_id = submit_owned(
        engine,
        cl_ord_id="AGGRESSOR",
        participant_id="private-owner-id",
        side=Side.BUY,
        order_qty_lots=2,
        price_ticks=10_000,
    )

    snapshot = metrics.snapshot()
    assert snapshot.trades == 0
    assert snapshot.traded_quantity == "0.000"
    payload = metrics.prometheus_payload().decode()
    assert 'mockx_self_trade_preventions_total{symbol="TEST"} 1.0' in payload
    assert (
        'mockx_self_trade_prevented_quantity_total{symbol="TEST"} 0.002'
        in payload
    )
    assert "private-owner-id" not in payload
    assert "policy=" not in payload

    values = next(
        values
        for measurement, _, values, _ in points
        if measurement == "self_trade_prevention"
    )
    assert values == {
        "participant_id": "private-owner-id",
        "aggressor_order_id": aggressor_id,
        "passive_order_id": passive_id,
        "prevented_qty_lots": 2,
        "prevented_quantity": "0.002",
        "lot_size": "0.001",
    }


def test_trade_model_defensively_rejects_common_participant():
    with pytest.raises(ValueError, match="Self-trades are forbidden"):
        Trade(
            id=1,
            symbol="TEST",
            price_ticks=10_000,
            qty_lots=1,
            notional_tick_lots=10_000,
            buyer_order_id=1,
            seller_order_id=2,
            timestamp=NOW,
            level=0,
            buyer_session_alias="A",
            seller_session_alias="B",
            buyer_participant_id="same",
            seller_participant_id="same",
            aggressor_order_id=1,
            passive_order_id=2,
            aggressor_side=Side.BUY,
        )


def run_deterministic_prevention() -> tuple:
    engine, reports, trades, _ = engine_stack()
    events = []
    engine.on_self_trade_prevention = events.append
    passive_id = submit_owned(
        engine,
        cl_ord_id="PASSIVE",
        participant_id="owner",
        side=Side.SELL,
        order_qty_lots=5,
        price_ticks=10_000,
    )
    aggressor_id = submit_owned(
        engine,
        cl_ord_id="AGGRESSOR",
        participant_id="owner",
        side=Side.BUY,
        order_qty_lots=2,
        price_ticks=10_000,
    )
    return (
        tuple(
            (
                report.order_id,
                report.exec_type.value,
                report.ord_status.value,
                report.cum_qty_lots,
                report.canceled_qty_lots,
                report.text,
            )
            for report in reports
        ),
        tuple(trades),
        tuple(events),
        engine.orders[passive_id].status,
        engine.orders[aggressor_id].status,
        engine.next_trade_id,
    )


def test_prevention_is_deterministic_across_repeated_runs():
    assert run_deterministic_prevention() == run_deterministic_prevention()

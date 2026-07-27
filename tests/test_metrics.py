"""Event ownership and fill-accounting regression tests."""

from src.mockx.engine import MatchingEngine
from src.mockx.latency import LatencyScheduler
from src.mockx.metrics import LatencyTracker, MetricsCollector
from src.mockx.models import (
    CancelOrderCommand,
    ExecutionParams,
    NewOrderCommand,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)


def make_instrumented_engine():
    scheduler = LatencyScheduler()
    engine = MatchingEngine(
        ExecutionParams(
            network_latency_ms=0,
            engine_latency_ms=0,
            reject_prob=0.0,
            rand_seed=42,
        ),
        scheduler,
    )
    engine.add_symbol("TEST")
    metrics = MetricsCollector("TEST")
    metrics.bind_engine(engine)
    tracker = LatencyTracker(scheduler, metrics)
    reports = []
    trades = []

    def submitted(order_id, command):
        metrics.record_order_submitted(order_id, command)
        tracker.order_submitted(
            order_id,
            command.session_id,
            command.submitted_at,
        )

    def reported(report):
        reports.append(report)
        metrics.record_order(report)
        tracker.report_created(report)

    def traded(trade):
        trades.append(trade)
        metrics.record_trade(trade)

    engine.on_order_submitted = submitted
    engine.on_execution_report = reported
    engine.on_trade = traded
    engine.on_cancel_request = metrics.record_cancel_request
    engine.on_cancel_reject = metrics.record_cancel_reject
    engine.on_matching_duration = tracker.record_real_matching_duration
    return engine, metrics, reports, trades


def submit(
    engine,
    cl_ord_id,
    side,
    qty,
    price,
    *,
    tif=TimeInForce.GTC,
):
    order_id = engine.submit_order(
        NewOrderCommand(
            cl_ord_id=cl_ord_id,
            symbol="TEST",
            side=side,
            qty=qty,
            price=price,
            tif=tif,
            submitted_at=engine.scheduler.current_time,
            session_id="FIX.4.4:MOCKX->CLIENT",
            session_alias="CLIENT",
        )
    )
    engine.process_order(ProcessOrderCommand(order_id, 0))
    return order_id


def test_one_fill_counts_one_trade_and_two_trade_reports():
    engine, metrics, reports, trades = make_instrumented_engine()
    submit(engine, "PASSIVE", Side.SELL, 5.0, 100.0)
    before = metrics.snapshot()

    submit(engine, "AGGRESSOR", Side.BUY, 5.0, 100.0)
    after = metrics.snapshot()

    assert len(trades) == 1
    assert after.trades - before.trades == 1
    assert after.execution_reports - before.execution_reports == 2
    assert after.traded_quantity - before.traded_quantity == 5.0
    assert after.traded_notional - before.traded_notional == 500.0
    assert len([report for report in reports if report.trade_id == 1]) == 2


def test_multi_fill_counts_volume_once_per_fill():
    engine, metrics, reports, trades = make_instrumented_engine()
    submit(engine, "P1", Side.SELL, 1.0, 100.0)
    submit(engine, "P2", Side.SELL, 2.0, 101.0)
    submit(engine, "P3", Side.SELL, 3.0, 102.0)
    before = metrics.snapshot()

    submit(engine, "TAKER", Side.BUY, 6.0, 102.0)
    after = metrics.snapshot()

    assert len(trades) == 3
    assert after.trades - before.trades == 3
    assert after.execution_reports - before.execution_reports == 6
    assert after.traded_quantity - before.traded_quantity == 6.0
    assert after.traded_notional - before.traded_notional == 608.0
    assert len([report for report in reports if report.trade_id]) == 6


def test_ioc_canceled_remainder_does_not_inflate_trade_volume():
    engine, metrics, _, _ = make_instrumented_engine()
    submit(engine, "PASSIVE", Side.SELL, 4.0, 100.0)
    before = metrics.snapshot()

    submit(
        engine,
        "IOC",
        Side.BUY,
        10.0,
        100.0,
        tif=TimeInForce.IOC,
    )
    after = metrics.snapshot()

    assert after.trades - before.trades == 1
    assert after.traded_quantity - before.traded_quantity == 4.0
    assert after.traded_notional - before.traded_notional == 400.0
    assert after.orders_canceled - before.orders_canceled == 1
    assert after.canceled_quantity - before.canceled_quantity == 6.0


def test_cancel_reject_counts_request_and_reject_not_success():
    engine, metrics, _, _ = make_instrumented_engine()
    before = metrics.snapshot()

    rejection = engine.cancel_order(
        CancelOrderCommand(
            order_id=0,
            expected_version=0,
            cancel_cl_ord_id="CXL-UNKNOWN",
            orig_cl_ord_id="UNKNOWN",
            requested_at=engine.scheduler.current_time,
            session_id="FIX.4.4:MOCKX->CLIENT",
            session_alias="CLIENT",
        )
    )
    after = metrics.snapshot()

    assert rejection is not None
    assert after.cancel_requests - before.cancel_requests == 1
    assert after.cancel_rejects - before.cancel_rejects == 1
    assert after.orders_canceled == before.orders_canceled
    assert after.trades == before.trades
    assert after.traded_quantity == before.traded_quantity


def test_active_and_resting_gauges_follow_authoritative_engine_state():
    engine, metrics, _, _ = make_instrumented_engine()
    resting_id = submit(engine, "REST", Side.BUY, 10.0, 100.0)
    first = metrics.snapshot()
    assert (first.active_orders, first.resting_orders) == (1, 1)

    submit(engine, "PARTIAL", Side.SELL, 4.0, 100.0)
    partial = metrics.snapshot()
    assert (partial.active_orders, partial.resting_orders) == (1, 1)

    submit(engine, "FINISH", Side.SELL, 6.0, 100.0)
    filled = metrics.snapshot()
    assert (filled.active_orders, filled.resting_orders) == (0, 0)

    cancel_id = submit(engine, "CANCEL", Side.BUY, 3.0, 99.0)
    live = metrics.snapshot()
    assert (live.active_orders, live.resting_orders) == (1, 1)
    order = engine.orders[cancel_id]
    assert engine.cancel_order(
        CancelOrderCommand(
            order_id=cancel_id,
            expected_version=order.version,
            cancel_cl_ord_id="CXL-CANCEL",
            orig_cl_ord_id=order.cl_ord_id,
            requested_at=engine.scheduler.current_time,
            session_id=order.session_id,
            session_alias=order.session_alias,
        )
    ) is None
    canceled = metrics.snapshot()
    assert (canceled.active_orders, canceled.resting_orders) == (0, 0)
    assert not engine.books["TEST"].is_resting(resting_id)


def test_duplicate_trade_report_and_stale_command_are_idempotent():
    engine, metrics, reports, trades = make_instrumented_engine()
    submit(engine, "PASSIVE", Side.SELL, 5.0, 100.0)
    aggressor_id = submit(engine, "AGGRESSOR", Side.BUY, 5.0, 100.0)
    terminal_report = next(
        report
        for report in reports
        if report.order_id == aggressor_id and report.leaves_qty == 0
    )
    trade = trades[0]
    before = metrics.snapshot()

    assert not metrics.record_order(terminal_report)
    assert not metrics.record_trade(trade)
    engine.process_order(ProcessOrderCommand(aggressor_id, 0))
    after = metrics.snapshot()

    assert after.execution_reports == before.execution_reports
    assert after.orders_filled == before.orders_filled
    assert after.trades == before.trades
    assert after.traded_quantity == before.traded_quantity
    assert after.traded_notional == before.traded_notional

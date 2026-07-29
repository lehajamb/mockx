from tests.exact_helpers import exact_engine, exact_metrics
from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""Regression tests for immutable execution-report snapshots."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.mockx.engine import MatchingEngine
from src.mockx.fix_fields import execution_report_fields
from src.mockx.latency import LatencyScheduler
from src.mockx.metrics import MetricsCollector
from src.mockx.models import (
    ExecutionParams,
    ExecutionType,
    NewOrderCommand,
    Order,
    OrderStatus,
    ProcessOrderCommand,
    Side,
    TimeInForce,
    create_execution_report,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_order(
    *,
    order_id: int = 1,
    cl_ord_id: str = "SNAPSHOT",
    side: Side = Side.BUY,
    qty: int = 10,
    price: int = 100,
) -> Order:
    order = exact_order(
        id=order_id,
        cl_ord_id=cl_ord_id,
        participant_id=cl_ord_id,
        symbol="TEST",
        side=side,
        order_qty_lots=qty,
        price_ticks=price,
        tif=TimeInForce.GTC,
        ts_submitted=NOW,
        session_id="FIX.4.4:CLIENT->MOCKX",
        session_alias="CLIENT",
    )
    order.transition_to(OrderStatus.NEW)
    return order


def new_report(order: Order):
    return create_execution_report(
        order,
        exec_type="NEW",
        timestamp=NOW,
        exec_id="G00000000-E00000001",
    )


def fill_order(order: Order) -> None:
    order.filled_qty_lots = order.order_qty_lots
    order.filled_notional_tick_lots = (
        order.order_qty_lots * order.price_ticks
    )
    order.transition_to(OrderStatus.FILLED)


def test_report_snapshot_does_not_follow_live_order_mutation():
    order = make_order()
    report = new_report(order)

    fill_order(order)

    assert report.exec_type == "NEW"
    assert report.ord_status == OrderStatus.NEW
    assert report.cum_qty_lots == 0.0
    assert report.leaves_qty_lots == 10.0
    assert report.average_fill_price == 0.0
    assert not hasattr(report, "order")


def test_execution_report_is_frozen():
    order = make_order()
    report = new_report(order)

    with pytest.raises(FrozenInstanceError):
        report.cum_qty_lots = 5.0

    fill_order(order)
    assert report.cum_qty_lots == 0.0
    assert report.ord_status == OrderStatus.NEW


@pytest.mark.asyncio
async def test_delayed_new_report_remains_new_when_delivered_after_fill():
    scheduler = LatencyScheduler()
    delivered = []
    scheduler.register_handler("exec_report", delivered.append)
    engine = exact_engine(
        ExecutionParams(
            network_latency_ms=10,
            engine_latency_ms=5,
            reject_prob=0.0,
            rand_seed=42,
        ),
        scheduler,
    )
    engine.on_execution_report = lambda report: (
        scheduler.schedule_execution_report(
            report,
            engine.exec_params.network_latency_ms,
            outbound_ms=engine.exec_params.simulated_outbound_latency_ms,
        )
    )
    scheduler.register_handler("order_arrived", lambda command: None)
    scheduler.register_handler("order_process", engine.process_order)

    passive_id = engine.submit_order(
        exact_command(
            cl_ord_id="PASSIVE",
            participant_id="PASSIVE",
            symbol="TEST",
            side=Side.SELL,
            order_qty_lots=10,
            price_ticks=100,
            tif=TimeInForce.GTC,
            submitted_at=NOW,
        )
    )
    engine.process_order(ProcessOrderCommand(passive_id, expected_version=0))
    captured_new = next(
        event.data
        for event in scheduler.event_queue
        if event.event_type == "exec_report"
        and event.data.order_id == passive_id
        and event.data.exec_type == "NEW"
    )

    aggressive_id = engine.submit_order(
        exact_command(
            cl_ord_id="AGGRESSIVE",
            participant_id="AGGRESSIVE",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=10,
            price_ticks=100,
            tif=TimeInForce.GTC,
            submitted_at=NOW,
        )
    )
    engine.process_order(ProcessOrderCommand(aggressive_id, expected_version=0))
    assert engine.orders[passive_id].status == OrderStatus.FILLED

    await scheduler.run_until_idle()

    passive_reports = [
        report for report in delivered if report.order_id == passive_id
    ]
    assert passive_reports[0] is captured_new
    assert passive_reports[0].exec_type == "NEW"
    assert passive_reports[0].ord_status == OrderStatus.NEW
    assert passive_reports[0].leaves_qty_lots == 10.0
    assert passive_reports[0].cum_qty_lots == 0.0

    fill_report = next(
        report
        for report in passive_reports
        if report.ord_status == OrderStatus.FILLED
    )
    assert fill_report.leaves_qty_lots == 0.0
    assert fill_report.cum_qty_lots == 10.0


def test_report_factory_rejects_inconsistent_state_and_quantities():
    order = make_order()
    report = new_report(order)
    fill_order(order)
    filled_report = create_execution_report(
        order,
        exec_type=ExecutionType.TRADE,
        timestamp=NOW,
        exec_id="G00000000-E00000002",
        last_price_ticks=order.price_ticks,
        last_qty_lots=order.order_qty_lots,
        trade_id=1,
    )

    invalid_reports = [
        lambda: replace(report, ord_status=OrderStatus.FILLED),
        lambda: replace(
            filled_report,
            leaves_qty_lots=1,
            cum_qty_lots=9,
        ),
        lambda: replace(report, cum_qty_lots=-1, leaves_qty_lots=11),
        lambda: replace(report, leaves_qty_lots=-1),
        lambda: replace(report, leaves_qty_lots=9),
    ]

    for build_invalid_report in invalid_reports:
        with pytest.raises(ValueError):
            build_invalid_report()


def test_fix_field_preparation_uses_only_report_snapshot():
    order = make_order()
    report = new_report(order)
    fill_order(order)

    fields = execution_report_fields(report)

    assert fields["ExecType"] == "0"
    assert fields["OrdStatus"] == "0"
    assert fields["LeavesQty"] == "10.000"
    assert fields["CumQty"] == "0.000"
    assert fields["AvgPx"] == "0.00"
    assert fields["OrderQty"] == "10.000"
    assert fields["ClOrdID"] == "SNAPSHOT"


def test_terminal_cleanup_uses_report_snapshot_status():
    from src.mockx.gateway import FIXApplication

    order = make_order()
    report = new_report(order)
    fill_order(order)
    terminal_report = create_execution_report(
        order,
        exec_type=ExecutionType.TRADE,
        timestamp=NOW,
        exec_id="G00000000-E00000002",
        last_price_ticks=order.price_ticks,
        last_qty_lots=order.order_qty_lots,
        trade_id=1,
    )
    fake_exchange = SimpleNamespace(
        latency_tracker=Mock(),
        engine=SimpleNamespace(
            scheduler=SimpleNamespace(current_time=NOW),
        ),
    )
    fake_exchange.latency_tracker.complete_order.return_value = True

    FIXApplication._complete_delivery(fake_exchange, report)
    fake_exchange.latency_tracker.cleanup_order.assert_not_called()

    FIXApplication._complete_delivery(fake_exchange, terminal_report)
    fake_exchange.latency_tracker.cleanup_order.assert_called_once_with(
        order.id,
        run_generation=terminal_report.run_generation,
    )


def test_metrics_record_report_snapshot_not_live_order():
    order = make_order()
    report = new_report(order)
    fill_order(order)

    collector = exact_metrics("TEST-EXCHANGE")
    captured = {}

    def capture_point(measurement, tags, fields, timestamp_ns):
        captured.update(
            measurement=measurement,
            tags=tags,
            fields=fields,
            timestamp_ns=timestamp_ns,
        )

    collector._create_point = capture_point
    collector.record_order(report)

    assert captured["measurement"] == "orders"
    assert captured["tags"]["status"] == "NEW"
    assert captured["fields"]["order_qty_lots"] == 10
    assert captured["fields"]["filled_qty_lots"] == 0
    assert captured["fields"]["quantity"] == "10.000"
    assert captured["fields"]["filled_quantity"] == "0.000"
    assert captured["fields"]["average_fill_price"] == "0.00"
    assert captured["timestamp_ns"] == int(NOW.timestamp() * 1_000_000_000)

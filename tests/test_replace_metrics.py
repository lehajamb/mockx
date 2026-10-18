"""Replacement event ownership, persistence, and independent latency traces."""

from dataclasses import replace
from datetime import timedelta

import pytest

from src.mockx.latency import LatencyScheduler
from src.mockx.metrics import BufferedMetricsWriter, InMemoryMetricsSink, LatencyTracker
from src.mockx.models import ExecutionType, OrderStatus, Side, TimeInForce
from src.mockx.questdb_writer import QUESTDB_SCHEMAS
from tests.exact_helpers import exact_metrics, exact_order


def replacement_command(scheduler, *, sequence=3, generation=1):
    from src.mockx.models import OrderType, ReplaceOrderCommand

    return ReplaceOrderCommand(
        replace_cl_ord_id="R-1",
        orig_cl_ord_id="N-1",
        symbol="TEST",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        tif=TimeInForce.GTC,
        order_qty_lots=5,
        price_ticks=99,
        requested_at=scheduler.current_time,
        session_id="FIX.4.4:MOCKX->CLIENT",
        participant_id="CLIENT",
        run_generation=generation,
        ingress_sequence=sequence,
    )


def new_report(scheduler):
    from src.mockx.models import create_execution_report

    order = exact_order(
        id=1,
        cl_ord_id="N-1",
        participant_id="CLIENT",
        symbol="TEST",
        side=Side.BUY,
        order_qty_lots=10,
        price_ticks=99,
        tif=TimeInForce.GTC,
        ts_submitted=scheduler.current_time,
        status=OrderStatus.NEW,
        session_id="FIX.4.4:MOCKX->CLIENT",
        run_generation=1,
    )
    return create_execution_report(
        order, exec_type=ExecutionType.NEW,
        timestamp=scheduler.current_time, exec_id="E-1",
    )


def replaced_report(scheduler, command):
    return replace(
        new_report(scheduler), exec_id="E-2", exec_type=ExecutionType.REPLACED,
        cl_ord_id=command.replace_cl_ord_id, orig_cl_ord_id=command.orig_cl_ord_id,
        order_qty_lots=5, leaves_qty_lots=5,
        replace_ingress_sequence=command.ingress_sequence,
    )


def test_replace_outcomes_are_bounded_and_preserved_in_lifetime_stats():
    from src.mockx.models import ReplacementOutcome

    scheduler = LatencyScheduler()
    command = replacement_command(scheduler)
    metrics = exact_metrics("TEST")
    for outcome in ReplacementOutcome:
        metrics.record_replace_request(command)
        metrics.record_replace_outcome(command, outcome)

    snapshot = metrics.snapshot().to_dict()
    assert snapshot["orders"]["replace_requests"] == len(ReplacementOutcome)
    assert snapshot["orders"]["submitted"] == 0
    assert snapshot["replacements"] == {outcome.value: 1 for outcome in ReplacementOutcome}
    assert all(snapshot["cancellations"][key] == 0 for key in snapshot["cancellations"])
    assert b"mockx_replace_accepted_total 1.0" in metrics.prometheus_payload()
    metrics.reset_run_metrics()
    assert metrics.snapshot().replace_requests == 0
    assert metrics.process_lifetime_snapshot()["replacements"] == snapshot["replacements"]
    assert metrics.process_lifetime_snapshot()["orders"]["replace_requests"] == len(ReplacementOutcome)

    with pytest.raises(ValueError):
        metrics.record_replace_outcome(command, "client-controlled-label")
    assert len(metrics.process_lifetime_snapshot()["replacements"]) == len(ReplacementOutcome)


@pytest.mark.asyncio
async def test_replace_stats_are_exposed_by_api_and_prometheus():
    from httpx import ASGITransport, AsyncClient
    from src.mockx.models import ReplacementOutcome
    from tests.test_observability_api import bare_app

    app, metrics = bare_app()
    command = replacement_command(LatencyScheduler())
    metrics.record_replace_request(command)
    metrics.record_replace_outcome(command, ReplacementOutcome.ACCEPTED)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/stats")
        prometheus = await client.get("/metrics")

    assert response.status_code == 200
    data = response.json()
    assert data["orders"]["replace_requests"] == 1
    assert data["orders"]["replace_rejects"] == 0
    assert data["replacements"]["accepted"] == 1
    assert data["process_lifetime"]["replacements"]["accepted"] == 1
    assert data["latency"]["average_sim_replace_end_to_end_latency_ms"] is None
    assert "mockx_replace_requests_total 1.0" in prometheus.text
    assert "mockx_sim_replace_end_to_end_latency_ms" in prometheus.text


@pytest.mark.asyncio
async def test_replacement_reports_persist_revision_identity_with_unique_events():
    scheduler = LatencyScheduler()
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink)
    metrics = exact_metrics("TEST", persistence=writer)
    metrics.start_run("replace-run")
    original = new_report(scheduler)
    amended = replaced_report(scheduler, replacement_command(scheduler))

    assert metrics.record_order(original)
    assert metrics.record_order(amended)
    assert not metrics.record_order(amended)
    await writer.flush()

    rows = sink.writes[0]
    assert len(rows) == 2
    assert [row.symbol_map["exec_type"] for row in rows] == ["NEW", "REPLACED"]
    assert rows[0].column_map["cl_ord_id"] == "N-1"
    assert "orig_cl_ord_id" not in rows[0].column_map
    assert rows[1].column_map["cl_ord_id"] == "R-1"
    assert rows[1].column_map["orig_cl_ord_id"] == "N-1"
    assert rows[0].metric_event_sequence != rows[1].metric_event_sequence
    assert metrics.snapshot().orders_accepted == 1
    assert metrics.snapshot().orders_canceled == metrics.snapshot().trades == 0


def test_order_schema_explicitly_requires_replacement_revision_columns():
    columns = dict(next(schema.columns for schema in QUESTDB_SCHEMAS if schema.name == "orders"))
    assert columns["exec_type"] == "SYMBOL"
    assert columns["cl_ord_id"] == columns["orig_cl_ord_id"] == "VARCHAR"


def test_quantity_clamp_counts_one_filled_order_without_trades_or_cancellations():
    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    original = new_report(scheduler)
    metrics.record_execution_report(original)
    amended = replace(
        replaced_report(scheduler, replacement_command(scheduler)),
        order_qty_lots=2, cum_qty_lots=2, leaves_qty_lots=0,
        filled_notional_tick_lots=198, ord_status=OrderStatus.FILLED,
    )
    metrics.record_execution_report(amended)
    assert not metrics.record_execution_report(amended)
    snapshot = metrics.snapshot()
    assert snapshot.orders_filled == 1
    assert snapshot.orders_accepted == 1
    assert snapshot.orders_canceled == snapshot.trades == 0
    assert snapshot.traded_quantity == snapshot.canceled_quantity == "0"


def test_replacement_ack_alone_never_counts_as_a_newly_accepted_order():
    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    report = replaced_report(scheduler, replacement_command(scheduler))
    assert metrics.record_execution_report(report)
    assert metrics.snapshot().orders_accepted == 0


def test_replace_latency_finishes_once_and_stays_separate_from_order_latency():
    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    tracker = LatencyTracker(scheduler, metrics)
    command = replacement_command(scheduler)
    tracker.replace_received(command)
    tracker.replace_received(command)
    scheduler.current_time += timedelta(milliseconds=5)
    tracker.replace_arrived(command)
    tracker.replace_arrived(command)
    scheduler.current_time += timedelta(milliseconds=3)
    report = replaced_report(scheduler, command)
    tracker.replace_report_created(command, report)
    tracker.replace_report_created(command, report)
    scheduler.current_time += timedelta(milliseconds=7)
    assert tracker.replace_report_delivered(report)
    assert not tracker.replace_report_delivered(report)

    values = {sample.metric: sample.value for sample in tracker.measurements}
    assert values == {
        "mockx_sim_replace_inbound_latency_ms": 5,
        "mockx_sim_replace_engine_latency_ms": 3,
        "mockx_sim_replace_outbound_latency_ms": 7,
        "mockx_sim_replace_end_to_end_latency_ms": 15,
    }
    assert not tracker.replaces
    assert all(sample.cl_ord_id == "R-1" for sample in tracker.measurements)
    snapshot = metrics.snapshot()
    assert snapshot.average_sim_replace_end_to_end_latency_ms == 15
    assert snapshot.average_sim_end_to_end_latency_ms is None
    assert metrics.process_lifetime_snapshot()["latency"]["average_sim_replace_end_to_end_latency_ms"] == 15


def test_replace_latency_pending_delivery_survives_reset_and_correlates_generation():
    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    tracker = LatencyTracker(scheduler, metrics)
    first = replacement_command(scheduler)
    other = replacement_command(scheduler, generation=2)
    tracker.replace_received(first)
    tracker.replace_received(other)
    scheduler.current_time += timedelta(milliseconds=2)
    tracker.replace_arrived(first)
    report = replaced_report(scheduler, first)
    tracker.replace_report_created(first, report)
    tracker.reset(preserve_replace_keys={(1, first.ingress_sequence)})

    assert set(tracker.replaces) == {(1, first.ingress_sequence)}
    assert tracker.replace_report_delivered(report, delivered_at=report.timestamp + timedelta(milliseconds=4))
    assert [sample.value for sample in tracker.measurements] == [4, 6]
    assert not tracker.replace_report_delivered(report)


def test_negative_replacement_latency_is_rejected_without_absolute_value():
    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    tracker = LatencyTracker(scheduler, metrics)
    command = replacement_command(scheduler)
    tracker.replace_received(command)
    scheduler.current_time -= timedelta(milliseconds=2)
    tracker.replace_arrived(command)

    assert not tracker.measurements
    assert metrics.snapshot().invalid_latency_measurements == 1
    assert metrics.snapshot().average_sim_replace_inbound_latency_ms is None


def test_replacement_elapsed_latency_spans_repeated_clock_resets():
    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    tracker = LatencyTracker(scheduler, metrics)
    scheduler.current_time += timedelta(milliseconds=100)
    command = replacement_command(scheduler)
    key = (command.run_generation, command.ingress_sequence)
    tracker.replace_received(command)
    scheduler.current_time += timedelta(milliseconds=5)
    tracker.replace_arrived(command)
    scheduler.current_time += timedelta(milliseconds=3)
    report = replaced_report(scheduler, command)
    tracker.replace_report_created(command, report)
    scheduler.current_time += timedelta(milliseconds=7)

    for elapsed_ms in (3, 4):
        previous_sim_time = scheduler.current_time
        scheduler.reset()
        tracker.reset(
            preserve_replace_keys={key}, previous_sim_time=previous_sim_time
        )
        scheduler.current_time += timedelta(milliseconds=elapsed_ms)

    assert tracker.replace_report_delivered(report)
    assert not tracker.replace_report_delivered(report)
    assert [sample.value for sample in tracker.measurements] == [14, 22]
    assert all(sample.timestamp == scheduler.current_time for sample in tracker.measurements)
    assert metrics.snapshot().invalid_latency_measurements == 0


def test_replace_rejection_counts_once_and_has_independent_latency():
    from src.mockx.models import CancelRejectReason, FixCommandType, OrderCancelReject

    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    tracker = LatencyTracker(scheduler, metrics)
    command = replacement_command(scheduler)
    tracker.replace_received(command)
    tracker.replace_arrived(command)
    report = OrderCancelReject(
        reject_id="R-REJECT", cl_ord_id="R-1", orig_cl_ord_id="N-1",
        order_id=None, ord_status=None, reason=CancelRejectReason.UNKNOWN_ORDER,
        text="Unknown order", session_id=command.session_id, session_alias=None,
        timestamp=scheduler.current_time, run_generation=1,
        ingress_sequence=command.ingress_sequence, request_type=FixCommandType.REPLACE_ORDER,
    )
    assert metrics.record_replace_reject(report)
    assert not metrics.record_replace_reject(report)
    tracker.replace_report_created(command, report)
    assert tracker.replace_report_delivered(report)
    assert metrics.snapshot().replace_rejects == 1
    assert metrics.snapshot().cancel_rejects == 0
    assert len(tracker.measurements) == 4

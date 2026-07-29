from tests.exact_helpers import exact_engine, exact_metrics
from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""Clock separation and terminal latency cleanup tests."""

from datetime import timedelta

import pytest

from src.mockx.engine import MatchingEngine
from src.mockx.gateway import FIXApplication
from src.mockx.latency import LatencyScheduler
from src.mockx.metrics import (
    LatencyTracker,
    MetricsCollector,
    REAL_MATCHING_DURATION,
    SIM_END_TO_END_LATENCY,
    SIM_ENGINE_LATENCY,
    SIM_INBOUND_LATENCY,
    SIM_OUTBOUND_LATENCY,
)
from src.mockx.models import (
    ExecutionParams,
    ExecutionType,
    NewOrderCommand,
    OrderStatus,
    ProcessOrderCommand,
    SessionIdentity,
    Side,
    TimeInForce,
)


SESSION_ID = "FIX.4.4:MOCKX->CLIENT"


class FakeMonotonicClock:
    def __init__(self, *values):
        self.values = iter(values)

    def now_ns(self):
        return next(self.values)


def wire_stack(*, inbound_ms=5, engine_ms=3, outbound_ms=7, clock=None):
    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    tracker = LatencyTracker(scheduler, metrics)
    engine = exact_engine(
        ExecutionParams(
            network_latency_ms=inbound_ms + outbound_ms,
            engine_latency_ms=engine_ms,
            reject_prob=0.0,
            rand_seed=42,
            inbound_network_latency_ms=inbound_ms,
            outbound_network_latency_ms=outbound_ms,
        ),
        scheduler,
        monotonic_clock=clock,
    )
    metrics.bind_engine(engine)
    reports = []

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
        app.enqueue_report(report)

    def process(command):
        tracker.order_processing_start(command.order_id)
        engine.process_order(command)
        tracker.order_processing_end(command.order_id)

    engine.on_order_submitted = submitted
    engine.on_execution_report = reported
    engine.on_trade = metrics.record_trade
    engine.on_matching_duration = tracker.record_real_matching_duration
    scheduler.register_handler("order_arrived", lambda command: tracker.order_arrived(command.order_id))
    scheduler.register_handler("order_process", process)

    delivered = []
    app = FIXApplication(
        engine,
        metrics,
        tracker,
        transport_sender=lambda transport, report: transport.append(report),
    )
    app.register_session(SessionIdentity(SESSION_ID, alias="CLIENT"), delivered)

    scheduler.register_handler("fix_delivery_due", app.process_delivery_due)
    return scheduler, engine, metrics, tracker, reports, delivered


@pytest.mark.asyncio
async def test_simulated_and_real_latency_clocks_are_separate():
    clock = FakeMonotonicClock(1_000, 201_000)
    scheduler, engine, metrics, tracker, _, _ = wire_stack(clock=clock)
    engine.submit_order(
        exact_command(
            cl_ord_id="LATENCY",
            participant_id="LATENCY",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=1,
            price_ticks=99,
            tif=TimeInForce.GTC,
            submitted_at=scheduler.current_time,
            session_id=SESSION_ID,
            session_alias="CLIENT",
        )
    )

    await scheduler.run_until_idle()

    values = {
        sample.metric: sample.value
        for sample in tracker.measurements
    }
    assert values[SIM_INBOUND_LATENCY] == pytest.approx(0.005)
    assert values[SIM_ENGINE_LATENCY] == pytest.approx(0.003)
    assert values[SIM_OUTBOUND_LATENCY] == pytest.approx(0.007)
    assert values[SIM_END_TO_END_LATENCY] == pytest.approx(0.015)
    assert values[REAL_MATCHING_DURATION] == pytest.approx(0.0002)
    snapshot = metrics.snapshot()
    assert snapshot.average_sim_inbound_latency_ms == pytest.approx(5.0)
    assert snapshot.average_sim_outbound_latency_ms == pytest.approx(7.0)
    assert snapshot.average_real_matching_duration_us == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_simulated_end_to_end_is_delivery_minus_submission():
    scheduler, engine, _, tracker, _, _ = wire_stack(
        inbound_ms=4,
        engine_ms=6,
        outbound_ms=8,
        clock=FakeMonotonicClock(10, 20),
    )
    submitted_at = scheduler.current_time
    engine.submit_order(
        exact_command(
            cl_ord_id="E2E",
            participant_id="E2E",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=1,
            price_ticks=99,
            tif=TimeInForce.GTC,
            submitted_at=submitted_at,
            session_id=SESSION_ID,
            session_alias="CLIENT",
        )
    )

    await scheduler.run_until_idle()

    sample = next(
        item
        for item in tracker.measurements
        if item.metric == SIM_END_TO_END_LATENCY
    )
    assert sample.value == pytest.approx(
        (scheduler.current_time - submitted_at).total_seconds()
    )
    assert sample.value == pytest.approx(0.018)


def test_negative_simulated_latency_is_rejected_not_absolute(caplog):
    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    tracker = LatencyTracker(scheduler, metrics)
    future = scheduler.current_time + timedelta(milliseconds=5)
    tracker.order_submitted(1, SESSION_ID, future)

    with caplog.at_level("WARNING"):
        tracker.order_arrived(1)

    assert not any(
        sample.metric == SIM_INBOUND_LATENCY
        for sample in tracker.measurements
    )
    assert metrics.snapshot().invalid_latency_measurements == 1
    assert any(
        getattr(record, "latency_metric", None) == SIM_INBOUND_LATENCY
        for record in caplog.records
    )


def test_fake_monotonic_clock_duration_is_exact_without_sleep():
    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    tracker = LatencyTracker(scheduler, metrics)

    sample = tracker.measure_real_duration(
        order_id=1,
        session_id=SESSION_ID,
        metric=REAL_MATCHING_DURATION,
        start_ns=10_000,
        end_ns=210_000,
        timestamp=scheduler.current_time,
    )

    assert sample is not None
    assert sample.value == pytest.approx(0.0002)
    assert metrics.snapshot().average_real_matching_duration_us == pytest.approx(
        200.0
    )


def test_partial_ioc_trace_cleans_only_on_terminal_cancel_and_is_idempotent():
    scheduler, engine, _, tracker, reports, _ = wire_stack(
        inbound_ms=0,
        engine_ms=0,
        outbound_ms=0,
        clock=FakeMonotonicClock(1, 2, 3, 4),
    )

    passive_id = engine.submit_order(
        exact_command(
            cl_ord_id="PASSIVE",
            participant_id="PASSIVE",
            symbol="TEST",
            side=Side.SELL,
            order_qty_lots=4,
            price_ticks=100,
            tif=TimeInForce.GTC,
            submitted_at=scheduler.current_time,
            session_id=SESSION_ID,
            session_alias="CLIENT",
        )
    )
    tracker.order_arrived(passive_id)
    engine.process_order(ProcessOrderCommand(passive_id, 0))

    ioc_id = engine.submit_order(
        exact_command(
            cl_ord_id="IOC",
            participant_id="IOC",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=10,
            price_ticks=100,
            tif=TimeInForce.IOC,
            submitted_at=scheduler.current_time,
            session_id=SESSION_ID,
            session_alias="CLIENT",
        )
    )
    tracker.order_arrived(ioc_id)
    engine.process_order(ProcessOrderCommand(ioc_id, 0))
    partial = next(
        report
        for report in reports
        if report.order_id == ioc_id
        and report.exec_type == ExecutionType.TRADE
    )
    terminal = next(
        report
        for report in reports
        if report.order_id == ioc_id
        and report.exec_type == ExecutionType.CANCELED
    )

    assert tracker.report_delivered(partial)
    assert (0, ioc_id) in tracker.orders
    assert not tracker.complete_order(partial)

    assert tracker.report_delivered(terminal)
    before_count = len(tracker.measurements)
    assert tracker.complete_order(terminal)
    tracker.cleanup_order(ioc_id)
    assert (0, ioc_id) not in tracker.orders

    assert not tracker.report_delivered(terminal)
    assert not tracker.complete_order(terminal)
    assert len(tracker.measurements) == before_count

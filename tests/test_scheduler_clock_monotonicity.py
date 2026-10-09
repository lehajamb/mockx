"""Idle draining preserves the current simulated clock and delivery timing."""

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from src.mockx.latency import LatencyScheduler, SchedulerFailedError
from src.mockx.metrics import (
    SIM_END_TO_END_LATENCY,
    SIM_ENGINE_LATENCY,
    SIM_INBOUND_LATENCY,
    SIM_OUTBOUND_LATENCY,
)
from src.mockx.models import (
    OrderStatus,
    SchedulerFailureStatus,
    SchedulerState,
    Side,
    TimeInForce,
)
from tests.exact_helpers import exact_command
from tests.test_latency_metrics import SESSION_ID, wire_stack


@pytest.mark.asyncio
async def test_empty_idle_drain_preserves_current_time():
    scheduler = LatencyScheduler()
    scheduler.advance_time(20)
    current = scheduler.current_time

    assert await scheduler.run_until_idle() == 0
    assert scheduler.current_time == current
    assert scheduler.state == SchedulerState.RUNNING


@pytest.mark.asyncio
async def test_overdue_event_runs_once_at_current_time():
    scheduler = LatencyScheduler()
    origin = scheduler.current_time
    observed = []
    scheduler.register_handler(
        "probe", lambda value: observed.append((value, scheduler.current_time))
    )
    scheduler.schedule_event("probe", "overdue", 10)
    scheduler.advance_time(20)
    current = scheduler.current_time

    assert await scheduler.run_until_idle() == 1
    assert observed == [("overdue", current)]
    assert scheduler.current_time == origin + timedelta(milliseconds=20)
    assert await scheduler.run_until_idle() == 0
    assert observed == [("overdue", current)]
    assert scheduler.current_time == current


@pytest.mark.asyncio
async def test_overdue_children_keep_fifo_and_actual_current_time_deadlines():
    scheduler = LatencyScheduler()
    origin = scheduler.current_time
    observed = []

    def handle(value):
        observed.append((value, scheduler.current_time))
        if value == "overdue-a":
            scheduler.schedule_event("probe", "zero-child", 0)
            scheduler.schedule_event("probe", "delayed-child", 5)

    scheduler.register_handler("probe", handle)
    scheduler.schedule_event("probe", "overdue-a", 10)
    scheduler.schedule_event("probe", "overdue-b", 10)
    scheduler.schedule_event("probe", "current-peer", 20)
    scheduler.schedule_event("probe", "future", 30)
    scheduler.advance_time(20)

    assert await scheduler.run_until_idle() == 6
    assert observed == [
        (value, origin + timedelta(milliseconds=milliseconds))
        for value, milliseconds in (
            ("overdue-a", 20),
            ("overdue-b", 20),
            ("current-peer", 20),
            ("zero-child", 20),
            ("delayed-child", 25),
            ("future", 30),
        )
    ]
    assert scheduler.current_time == origin + timedelta(milliseconds=30)
    assert scheduler.get_pending_count() == 0


@pytest.mark.asyncio
async def test_async_handler_clock_advance_applies_to_remaining_due_work():
    scheduler = LatencyScheduler()
    origin = scheduler.current_time
    observed = []

    async def handle(value):
        observed.append((value, scheduler.current_time))
        if value == "advance":
            scheduler.advance_time(20)

    scheduler.register_handler("probe", handle)
    scheduler.schedule_event("probe", "advance", 20)
    scheduler.schedule_event("probe", "now-overdue", 25)
    scheduler.schedule_event("probe", "future", 50)
    scheduler.advance_time(20)

    assert await scheduler.run_until_idle() == 3
    assert observed == [
        ("advance", origin + timedelta(milliseconds=20)),
        ("now-overdue", origin + timedelta(milliseconds=40)),
        ("future", origin + timedelta(milliseconds=50)),
    ]
    assert scheduler.current_time == origin + timedelta(milliseconds=50)


@pytest.mark.asyncio
async def test_overdue_failure_keeps_deadline_and_actual_failure_time():
    scheduler = LatencyScheduler()
    origin = scheduler.current_time
    cause = RuntimeError("overdue handler failed")
    later_handled = []

    def fail(_value):
        raise cause

    scheduler.register_handler("fail", fail)
    scheduler.register_handler("later", later_handled.append)
    event_id = scheduler.schedule_event("fail", None, 10)
    scheduler.schedule_event("later", "must-not-run", 15)
    scheduler.advance_time(20)
    current = scheduler.current_time

    with pytest.raises(SchedulerFailedError) as raised:
        await scheduler.run_until_idle()

    failure = scheduler.dead_letter
    assert failure is not None
    assert failure.event_id == event_id
    assert failure.scheduled_timestamp == origin + timedelta(milliseconds=10)
    assert failure.failure_timestamp == current
    assert failure.status == SchedulerFailureStatus.HANDLER_EXCEPTION
    assert raised.value.__cause__ is cause
    assert scheduler.current_time == current
    assert scheduler.state == SchedulerState.FAILED
    assert scheduler.failure_count == 1
    assert scheduler.get_pending_count() == 1
    assert later_handled == []
    with pytest.raises(SchedulerFailedError):
        await scheduler.run_until_idle()
    assert scheduler.dead_letter is failure
    assert scheduler.current_time == current


@pytest.mark.asyncio
async def test_idle_drain_shares_event_budget_across_overdue_and_future_work():
    scheduler = LatencyScheduler(max_events_per_advance=3)
    origin = scheduler.current_time
    observed = []
    scheduler.register_handler(
        "probe", lambda value: observed.append((value, scheduler.current_time))
    )
    scheduler.schedule_event("probe", "overdue-a", 10)
    scheduler.schedule_event("probe", "overdue-b", 10)
    scheduler.schedule_event("probe", "future", 25)
    over_budget = scheduler.schedule_event("probe", "over-budget", 30)
    scheduler.schedule_event("probe", "retained", 40)
    scheduler.advance_time(20)

    with pytest.raises(SchedulerFailedError):
        await scheduler.run_until_idle()

    assert observed == [
        ("overdue-a", origin + timedelta(milliseconds=20)),
        ("overdue-b", origin + timedelta(milliseconds=20)),
        ("future", origin + timedelta(milliseconds=25)),
    ]
    failure = scheduler.dead_letter
    assert failure is not None
    assert failure.event_id == over_budget
    assert failure.status == SchedulerFailureStatus.EVENT_PROCESSING_LIMIT_EXCEEDED
    assert failure.scheduled_timestamp == origin + timedelta(milliseconds=30)
    assert failure.failure_timestamp == origin + timedelta(milliseconds=30)
    assert scheduler.current_time == origin + timedelta(milliseconds=30)
    assert scheduler.get_pending_count() == 1
    assert scheduler.get_next_event_time() == origin + timedelta(milliseconds=40)


@pytest.mark.asyncio
async def test_overdue_engine_work_preserves_fix_outbound_delay_and_latency():
    scheduler, engine, metrics, tracker, reports, delivered = wire_stack()
    origin = scheduler.current_time
    order_id = engine.submit_order(
        exact_command(
            cl_ord_id="OVERDUE",
            participant_id="OVERDUE",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=1,
            price_ticks=99,
            tif=TimeInForce.GTC,
            submitted_at=origin,
            session_id=SESSION_ID,
            session_alias="CLIENT",
        )
    )
    scheduler.advance_time(20)

    assert await scheduler.run_until_idle() == 3
    assert len(reports) == len(delivered) == 1
    report = reports[0]
    assert delivered[0] is report
    assert report.timestamp == origin + timedelta(milliseconds=20)
    assert report.ord_status == OrderStatus.NEW
    assert engine.orders[order_id].status == OrderStatus.NEW
    assert scheduler.current_time == origin + timedelta(milliseconds=27)
    assert scheduler.get_pending_count() == 0
    with pytest.raises(FrozenInstanceError):
        report.timestamp = origin

    expected = {
        SIM_INBOUND_LATENCY: 20.0,
        SIM_ENGINE_LATENCY: 0.0,
        SIM_OUTBOUND_LATENCY: 7.0,
        SIM_END_TO_END_LATENCY: 27.0,
    }
    assert {
        sample.metric: sample.value
        for sample in tracker.measurements
        if sample.metric in expected
    } == expected
    snapshot = metrics.snapshot()
    assert snapshot.average_sim_inbound_latency_ms == 20.0
    assert snapshot.average_sim_outbound_latency_ms == 7.0
    assert snapshot.invalid_latency_measurements == 0
    assert await scheduler.run_until_idle() == 0
    assert delivered == [report]


@pytest.mark.asyncio
async def test_overdue_existing_fix_report_keeps_creation_time_and_actual_latency():
    scheduler, engine, _, tracker, reports, delivered = wire_stack()
    origin = scheduler.current_time
    engine.submit_order(
        exact_command(
            cl_ord_id="LATE-DELIVERY",
            participant_id="LATE-DELIVERY",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=1,
            price_ticks=99,
            tif=TimeInForce.GTC,
            submitted_at=origin,
            session_id=SESSION_ID,
            session_alias="CLIENT",
        )
    )
    assert await scheduler.run_until(origin + timedelta(milliseconds=8)) == 2
    assert len(reports) == 1
    report = reports[0]
    assert report.timestamp == origin + timedelta(milliseconds=8)
    assert delivered == []
    assert scheduler.get_next_event_time() == origin + timedelta(milliseconds=15)
    scheduler.advance_time(20)

    assert await scheduler.run_until_idle() == 1
    assert delivered == [report]
    assert delivered[0] is report
    assert report.timestamp == origin + timedelta(milliseconds=8)
    assert scheduler.current_time == origin + timedelta(milliseconds=28)
    expected = {
        SIM_INBOUND_LATENCY: 5.0,
        SIM_ENGINE_LATENCY: 3.0,
        SIM_OUTBOUND_LATENCY: 20.0,
        SIM_END_TO_END_LATENCY: 28.0,
    }
    assert {
        sample.metric: sample.value
        for sample in tracker.measurements
        if sample.metric in expected
    } == expected


@pytest.mark.asyncio
async def test_explicit_set_time_and_generation_reset_can_still_rewind():
    scheduler = LatencyScheduler()
    origin = scheduler.current_time
    scheduler.advance_time(20)
    scheduler.set_time(origin + timedelta(milliseconds=5))
    assert scheduler.current_time == origin + timedelta(milliseconds=5)
    discarded = scheduler.schedule_event("probe", "discarded", 10)

    scheduler.reset()

    assert scheduler.current_time == origin
    assert scheduler.get_pending_count() == 0
    observed = []
    scheduler.register_handler("probe", observed.append)
    fresh = scheduler.schedule_event("probe", "new-generation", 0)
    assert fresh != discarded
    assert await scheduler.run_until_idle() == 1
    assert observed == ["new-generation"]
    assert scheduler.current_time == origin

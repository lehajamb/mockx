"""Fail-stop scheduler state, diagnostics, and processing-budget tests."""

from datetime import timedelta

import pytest

from src.mockx.latency import (
    LatencyScheduler,
    SchedulerFailedError,
)
from src.mockx.models import (
    SchedulerFailureStatus,
    SchedulerState,
)


@pytest.mark.asyncio
async def test_handler_failure_is_dead_lettered_and_stops_all_later_work():
    scheduler = LatencyScheduler()
    original_error = RuntimeError("deterministic handler failure")
    payload = {"id": 7, "nested": ["mutable"]}
    later_handled = []

    def fail_before_mutation(_data):
        raise original_error

    scheduler.register_handler("fail", fail_before_mutation)
    scheduler.register_handler("later", later_handled.append)
    failed_event_id = scheduler.schedule_event("fail", payload, 0)
    scheduler.schedule_event("later", "must-not-run", 1)
    payload["id"] = 99
    payload["nested"].append("changed")

    with pytest.raises(SchedulerFailedError) as raised:
        await scheduler.run_until(
            scheduler.current_time + timedelta(milliseconds=1)
        )

    assert raised.value.__cause__ is original_error
    assert scheduler.failure_cause is original_error
    assert scheduler.state == SchedulerState.FAILED
    assert scheduler.failure_count == 1
    assert scheduler.get_pending_count() == 1
    assert later_handled == []

    failure = scheduler.dead_letter
    assert failure is not None
    assert failure.event_id == failed_event_id
    assert failure.scheduler_sequence == 0
    assert failure.status == SchedulerFailureStatus.HANDLER_EXCEPTION
    assert failure.retry_attempted is False
    assert failure.exception_type == "RuntimeError"
    assert failure.exception_message == "deterministic handler failure"
    assert dict(failure.event_metadata) == {
        "data_type": "dict",
        "id": 7,
        "nested": "list",
    }

    pending_before = scheduler.get_pending_count()
    with pytest.raises(SchedulerFailedError):
        scheduler.schedule_event("later", "rejected", 0)
    assert scheduler.get_pending_count() == pending_before

    with pytest.raises(SchedulerFailedError):
        await scheduler.process_events()
    with pytest.raises(SchedulerFailedError):
        await scheduler.run_until(scheduler.current_time)
    with pytest.raises(SchedulerFailedError):
        await scheduler.run_until_idle()
    with pytest.raises(SchedulerFailedError):
        scheduler.advance_time(1)
    with pytest.raises(SchedulerFailedError):
        scheduler.set_time(scheduler.current_time)
    with pytest.raises(SchedulerFailedError):
        scheduler.clear()


@pytest.mark.asyncio
async def test_reset_clears_failure_and_starts_a_new_event_generation():
    scheduler = LatencyScheduler()
    scheduler.register_handler(
        "fail",
        lambda _data: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    first_event_id = scheduler.schedule_event("fail", 1, 0)

    with pytest.raises(SchedulerFailedError):
        await scheduler.process_events()

    scheduler.reset()

    assert scheduler.state == SchedulerState.RUNNING
    assert scheduler.dead_letter is None
    assert scheduler.failure_cause is None
    assert scheduler.failure_count == 0
    assert scheduler.get_pending_count() == 0

    handled = []
    scheduler.register_handler("ok", handled.append)
    next_event_id = scheduler.schedule_event("ok", 2, 0)
    await scheduler.process_events()

    assert handled == [2]
    assert next_event_id != first_event_id
    assert next_event_id.endswith(":0")


@pytest.mark.asyncio
async def test_unregistered_handler_is_a_fatal_dead_letter():
    scheduler = LatencyScheduler()
    scheduler.schedule_event("missing", {"id": "MISSING"}, 0)

    with pytest.raises(SchedulerFailedError):
        await scheduler.process_events()

    assert scheduler.dead_letter is not None
    assert (
        scheduler.dead_letter.status
        == SchedulerFailureStatus.HANDLER_NOT_REGISTERED
    )
    assert scheduler.dead_letter.exception_type == "EventHandlerNotRegistered"


@pytest.mark.asyncio
async def test_same_time_self_rescheduling_trips_processing_limit():
    scheduler = LatencyScheduler(max_events_per_advance=5)
    handled = []

    def reschedule(value):
        handled.append(value)
        scheduler.schedule_event("loop", value + 1, 0)

    scheduler.register_handler("loop", reschedule)
    scheduler.schedule_event("loop", 0, 0)

    with pytest.raises(SchedulerFailedError) as raised:
        await scheduler.run_until(scheduler.current_time)

    assert handled == [0, 1, 2, 3, 4]
    assert scheduler.state == SchedulerState.FAILED
    assert scheduler.dead_letter is not None
    assert (
        scheduler.dead_letter.status
        == SchedulerFailureStatus.EVENT_PROCESSING_LIMIT_EXCEEDED
    )
    assert (
        scheduler.dead_letter.exception_type
        == "EventProcessingLimitExceeded"
    )
    assert dict(scheduler.dead_letter.event_metadata)["value"] == 5
    assert isinstance(raised.value.__cause__, RuntimeError)


@pytest.mark.parametrize("invalid_limit", [0, -1, True, 1.5])
def test_processing_limit_requires_a_positive_integer(invalid_limit):
    with pytest.raises(ValueError):
        LatencyScheduler(max_events_per_advance=invalid_limit)

"""Queue-owned delivery commands preserve deadlines and attempt budgets."""

from dataclasses import replace
from datetime import timedelta

import pytest

from src.mockx.delivery import OutboundFixDeliveryQueue
from src.mockx.models import FixDeliveryDueCommand, FixDeliveryState

from .test_fix_delivery_queue import NOW, SESSION, report


def _enqueue(queue, *, due=NOW):
    record, fresh = queue.enqueue(
        delivery_id="G00000001-D00000001",
        destination=SESSION,
        payload=report("G00000001-E00000001", 1),
        simulated_due_timestamp=due,
    )
    assert fresh
    command = queue.current_due_command(record.delivery_id)
    assert command is not None
    return record, command


def test_initial_deadline_is_inclusive_and_early_events_do_not_consume_it():
    queue = OutboundFixDeliveryQueue()
    due = NOW + timedelta(milliseconds=10)
    record, command = _enqueue(queue, due=due)
    before = queue.snapshot(now=NOW)

    assert queue.mark_due(command, simulated_timestamp=due - timedelta(microseconds=1)) is None
    assert queue.get(record.delivery_id) == record
    assert queue.current_due_command(record.delivery_id) == command
    assert queue.snapshot(now=NOW) == before

    accepted = queue.mark_due(command, simulated_timestamp=due)
    assert accepted.state == FixDeliveryState.DUE
    assert queue.current_due_command(record.delivery_id) is None
    after = queue.snapshot(now=NOW)
    assert queue.mark_due(command, simulated_timestamp=due) is None
    assert queue.snapshot(now=NOW) == after


def test_changed_deadline_with_current_version_is_not_the_armed_command():
    queue = OutboundFixDeliveryQueue()
    due = NOW + timedelta(milliseconds=10)
    record, command = _enqueue(queue, due=due)
    altered = replace(command, due_timestamp=NOW)

    assert queue.mark_due(altered, simulated_timestamp=NOW) is None
    assert queue.current_due_command(record.delivery_id) == command
    assert queue.get(record.delivery_id) == record
    assert queue.mark_due(command, simulated_timestamp=due).state == FixDeliveryState.DUE


def test_reconnect_rearms_same_time_retry_with_a_fresh_version():
    queue = OutboundFixDeliveryQueue(retry_interval_ms=25, max_attempts_per_cycle=2)
    record, initial = _enqueue(queue)
    queue.mark_due(initial, simulated_timestamp=NOW)
    queue.start_attempt(record.delivery_id, simulated_timestamp=NOW)
    queue.record_transport_failure(record.delivery_id, reason="first", simulated_timestamp=NOW)
    old_retry = queue.current_due_command(record.delivery_id)

    queue.start_reconnect_cycle(SESSION.session_id)
    assert queue.current_due_command(record.delivery_id) is None
    queue.start_attempt(record.delivery_id, simulated_timestamp=NOW)
    pending = queue.record_transport_failure(
        record.delivery_id, reason="reconnect", simulated_timestamp=NOW,
    )
    new_retry = queue.current_due_command(record.delivery_id)
    assert old_retry.due_timestamp == new_retry.due_timestamp
    assert new_retry.schedule_version > old_retry.schedule_version
    before = queue.snapshot(now=NOW)
    assert queue.mark_due(old_retry, simulated_timestamp=old_retry.due_timestamp) is None
    assert queue.get(record.delivery_id) == pending
    assert queue.snapshot(now=NOW) == before
    assert queue.mark_due(new_retry, simulated_timestamp=new_retry.due_timestamp).state == FixDeliveryState.DUE


@pytest.mark.parametrize("due_ms", [-5, 10])
def test_reset_rearms_pending_records_without_losing_original_due_time(due_ms):
    queue = OutboundFixDeliveryQueue()
    due = NOW + timedelta(milliseconds=due_ms)
    record, old_command = _enqueue(queue, due=due)
    (command, delay), = queue.prepare_after_scheduler_reset(NOW)
    assert delay == max(0, due_ms)
    assert command.due_timestamp == max(NOW, due)
    assert command.schedule_version > old_command.schedule_version
    assert queue.get(record.delivery_id) == record
    assert queue.mark_due(old_command, simulated_timestamp=command.due_timestamp) is None
    assert queue.mark_due(command, simulated_timestamp=command.due_timestamp).state == FixDeliveryState.DUE


def test_reset_versions_are_not_reused_at_the_same_deadline():
    queue = OutboundFixDeliveryQueue()
    record, initial = _enqueue(queue)
    (first, _), = queue.prepare_after_scheduler_reset(NOW)
    (second, _), = queue.prepare_after_scheduler_reset(NOW)
    assert initial.due_timestamp == first.due_timestamp == second.due_timestamp
    assert initial.schedule_version < first.schedule_version < second.schedule_version
    assert queue.mark_due(initial, simulated_timestamp=NOW) is None
    assert queue.mark_due(first, simulated_timestamp=NOW) is None
    assert queue.get(record.delivery_id).state == FixDeliveryState.PENDING
    assert queue.mark_due(second, simulated_timestamp=NOW).state == FixDeliveryState.DUE


def test_attempt_admission_enforces_budget_without_mutating_counters():
    queue = OutboundFixDeliveryQueue(max_attempts_per_cycle=2)
    record, command = _enqueue(queue)
    queue.mark_due(command, simulated_timestamp=NOW)
    queue.start_attempt(record.delivery_id, simulated_timestamp=NOW)
    exhausted = queue.start_attempt(record.delivery_id, simulated_timestamp=NOW)
    before = queue.snapshot(now=NOW)
    with pytest.raises(ValueError, match="cycle is exhausted"):
        queue.start_attempt(record.delivery_id, simulated_timestamp=NOW)
    assert queue.get(record.delivery_id) == exhausted
    assert queue.snapshot(now=NOW) == before


def test_disconnected_delivery_invalidates_a_still_armed_reset_command():
    queue = OutboundFixDeliveryQueue()
    record, initial = _enqueue(queue)
    queue.mark_due(initial, simulated_timestamp=NOW)
    (reset, _), = queue.prepare_after_scheduler_reset(NOW)
    assert queue.get(record.delivery_id).state == FixDeliveryState.DUE
    pending = queue.note_unavailable(record.delivery_id, reason="disconnected")
    assert queue.current_due_command(record.delivery_id) is None
    before = queue.snapshot(now=NOW)
    assert queue.mark_due(reset, simulated_timestamp=NOW) is None
    assert queue.get(record.delivery_id) == pending
    assert queue.snapshot(now=NOW) == before


@pytest.mark.parametrize("terminal", ["delivered", "permanent"])
def test_terminal_transition_invalidates_a_still_armed_reset_command(terminal):
    queue = OutboundFixDeliveryQueue()
    record, initial = _enqueue(queue)
    queue.mark_due(initial, simulated_timestamp=NOW)
    (reset, _), = queue.prepare_after_scheduler_reset(NOW)
    if terminal == "delivered":
        result = queue.record_delivered(record.delivery_id)
    else:
        result = queue.record_permanent_failure(record.delivery_id, reason="serialization")
    assert queue.current_due_command(record.delivery_id) is None
    before = queue.snapshot(now=NOW)
    assert queue.mark_due(reset, simulated_timestamp=NOW) is None
    assert queue.get(record.delivery_id) == result
    assert queue.snapshot(now=NOW) == before


def test_unknown_command_is_a_noop():
    queue = OutboundFixDeliveryQueue()
    command = FixDeliveryDueCommand("unknown", 1, NOW)
    before = queue.snapshot(now=NOW)
    assert queue.mark_due(command, simulated_timestamp=NOW) is None
    assert queue.snapshot(now=NOW) == before


def test_delivery_id_only_events_are_rejected():
    queue = OutboundFixDeliveryQueue()
    record, command = _enqueue(queue)
    with pytest.raises(TypeError, match="FixDeliveryDueCommand"):
        queue.mark_due(record.delivery_id, simulated_timestamp=NOW)
    assert queue.current_due_command(record.delivery_id) == command
    assert queue.get(record.delivery_id) == record

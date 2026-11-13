"""Independent delivery accounting across retries, pruning and generations."""

from collections import Counter
from dataclasses import replace
from datetime import timedelta

import pytest

from src.mockx.delivery import OutboundFixDeliveryQueue
from src.mockx.models import FixDeliveryState, SessionIdentity
from tests.test_fix_delivery_queue import NOW, SESSION, report


def test_delivery_metrics_do_not_scan_retained_terminal_history():
    queue = OutboundFixDeliveryQueue(wall_clock=lambda: NOW)
    for number in range(40):
        record, _ = queue.enqueue(
            delivery_id=f"D{number}",
            destination=SESSION,
            payload=report(f"E{number}", number + 1),
            simulated_due_timestamp=NOW,
        )
        queue.mark_due(
            queue.current_due_command(record.delivery_id), simulated_timestamp=NOW
        )
        queue.record_delivered(record.delivery_id)
    pending, _ = queue.enqueue(
        delivery_id="PENDING",
        destination=SESSION,
        payload=report("PENDING-E", 41),
        simulated_due_timestamp=NOW,
    )

    class NoHistoryScan(dict):
        def values(self):
            raise AssertionError("delivery metrics scanned retained history")

        def items(self):
            raise AssertionError("delivery metrics scanned retained history")

        def __iter__(self):
            raise AssertionError("delivery metrics scanned retained history")

    queue._records = NoHistoryScan(queue._records)
    snapshot = queue.snapshot(now=NOW + timedelta(seconds=2))
    assert snapshot.delivered == snapshot.retained_delivered_records == 40
    assert snapshot.queue_depth == snapshot.pending == 1
    assert snapshot.oldest_pending_age_seconds == 2
    assert queue.pending_order_keys() == {(1, 41)}
    scheduled = queue.prepare_after_scheduler_reset(NOW)
    assert [command.delivery_id for command, _ in scheduled] == [pending.delivery_id]


def test_delivery_snapshot_matches_independent_history_after_every_transition():
    wall = [NOW]
    queue = OutboundFixDeliveryQueue(
        retry_interval_ms=5,
        max_attempts_per_cycle=2,
        terminal_retention_records=2,
        wall_clock=lambda: wall[0],
    )
    other = SessionIdentity("FIX.4.4:MOCKX->OTHER", alias="OTHER")
    history = {}
    failures = 0

    def check(record=None):
        if record is not None:
            history[record.delivery_id] = record
        retained = queue.snapshot_records()
        for item in retained:
            history[item.delivery_id] = item
        snapshot = queue.snapshot(now=wall[0])
        states = Counter(item.state for item in retained)
        pending = [
            item for item in retained if item.state != FixDeliveryState.DELIVERED
        ]
        assert snapshot.enqueued == len(history)
        assert snapshot.queue_depth == len(pending)
        assert snapshot.pending == states[FixDeliveryState.PENDING]
        assert snapshot.due == states[FixDeliveryState.DUE]
        assert snapshot.retry_pending == states[FixDeliveryState.RETRY_PENDING]
        assert snapshot.retained_delivered_records == states[FixDeliveryState.DELIVERED]
        assert snapshot.delivered == sum(
            item.state == FixDeliveryState.DELIVERED for item in history.values()
        )
        assert snapshot.permanently_failed == sum(
            item.state == FixDeliveryState.PERMANENTLY_FAILED
            for item in history.values()
        )
        assert snapshot.attempts == sum(item.attempt_count for item in history.values())
        assert snapshot.retries == sum(
            max(0, item.attempt_count - 1) for item in history.values()
        )
        assert snapshot.failures == failures
        assert snapshot.abandoned == 0
        assert snapshot.sessions_with_backlog == len(
            {item.destination.session_id for item in pending}
        )
        expected_age = (
            max(
                0, (wall[0] - min(item.enqueued_at for item in pending)).total_seconds()
            )
            if pending
            else None
        )
        assert snapshot.oldest_pending_age_seconds == expected_age
        assert len({item.delivery_id for item in retained}) == len(retained)

    def enqueue(number, destination=SESSION, generation=1):
        wall[0] += timedelta(seconds=1)
        record, fresh = queue.enqueue(
            delivery_id=f"D{number}",
            destination=destination,
            payload=replace(report(f"E{number}", number), run_generation=generation),
            simulated_due_timestamp=NOW,
        )
        assert fresh
        check(record)
        return record

    first, second, blocked = enqueue(1), enqueue(2), enqueue(3, other)
    later_due = queue.current_due_command(second.delivery_id)
    check(queue.mark_due(later_due, simulated_timestamp=NOW))
    with pytest.raises(ValueError, match="FIFO head"):
        queue.record_delivered(second.delivery_id)
    check()
    check(
        queue.mark_due(
            queue.current_due_command(first.delivery_id), simulated_timestamp=NOW
        )
    )
    failures += 1
    check(queue.note_unavailable(first.delivery_id, reason="offline"))
    check(queue.start_reconnect_cycle(SESSION.session_id))
    for attempt in range(2):
        check(queue.start_attempt(first.delivery_id, simulated_timestamp=NOW))
        failures += 1
        check(
            queue.record_transport_failure(
                first.delivery_id, reason="transport", simulated_timestamp=NOW
            )
        )
        if attempt == 0:
            command = queue.current_due_command(first.delivery_id)
            assert queue.mark_due(command, simulated_timestamp=NOW) is None
            check()
            check(queue.mark_due(command, simulated_timestamp=command.due_timestamp))
    queue.prepare_after_scheduler_reset(NOW)
    check()
    check(queue.start_attempt(first.delivery_id, simulated_timestamp=NOW))
    check(queue.record_delivered(first.delivery_id))
    check(queue.record_delivered(first.delivery_id))  # Idempotent success.
    assert queue.mark_due(later_due, simulated_timestamp=NOW) is None
    check()
    check(queue.record_delivered(second.delivery_id))
    check(
        queue.mark_due(
            queue.current_due_command(blocked.delivery_id), simulated_timestamp=NOW
        )
    )
    failures += 1
    check(queue.record_permanent_failure(blocked.delivery_id, reason="serialization"))
    check(queue.record_permanent_failure(blocked.delivery_id, reason="same failure"))
    with pytest.raises(ValueError, match="permanently failed"):
        queue.record_delivered(blocked.delivery_id)
    check()
    fourth = enqueue(4)
    check(
        queue.mark_due(
            queue.current_due_command(fourth.delivery_id), simulated_timestamp=NOW
        )
    )
    check(queue.record_delivered(fourth.delivery_id))  # Prunes first payload.
    duplicate, fresh = queue.enqueue(
        delivery_id="DUPLICATE",
        destination=SESSION,
        payload=first.payload,
        simulated_due_timestamp=NOW,
    )
    assert duplicate is None and not fresh
    check()
    queue.begin_generation(2)
    check()
    fifth = enqueue(5, generation=2)
    queue.prepare_after_scheduler_reset(NOW)
    check()
    check(
        queue.mark_due(
            queue.current_due_command(fifth.delivery_id), simulated_timestamp=NOW
        )
    )
    check(queue.record_delivered(fifth.delivery_id))

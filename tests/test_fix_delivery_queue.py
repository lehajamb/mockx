"""Pure state-machine tests for the process-lifetime FIX delivery queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.mockx.delivery import OutboundFixDeliveryQueue
from src.mockx.models import (
    ExecutionType,
    FixDeliveryState,
    Order,
    OrderStatus,
    SessionIdentity,
    Side,
    TimeInForce,
    create_execution_report,
)


NOW = datetime(1970, 1, 1, tzinfo=timezone.utc)
SESSION = SessionIdentity("FIX.4.4:MOCKX->CLIENT", alias="CLIENT")


def report(exec_id: str, order_id: int):
    order = Order(
        id=order_id,
        cl_ord_id=f"ORDER-{order_id}",
        symbol="TEST",
        side=Side.BUY,
        qty=1.0,
        price=100.0,
        tif=TimeInForce.GTC,
        ts_submitted=NOW,
        session_id=SESSION.session_id,
        session_alias=SESSION.alias,
        exchange_order_id=f"G00000001-O{order_id:08d}",
        run_generation=1,
    )
    order.transition_to(OrderStatus.NEW)
    return create_execution_report(
        order,
        exec_type=ExecutionType.NEW,
        timestamp=NOW,
        exec_id=exec_id,
    )


def test_queue_deduplicates_domain_id_and_keeps_per_session_sequence():
    queue = OutboundFixDeliveryQueue()
    first_payload = report("G00000001-E00000001", 1)
    second_payload = report("G00000001-E00000002", 2)

    first, fresh = queue.enqueue(
        delivery_id="G00000001-D00000001",
        destination=SESSION,
        payload=first_payload,
        simulated_due_timestamp=NOW,
    )
    duplicate, duplicate_fresh = queue.enqueue(
        delivery_id="IGNORED",
        destination=SESSION,
        payload=first_payload,
        simulated_due_timestamp=NOW,
    )
    second, second_fresh = queue.enqueue(
        delivery_id="G00000001-D00000002",
        destination=SESSION,
        payload=second_payload,
        simulated_due_timestamp=NOW,
    )

    assert fresh and second_fresh and not duplicate_fresh
    assert duplicate == first
    assert (first.session_sequence, second.session_sequence) == (0, 1)
    assert queue.snapshot().enqueued == 2


def test_later_due_record_cannot_overtake_pending_fifo_head():
    queue = OutboundFixDeliveryQueue()
    first, _ = queue.enqueue(
        delivery_id="G00000001-D00000001",
        destination=SESSION,
        payload=report("G00000001-E00000001", 1),
        simulated_due_timestamp=NOW + timedelta(milliseconds=10),
    )
    second, _ = queue.enqueue(
        delivery_id="G00000001-D00000002",
        destination=SESSION,
        payload=report("G00000001-E00000002", 2),
        simulated_due_timestamp=NOW,
    )

    queue.mark_due(second.delivery_id)
    assert queue.head_ready(SESSION.session_id) is None

    queue.mark_due(first.delivery_id)
    assert queue.head_ready(SESSION.session_id).delivery_id == first.delivery_id
    queue.start_attempt(
        first.delivery_id,
        simulated_timestamp=NOW + timedelta(milliseconds=10),
    )
    queue.record_delivered(first.delivery_id)
    assert queue.head_ready(SESSION.session_id).delivery_id == second.delivery_id


def test_retry_cycle_is_bounded_and_reset_reschedules_nonterminal_records():
    queue = OutboundFixDeliveryQueue(
        retry_interval_ms=25,
        max_attempts_per_cycle=2,
    )
    record, _ = queue.enqueue(
        delivery_id="G00000001-D00000001",
        destination=SESSION,
        payload=report("G00000001-E00000001", 1),
        simulated_due_timestamp=NOW,
    )
    queue.mark_due(record.delivery_id)
    queue.start_attempt(record.delivery_id, simulated_timestamp=NOW)
    failed = queue.record_transport_failure(
        record.delivery_id,
        reason="first",
        simulated_timestamp=NOW,
    )
    assert failed.next_retry_timestamp == NOW + timedelta(milliseconds=25)

    queue.mark_due(record.delivery_id)
    queue.start_attempt(
        record.delivery_id,
        simulated_timestamp=NOW + timedelta(milliseconds=25),
    )
    exhausted = queue.record_transport_failure(
        record.delivery_id,
        reason="second",
        simulated_timestamp=NOW + timedelta(milliseconds=25),
    )
    assert exhausted.state == FixDeliveryState.RETRY_PENDING
    assert exhausted.next_retry_timestamp is None
    assert exhausted.attempt_count == 2

    assert queue.prepare_after_scheduler_reset(NOW) == (
        (record.delivery_id, 0),
    )
    resumed = queue.get(record.delivery_id)
    assert resumed.state == FixDeliveryState.DUE
    assert resumed.cycle_attempt_count == 0


def test_queue_rejects_delivery_transitions_from_a_non_due_state():
    queue = OutboundFixDeliveryQueue()
    payload = report("G00000001-E00000004", 4)
    record, _ = queue.enqueue(
        delivery_id="G00000001-D00000004",
        destination=SESSION,
        payload=payload,
        simulated_due_timestamp=NOW,
    )

    with pytest.raises(ValueError, match="not due"):
        queue.note_unavailable(record.delivery_id, reason="disconnected")
    with pytest.raises(ValueError, match="not due"):
        queue.record_transport_failure(
            record.delivery_id,
            reason="failed",
            simulated_timestamp=NOW,
        )
    with pytest.raises(ValueError, match="not due"):
        queue.record_permanent_failure(
            record.delivery_id,
            reason="cannot serialize",
        )
    with pytest.raises(ValueError, match="not due"):
        queue.record_delivered(record.delivery_id)

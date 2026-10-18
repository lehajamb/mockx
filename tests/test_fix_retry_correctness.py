"""A scheduled FIX event only owns the delivery deadline it was armed for."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from src.mockx.models import (
    OrderCancelReject,
    ExecutionReport,
    FixDeliveryState,
    SessionIdentity,
)
from src.mockx.configuration import validate_config
from src.mockx.main import MockExchange

from .test_fix_ingress_pipeline import _cancel, _exchange, _new_order, _start
from .api_helpers import api_config
from .test_persistence_run_failure import _saturate_on_report


def _message(kind: str, sequence: int = 1):
    if kind == "execution_report":
        return _new_order(f"RETRY-{sequence}", sequence)
    return _cancel(f"CANCEL-{sequence}", "MISSING", sequence)


def _delivery_event(exchange):
    return next(
        event
        for event in exchange.scheduler.event_queue
        if event.event_type == "fix_delivery_due"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["execution_report", "cancel_reject"])
@pytest.mark.parametrize("failure", ["false", "exception"])
async def test_reconnect_invalidates_old_deadline_and_obeys_cycle_limit(kind, failure):
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    queue = exchange.fix_app.delivery_queue
    queue.max_attempts_per_cycle = 2
    start = exchange.scheduler.current_time
    attempts = []
    reconnect_attempt = asyncio.Event()

    def unavailable(_transport, payload):
        attempts.append((exchange.scheduler.current_time, payload))
        if len(attempts) == 2:
            reconnect_attempt.set()
        if failure == "exception":
            raise RuntimeError("temporary transport failure")
        return False

    exchange.fix_app.transport_sender = unavailable
    try:
        exchange.fix_app.fromApp(_message(kind), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)
        record = queue.snapshot_records()[0]
        assert isinstance(
            record.payload,
            ExecutionReport if kind == "execution_report" else OrderCancelReject,
        )
        old_event = _delivery_event(exchange)
        assert old_event.timestamp == start + timedelta(milliseconds=1000)

        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=500))
        session_id = sessions["CLIENT"].toString()
        assert exchange.fix_app.unregister_session(session_id)
        exchange.fix_app.register_session(
            SessionIdentity(session_id, alias="CLIENT"), object()
        )
        await asyncio.wait_for(reconnect_attempt.wait(), timeout=2)
        assert [timestamp for timestamp, _ in attempts] == [
            start, start + timedelta(milliseconds=500)
        ]
        before_obsolete = queue.snapshot(now=start)

        assert await exchange.lifecycle.advance_to(start + timedelta(milliseconds=1000)) == 1
        assert queue.snapshot(now=start) == before_obsolete
        assert [timestamp for timestamp, _ in attempts] == [
            start, start + timedelta(milliseconds=500)
        ]

        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=1500))
        assert [timestamp for timestamp, _ in attempts] == [
            start,
            start + timedelta(milliseconds=500),
            start + timedelta(milliseconds=1500),
        ]
        assert all(payload is record.payload for _, payload in attempts)
        exhausted = queue.get(record.delivery_id)
        assert exhausted.state == FixDeliveryState.RETRY_PENDING
        assert exhausted.cycle_attempt_count == 2
        assert exhausted.next_retry_timestamp is None
        finished_stats = queue.snapshot(now=start)

        # Duplicate leftover events cannot restart an exhausted retry cycle.
        exchange.scheduler.schedule_event("fix_delivery_due", old_event.data, 500)
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=5000))
        assert queue.snapshot(now=start) == finished_stats
        assert exchange.scheduler.state.value == "RUNNING"
        assert "mockx_fix_delivery_attempts_total 3.0" in (
            exchange.metrics.prometheus_payload().decode()
        )
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_pruned_delivered_record_does_not_fail_obsolete_event():
    exchange = _exchange(outbound_ms=10)
    sessions, delivered = await _start(exchange)
    exchange.fix_app.delivery_queue.terminal_retention_records = 1
    start = exchange.scheduler.current_time
    try:
        exchange.fix_app.fromApp(_new_order("PRUNED", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)
        old_event = _delivery_event(exchange)
        old_id = exchange.fix_app.delivery_queue.snapshot_records()[0].delivery_id
        exchange.scheduler.schedule_event("fix_delivery_due", old_event.data, 1000)
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=10))

        exchange.fix_app.fromApp(_new_order("RETAINED", 2), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=20))
        assert old_id not in {
            record.delivery_id
            for record in exchange.fix_app.delivery_queue.snapshot_records()
        }
        before_obsolete = exchange.fix_app.delivery_queue.snapshot(now=start)

        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=1000))
        assert [payload.cl_ord_id for payload in delivered] == [
            "PRUNED", "RETAINED"
        ]
        assert exchange.fix_app.delivery_queue.snapshot(now=start) == before_obsolete
        assert exchange.scheduler.state.value == "RUNNING"
        assert exchange.scheduler.dead_letter is None
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_duplicate_equal_time_head_event_cannot_retry_or_drain_fifo_early():
    exchange = _exchange(outbound_ms=10)
    sessions, delivered = await _start(exchange, "CLIENT", "CLIENT2")
    start = exchange.scheduler.current_time
    attempts = []

    def sender(_transport, payload):
        attempts.append((payload.cl_ord_id, exchange.scheduler.current_time))
        if len(attempts) == 1:
            return False
        delivered.append(payload)
        return True

    exchange.fix_app.transport_sender = sender
    try:
        exchange.fix_app.fromApp(_new_order("FIFO-HEAD", 1), sessions["CLIENT"])
        exchange.fix_app.fromApp(_new_order("FIFO-LATER", 2), sessions["CLIENT"])
        exchange.fix_app.fromApp(_new_order("INDEPENDENT", 1), sessions["CLIENT2"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)
        head_command = _delivery_event(exchange).data
        exchange.scheduler.schedule_event("fix_delivery_due", head_command, 10)
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=10))

        assert attempts == [
            ("FIFO-HEAD", start + timedelta(milliseconds=10)),
            ("INDEPENDENT", start + timedelta(milliseconds=10)),
        ]
        records = exchange.fix_app.delivery_queue.snapshot_records()
        assert [record.state for record in records] == [
            FixDeliveryState.RETRY_PENDING,
            FixDeliveryState.DUE,
            FixDeliveryState.DELIVERED,
        ]
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=1010))
        assert attempts[-2:] == [
            ("FIFO-HEAD", start + timedelta(milliseconds=1010)),
            ("FIFO-LATER", start + timedelta(milliseconds=1010)),
        ]
        assert [payload.cl_ord_id for payload in delivered] == [
            "INDEPENDENT", "FIFO-HEAD", "FIFO-LATER"
        ]
        stats = exchange.fix_app.delivery_queue.snapshot()
        assert (stats.attempts, stats.retries, stats.failures, stats.delivered) == (
            4, 1, 1, 3
        )
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_duplicate_retry_commands_are_consumed_once_at_equal_timestamp():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    start = exchange.scheduler.current_time
    attempts = []

    def sender(_transport, payload):
        attempts.append((payload, exchange.scheduler.current_time))
        if len(attempts) < 3:
            return False
        delivered.append(payload)
        return True

    exchange.fix_app.transport_sender = sender
    try:
        exchange.fix_app.fromApp(_new_order("DUPLICATE-RETRY", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)
        command = _delivery_event(exchange).data
        exchange.scheduler.schedule_event("fix_delivery_due", command, 1000)
        exchange.scheduler.schedule_event("fix_delivery_due", command, 1000)
        assert await exchange.lifecycle.advance_to(
            start + timedelta(milliseconds=1000)
        ) == 3
        assert [timestamp for _, timestamp in attempts] == [
            start, start + timedelta(milliseconds=1000)
        ]
        assert delivered == []
        assert exchange.fix_app.delivery_queue.snapshot().attempts == 2
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=2000))
        assert len(attempts) == 3
        assert attempts[-1][1] == start + timedelta(milliseconds=2000)
        assert all(payload is delivered[0] for payload, _ in attempts)
        stats = exchange.fix_app.delivery_queue.snapshot()
        assert (stats.attempts, stats.retries, stats.failures, stats.delivered) == (
            3, 2, 2, 1
        )
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_disconnected_due_event_is_invalidated_before_reconnect():
    exchange = _exchange(outbound_ms=10)
    sessions, delivered = await _start(exchange)
    start = exchange.scheduler.current_time
    session_id = sessions["CLIENT"].toString()
    attempted = asyncio.Event()

    def sender(_transport, _payload):
        attempted.set()
        return False

    try:
        exchange.fix_app.fromApp(_new_order("DISCONNECTED-COMMAND", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)
        old_command = _delivery_event(exchange).data
        assert exchange.fix_app.unregister_session(session_id)
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=10))
        queue = exchange.fix_app.delivery_queue
        assert queue.current_due_command(old_command.delivery_id) is None
        assert queue.snapshot().attempts == 0
        assert queue.snapshot().failures == 1

        exchange.fix_app.transport_sender = sender
        exchange.fix_app.register_session(
            SessionIdentity(session_id, alias="CLIENT"), object()
        )
        await asyncio.wait_for(attempted.wait(), timeout=2)
        retry_command = queue.current_due_command(old_command.delivery_id)
        assert retry_command.schedule_version > old_command.schedule_version
        assert retry_command.due_timestamp == start + timedelta(milliseconds=1010)
        before_obsolete = queue.snapshot(now=start)
        exchange.fix_app.process_delivery_due(old_command)
        assert queue.snapshot(now=start) == before_obsolete
        assert delivered == []
        assert exchange.scheduler.state.value == "RUNNING"
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_reset_rearms_pending_deadline_and_invalidates_old_command():
    exchange = _exchange(outbound_ms=20)
    sessions, delivered = await _start(exchange)
    start = exchange.scheduler.current_time
    try:
        exchange.fix_app.fromApp(_new_order("OLD-RUN", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)
        old_command = _delivery_event(exchange).data
        await exchange.lifecycle.end_session()
        await exchange.lifecycle.start_session("new-run")
        reset_command = _delivery_event(exchange).data
        assert reset_command.delivery_id == old_command.delivery_id
        assert reset_command.schedule_version > old_command.schedule_version
        assert reset_command.due_timestamp == old_command.due_timestamp

        exchange.scheduler.schedule_event("fix_delivery_due", old_command, 10)
        exchange.fix_app.fromApp(_new_order("NEW-RUN", 2), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=10))
        assert delivered == []
        assert exchange.fix_app.delivery_queue.snapshot().attempts == 0
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=20))
        assert [payload.cl_ord_id for payload in delivered] == [
            "OLD-RUN", "NEW-RUN"
        ]
        assert [payload.run_generation for payload in delivered] == [1, 2]
        assert exchange.fix_app.delivery_queue.snapshot().attempts == 2
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_reset_current_command_resumes_exhausted_due_backlog():
    exchange = _exchange(outbound_ms=10)
    sessions, delivered = await _start(exchange)
    exchange.fix_app.delivery_queue.max_attempts_per_cycle = 1
    start = exchange.scheduler.current_time
    attempts = []

    def sender(_transport, payload):
        attempts.append(payload)
        if len(attempts) == 1:
            return False
        delivered.append(payload)
        return True

    exchange.fix_app.transport_sender = sender
    try:
        exchange.fix_app.fromApp(_new_order("RESET-RETRY", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)
        old_command = _delivery_event(exchange).data
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=10))
        record = exchange.fix_app.delivery_queue.snapshot_records()[0]
        assert record.cycle_attempt_count == 1
        assert record.next_retry_timestamp is None
        await exchange.lifecycle.end_session()
        await exchange.lifecycle.start_session("reset-exhausted")
        reset_command = _delivery_event(exchange).data
        assert reset_command.schedule_version > old_command.schedule_version
        assert exchange.fix_app.delivery_queue.get(record.delivery_id).state == (
            FixDeliveryState.DUE
        )
        exchange.fix_app.process_delivery_due(old_command)
        assert delivered == []
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert delivered == [record.payload]
        assert attempts == [record.payload, record.payload]
        assert exchange.fix_app.delivery_queue.get(
            record.delivery_id
        ).cycle_attempt_count == 1
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["external", "realtime"])
async def test_failed_run_keeps_valid_retry_latency_in_both_clock_modes(mode, monkeypatch):
    raw = api_config()
    raw["fix"]["session_participants"] = {"FIX.4.4:MOCKX->CLIENT": "buyer"}
    raw["scheduler"].update(mode=mode, interval_ms=1)
    raw["latency"] = {"inbound_ms": 0, "engine_ms": 0, "outbound_ms": 7}
    exchange = MockExchange(validate_config(raw, environ={}))
    sessions, delivered = await _start(exchange)
    queue = exchange.fix_app.delivery_queue
    queue.retry_interval_ms = 20
    queue.max_attempts_per_cycle = 2
    attempts = []
    complete = asyncio.Event()

    def reconnect(_data):
        exchange.fix_app.retry_pending_for_session(sessions["CLIENT"].toString())

    exchange.scheduler.register_handler("test_fix_reconnect", reconnect)

    def sender(_transport, payload):
        attempts.append((payload, exchange.scheduler.current_time))
        if len(attempts) == 1:
            exchange.scheduler.schedule_event("test_fix_reconnect", None, 5)
            return False
        if len(attempts) == 2:
            raise RuntimeError("reconnected transport still unavailable")
        delivered.append(payload)
        complete.set()
        return True

    exchange.fix_app.transport_sender = sender
    try:
        _saturate_on_report(exchange, monkeypatch)
        exchange.fix_app.fromApp(_new_order("FAILED-RETRY", 1), sessions["CLIENT"])
        if mode == "external":
            await exchange.fix_app.wait_for_ingress()
            start = exchange.scheduler.current_time
            await exchange.lifecycle.advance_to(start + timedelta(milliseconds=32))
        else:
            await asyncio.wait_for(complete.wait(), timeout=2)
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert len(attempts) == 3
        assert [timestamp - payload.timestamp for payload, timestamp in attempts] == [
            timedelta(milliseconds=7),
            timedelta(milliseconds=12),
            timedelta(milliseconds=32),
        ]
        assert all(payload is delivered[0] for payload, _ in attempts)
        assert queue.snapshot().attempts == 3
        assert queue.snapshot().delivered == 1
        assert exchange.metrics.snapshot().average_sim_outbound_latency_ms == 32.0
        assert exchange.scheduler.state.value == "RUNNING"
    finally:
        await exchange.stop_components()

"""Reliable application-level FIX delivery queue regressions."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from src.mockx.delivery import FixDeliverySerializationError
from src.mockx.models import (
    OrderCancelReject,
    ExecutionReport,
    ExecutionType,
    FixDeliveryState,
    OrderStatus,
    SessionIdentity,
)

from .test_fix_ingress_pipeline import (
    _cancel,
    _exchange,
    _new_order,
    _session,
    _start,
    _stop,
)


@pytest.mark.asyncio
async def test_new_report_due_while_disconnected_is_retained_and_retried():
    exchange = _exchange(outbound_ms=5)
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    session_id = session.toString()
    start = exchange.scheduler.current_time
    try:
        exchange.fix_app.fromApp(_new_order("N-DISCONNECTED", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)

        queued = exchange.fix_app.delivery_queue.snapshot_records()
        assert len(queued) == 1
        original = queued[0]
        assert original.state == FixDeliveryState.PENDING
        assert original.simulated_due_timestamp == start + timedelta(
            milliseconds=5
        )
        with pytest.raises(FrozenInstanceError):
            original.attempt_count = 99

        assert exchange.fix_app.unregister_session(session_id)
        await exchange.lifecycle.advance_to(
            start + timedelta(milliseconds=5)
        )

        failed = exchange.fix_app.delivery_queue.get(original.delivery_id)
        assert failed.state == FixDeliveryState.RETRY_PENDING
        assert failed.payload == original.payload
        assert failed.domain_id == original.domain_id
        assert failed.attempt_count == 0
        assert delivered == []

        exchange.fix_app.register_session(
            SessionIdentity(session_id, alias="CLIENT"),
            object(),
        )
        await asyncio.sleep(0)

        retried = exchange.fix_app.delivery_queue.get(original.delivery_id)
        assert retried.state == FixDeliveryState.DELIVERED
        assert retried.payload == original.payload
        assert isinstance(delivered[0], ExecutionReport)
        assert delivered[0].exec_id == original.domain_id
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_partial_and_filled_reports_wait_behind_new_in_session_order():
    exchange = _exchange(outbound_ms=5)
    sessions, delivered = await _start(exchange, "CLIENT", "CLIENT2")
    session = sessions["CLIENT"]
    aggressor_session = sessions["CLIENT2"]
    session_id = session.toString()
    start = exchange.scheduler.current_time
    try:
        exchange.fix_app.fromApp(
            _new_order(
                "PASSIVE",
                1,
                side="2",
                qty=10,
                price=100,
            ),
            session,
        )
        exchange.fix_app.fromApp(
            _new_order("AGGRESSOR", 2, qty=4, price=100),
            aggressor_session,
        )
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)
        assert exchange.fix_app.unregister_session(session_id)

        await exchange.lifecycle.advance_to(
            start + timedelta(milliseconds=5)
        )
        reports = [
            record.payload
            for record in exchange.fix_app.delivery_queue.snapshot_records()
            if isinstance(record.payload, ExecutionReport)
        ]
        assert [report.ord_status for report in reports] == [
            OrderStatus.NEW,
            OrderStatus.PARTIAL,
            OrderStatus.FILLED,
        ]
        assert [report.ord_status for report in delivered] == [
            OrderStatus.FILLED,
        ]

        exchange.fix_app.register_session(
            SessionIdentity(session_id, alias="CLIENT"),
            object(),
        )
        await asyncio.sleep(0)
        assert [report.ord_status for report in delivered] == [
            OrderStatus.FILLED,
            OrderStatus.NEW,
            OrderStatus.PARTIAL,
        ]
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_multiple_reports_for_one_order_keep_fifo_across_fills():
    exchange = _exchange()
    sessions, delivered = await _start(exchange, "CLIENT", "CLIENT2")
    session = sessions["CLIENT"]
    aggressor_session = sessions["CLIENT2"]
    try:
        exchange.fix_app.fromApp(
            _new_order(
                "PASSIVE-MULTI",
                1,
                side="2",
                qty=5,
                price=100,
            ),
            session,
        )
        exchange.fix_app.fromApp(
            _new_order("HIT-1", 2, qty=2, price=100),
            aggressor_session,
        )
        exchange.fix_app.fromApp(
            _new_order("HIT-2", 3, qty=3, price=100),
            aggressor_session,
        )
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        passive = [
            report
            for report in delivered
            if (
                isinstance(report, ExecutionReport)
                and report.cl_ord_id == "PASSIVE-MULTI"
            )
        ]
        assert [report.ord_status for report in passive] == [
            OrderStatus.NEW,
            OrderStatus.PARTIAL,
            OrderStatus.FILLED,
        ]
        assert [report.cum_qty_lots for report in passive] == [0, 2, 5]
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_cancel_success_failure_retains_then_cleans_both_latency_traces():
    exchange = _exchange(outbound_ms=5)
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    session_id = session.toString()
    start = exchange.scheduler.current_time
    try:
        exchange.fix_app.fromApp(_new_order("CANCEL-ME", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(
            start + timedelta(milliseconds=5)
        )
        delivered.clear()

        exchange.fix_app.fromApp(
            _cancel("CANCEL-OK", "CANCEL-ME", 2),
            session,
        )
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        cancel_record = next(
            record
            for record in exchange.fix_app.delivery_queue.snapshot_records()
            if (
                isinstance(record.payload, ExecutionReport)
                and record.payload.exec_type == ExecutionType.CANCELED
            )
        )
        assert exchange.fix_app.unregister_session(session_id)
        await exchange.lifecycle.advance_to(
            exchange.scheduler.current_time + timedelta(milliseconds=5)
        )

        cancel_report = cancel_record.payload
        order_key = (cancel_report.run_generation, cancel_report.order_id)
        cancel_key = (
            cancel_report.run_generation,
            cancel_report.cancel_ingress_sequence,
        )
        assert order_key in exchange.latency_tracker.orders
        assert cancel_key in exchange.latency_tracker.cancels
        assert (
            exchange.fix_app.delivery_queue.get(
                cancel_record.delivery_id
            ).state
            == FixDeliveryState.RETRY_PENDING
        )

        exchange.fix_app.register_session(
            SessionIdentity(session_id, alias="CLIENT"),
            object(),
        )
        await asyncio.sleep(0)
        assert order_key not in exchange.latency_tracker.orders
        assert cancel_key not in exchange.latency_tracker.cancels
        assert delivered == [cancel_report]
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_cancel_reject_due_disconnected_preserves_reject_identity():
    exchange = _exchange(outbound_ms=5)
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    session_id = session.toString()
    start = exchange.scheduler.current_time
    try:
        exchange.fix_app.fromApp(
            _cancel("C-UNKNOWN", "MISSING", 1),
            session,
        )
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)
        record = exchange.fix_app.delivery_queue.snapshot_records()[0]
        assert isinstance(record.payload, OrderCancelReject)
        assert record.domain_id == record.payload.reject_id

        assert exchange.fix_app.unregister_session(session_id)
        await exchange.lifecycle.advance_to(
            start + timedelta(milliseconds=5)
        )
        failed = exchange.fix_app.delivery_queue.get(record.delivery_id)
        assert failed.state == FixDeliveryState.RETRY_PENDING
        assert failed.payload.reject_id == record.payload.reject_id

        exchange.fix_app.register_session(
            SessionIdentity(session_id, alias="CLIENT"),
            object(),
        )
        await asyncio.sleep(0)
        assert delivered == [record.payload]
        assert exchange.metrics.snapshot().cancel_rejects == 1
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_blocked_fix_session_does_not_block_another_session():
    exchange = _exchange()
    sessions, delivered = await _start(
        exchange,
        "CLIENT-A",
        "CLIENT-B",
    )
    try:
        exchange.fix_app.fromApp(
            _new_order("ORDER-A", 1, price=99),
            sessions["CLIENT-A"],
        )
        exchange.fix_app.fromApp(
            _new_order("ORDER-B", 1, price=98),
            sessions["CLIENT-B"],
        )
        await exchange.fix_app.wait_for_ingress()
        assert exchange.fix_app.unregister_session(
            sessions["CLIENT-A"].toString()
        )
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        assert [report.cl_ord_id for report in delivered] == ["ORDER-B"]
        records = exchange.fix_app.delivery_queue.snapshot_records()
        states = {
            record.payload.cl_ord_id: record.state
            for record in records
            if isinstance(record.payload, ExecutionReport)
        }
        assert states == {
            "ORDER-A": FixDeliveryState.RETRY_PENDING,
            "ORDER-B": FixDeliveryState.DELIVERED,
        }
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_transport_failures_retry_three_times_without_logical_duplicates():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    attempts = []

    def flaky_sender(_transport, report):
        attempts.append(report)
        if len(attempts) < 3:
            raise RuntimeError("temporary failure")
        delivered.append(report)

    exchange.fix_app.transport_sender = flaky_sender
    start = exchange.scheduler.current_time
    try:
        exchange.fix_app.fromApp(_new_order("RETRY-THREE", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(
            start + timedelta(milliseconds=2_000)
        )

        queue_stats = exchange.fix_app.delivery_queue.snapshot()
        assert len(attempts) == 3
        assert attempts[0] == attempts[1] == attempts[2] == delivered[0]
        assert queue_stats.attempts == 3
        assert queue_stats.failures == 2
        assert queue_stats.delivered == 1
        assert exchange.metrics.snapshot().execution_reports == 1
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_false_transport_result_is_retained_and_retried():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    calls = 0

    def false_then_success(_transport, report):
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        delivered.append(report)
        return True

    exchange.fix_app.transport_sender = false_then_success
    start = exchange.scheduler.current_time
    try:
        exchange.fix_app.fromApp(_new_order("FALSE-RESULT", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(
            start + timedelta(milliseconds=1_000)
        )

        record = exchange.fix_app.delivery_queue.snapshot_records()[0]
        assert calls == 2
        assert delivered == [record.payload]
        assert record.state == FixDeliveryState.DELIVERED
        assert record.attempt_count == 2
        assert exchange.fix_app.delivery_queue.snapshot().failures == 1
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_permanent_serialization_failure_is_visible_and_blocks_fifo():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]

    def fail_serialization(_transport, _report):
        raise FixDeliverySerializationError("invalid immutable mapping")

    exchange.fix_app.transport_sender = fail_serialization
    try:
        exchange.fix_app.fromApp(_new_order("BAD-SERIALIZE", 1), session)
        exchange.fix_app.fromApp(_new_order("BLOCKED-LATER", 2), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        records = exchange.fix_app.delivery_queue.snapshot_records()
        assert records[0].state == FixDeliveryState.PERMANENTLY_FAILED
        assert records[1].state == FixDeliveryState.DUE
        assert exchange.fix_app.delivery_queue.snapshot().permanently_failed == 1
        assert exchange.scheduler.state.value == "RUNNING"
        assert delivered == []
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_session_reset_preserves_old_generation_ahead_of_new_reports():
    exchange = _exchange(outbound_ms=10)
    await exchange.start_components()
    delivered = []
    exchange.fix_app.transport_sender = (
        lambda _transport, report: delivered.append(report)
    )
    session = _session()
    session_id = session.toString()
    exchange.fix_app.register_session(
        SessionIdentity(session_id, alias="CLIENT"),
        object(),
    )
    try:
        await exchange.lifecycle.start_session("old-run")
        exchange.fix_app.fromApp(_new_order("OLD", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        ended = await exchange.lifecycle.end_session()
        assert not hasattr(ended.final_stats, "fix_delivery")
        assert exchange.fix_app.delivery_queue.snapshot().queue_depth == 1

        await exchange.lifecycle.start_session("new-run")
        exchange.fix_app.fromApp(_new_order("NEW", 2), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(
            exchange.scheduler.current_time + timedelta(milliseconds=10)
        )

        assert [report.cl_ord_id for report in delivered] == ["OLD", "NEW"]
        assert [report.run_generation for report in delivered] == [1, 2]
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_delivery_backlog_degrades_readiness_but_stats_remain_available():
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    session = sessions["CLIENT"]
    try:
        assert exchange.fix_app.unregister_session(session.toString())
        exchange.fix_app.fromApp(_new_order("OBSERVE-BACKLOG", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        # Exercise the enabled-FIX readiness branch without starting a real
        # network acceptor in this application-level delivery test.
        exchange.fix_enabled = True
        exchange.app.state.component_status["fix_gateway"] = "ready"
        async with AsyncClient(
            transport=ASGITransport(app=exchange.app),
            base_url="http://test",
        ) as client:
            readiness = await client.get("/readyz")
            stats = await client.get("/stats")
            prometheus = await client.get("/metrics")

        assert readiness.status_code == 200
        assert readiness.json()["status"] == "degraded"
        assert readiness.json()["components"]["fix_delivery"] == "degraded"
        assert stats.status_code == 200
        delivery_stats = stats.json()["fix_delivery"]
        assert delivery_stats["queue_depth"] == 1
        assert delivery_stats["retry_pending"] == 1
        assert delivery_stats["failures"] == 1
        assert "mockx_fix_delivery_queue_depth 1.0" in prometheus.text
        assert "mockx_fix_delivery_failures_total 1.0" in prometheus.text
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_repeated_reconnect_does_not_redeliver_a_delivered_record():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    session_id = session.toString()
    try:
        exchange.fix_app.fromApp(_new_order("N-ONCE", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        assert len(delivered) == 1
        assert exchange.fix_app.unregister_session(session_id)
        for _ in range(2):
            exchange.fix_app.register_session(
                SessionIdentity(session_id, alias="CLIENT"),
                object(),
            )
            await asyncio.sleep(0)
            assert exchange.fix_app.unregister_session(session_id)

        assert len(delivered) == 1
        assert (
            exchange.fix_app.delivery_queue.snapshot().delivered
            == 1
        )
    finally:
        await _stop(exchange)

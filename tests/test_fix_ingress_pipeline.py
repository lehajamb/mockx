"""Deterministic FIX ingress and scheduled-cancel lifecycle regressions."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from src.mockx.gateway import fix
from src.mockx.configuration import validate_config
from src.mockx.main import MockExchange
from src.mockx.models import (
    CancelReject,
    CancelRejectReason,
    ExecutionReport,
    ExecutionType,
    OrderStatus,
    SessionIdentity,
)

from .api_helpers import api_config


def _exchange(
    *,
    inbound_ms: int = 0,
    engine_ms: int = 0,
    outbound_ms: int = 0,
    session_delay_ms: int = 0,
) -> MockExchange:
    config = api_config()
    config["latency"] = {
        "inbound_ms": inbound_ms,
        "outbound_ms": outbound_ms,
        "engine_ms": engine_ms,
        "session_delays": {"CLIENT": session_delay_ms},
    }
    config["fix"]["session_participants"] = {
        "FIX.4.4:MOCKX->CLIENT": "participant-client",
        "FIX.4.4:MOCKX->CLIENT2": "participant-client-2",
        "FIX.4.4:MOCKX->CLIENT-A": "participant-client-a",
        "FIX.4.4:MOCKX->CLIENT-B": "participant-client-b",
    }
    return MockExchange(validate_config(config, environ={}))


def _session(client: str = "CLIENT"):
    return fix.SessionID("FIX.4.4", "MOCKX", client)


def _new_order(
    cl_ord_id: str,
    msg_seq_num: int,
    *,
    side: str = fix.Side_BUY,
    qty: float = 5.0,
    price: float = 100.0,
):
    message = fix.Message()
    message.getHeader().setField(fix.MsgType(fix.MsgType_NewOrderSingle))
    message.getHeader().setField(fix.MsgSeqNum(msg_seq_num))
    message.setField(fix.ClOrdID(cl_ord_id))
    message.setField(fix.Symbol("TEST"))
    message.setField(fix.Side(side))
    message.setField(fix.OrderQty(qty))
    message.setField(fix.OrdType(fix.OrdType_LIMIT))
    message.setField(fix.Price(price))
    message.setField(fix.TimeInForce(fix.TimeInForce_GOOD_TILL_CANCEL))
    return message


def _cancel(
    cl_ord_id: str,
    orig_cl_ord_id: str,
    msg_seq_num: int,
    *,
    side: str = fix.Side_BUY,
    qty: float = 5.0,
):
    message = fix.Message()
    message.getHeader().setField(fix.MsgType(fix.MsgType_OrderCancelRequest))
    message.getHeader().setField(fix.MsgSeqNum(msg_seq_num))
    message.setField(fix.ClOrdID(cl_ord_id))
    message.setField(fix.OrigClOrdID(orig_cl_ord_id))
    message.setField(fix.Symbol("TEST"))
    message.setField(fix.Side(side))
    message.setField(fix.OrderQty(qty))
    return message


async def _start(exchange: MockExchange, *clients: str):
    await exchange.start_components()
    delivered = []
    exchange.fix_app.transport_sender = (
        lambda _transport, report: delivered.append(report)
    )
    sessions = {}
    for client in clients or ("CLIENT",):
        session = _session(client)
        identity = SessionIdentity(
            session.toString(),
            sender_comp_id="MOCKX",
            target_comp_id=client,
            alias=client,
        )
        exchange.fix_app.register_session(identity, object())
        sessions[client] = session
    await exchange.lifecycle.start_session("pr5")
    return sessions, delivered


async def _stop(exchange: MockExchange):
    await exchange.lifecycle.end_session()
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_zero_latency_new_then_cancel_uses_scheduled_pipeline():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    try:
        exchange.fix_app.fromApp(_new_order("N-1", 1), session)
        exchange.fix_app.fromApp(_cancel("C-1", "N-1", 2), session)

        barrier = await exchange.fix_app.wait_for_ingress()
        assert barrier.run_generation == 1
        assert barrier.through_sequence == 1
        envelope = next(
            event.data
            for event in exchange.scheduler.event_queue
            if event.event_type == "new_order_received"
        )
        with pytest.raises(FrozenInstanceError):
            envelope.cl_ord_id = "changed"

        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        order = next(iter(exchange.engine.orders.values()))
        assert order.status == OrderStatus.CANCELLED
        assert [
            report.exec_type
            for report in delivered
            if isinstance(report, ExecutionReport)
        ] == [ExecutionType.NEW, ExecutionType.CANCELED]
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_nonzero_cancel_stages_apply_all_configured_latency():
    exchange = _exchange(
        inbound_ms=5,
        engine_ms=3,
        outbound_ms=7,
        session_delay_ms=4,
    )
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    start = exchange.scheduler.current_time
    try:
        exchange.fix_app.fromApp(_new_order("N-LAT", 1), session)
        exchange.fix_app.fromApp(_cancel("C-LAT", "N-LAT", 2), session)
        await exchange.fix_app.wait_for_ingress()

        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=8))
        assert delivered == []
        assert len(exchange.engine.orders) == 1
        assert next(iter(exchange.engine.orders.values())).status == (
            OrderStatus.PENDING_NEW
        )

        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=9))
        assert len(exchange.engine.orders) == 1
        assert next(iter(exchange.engine.orders.values())).status == (
            OrderStatus.PENDING_NEW
        )

        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=12))
        assert next(iter(exchange.engine.orders.values())).status == (
            OrderStatus.CANCELLED
        )
        assert delivered == []

        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=19))
        assert [
            report.exec_type
            for report in delivered
            if isinstance(report, ExecutionReport)
        ] == [ExecutionType.NEW, ExecutionType.CANCELED]
        snapshot = exchange.metrics.snapshot()
        assert (
            snapshot.average_sim_cancel_inbound_latency_ms,
            snapshot.average_sim_cancel_engine_latency_ms,
            snapshot.average_sim_cancel_outbound_latency_ms,
            snapshot.average_sim_cancel_end_to_end_latency_ms,
        ) == (9.0, 3.0, 7.0, 19.0)
        cancel_report = next(
            report
            for report in delivered
            if isinstance(report, ExecutionReport)
            and report.exec_type == ExecutionType.CANCELED
        )
        correlated = [
            sample
            for sample in exchange.latency_tracker.measurements
            if sample.report_exec_id == cancel_report.exec_id
        ]
        assert len(correlated) == 3
        assert all(
            "sim_cancel_" in sample.metric
            for sample in correlated
        )
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_msg_seq_num_beats_callback_arrival_for_same_session():
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    session = sessions["CLIENT"]
    try:
        exchange.fix_app.fromApp(_new_order("SECOND", 2, price=99), session)
        exchange.fix_app.fromApp(_new_order("FIRST", 1, price=100), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        assert [
            exchange.engine.orders[order_id].cl_ord_id
            for order_id in sorted(exchange.engine.orders)
        ] == ["FIRST", "SECOND"]
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_session_identity_beats_callback_arrival_at_equal_time():
    exchange = _exchange()
    sessions, _ = await _start(exchange, "CLIENT-A", "CLIENT-B")
    try:
        exchange.fix_app.fromApp(
            _new_order("B", 1, price=99),
            sessions["CLIENT-B"],
        )
        exchange.fix_app.fromApp(
            _new_order("A", 1, price=100),
            sessions["CLIENT-A"],
        )
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        assert [
            exchange.engine.orders[order_id].cl_ord_id
            for order_id in sorted(exchange.engine.orders)
        ] == ["A", "B"]
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_duplicate_cancel_cl_ord_id_precedes_terminal_state():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    try:
        exchange.fix_app.fromApp(_new_order("N-DUP", 1), session)
        exchange.fix_app.fromApp(_cancel("C-DUP", "N-DUP", 2), session)
        exchange.fix_app.fromApp(_cancel("C-DUP", "N-DUP", 3), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        rejects = [
            report for report in delivered if isinstance(report, CancelReject)
        ]
        assert len(rejects) == 1
        assert rejects[0].reason == CancelRejectReason.INVALID_REQUEST
        assert "Duplicate" in rejects[0].text
        with pytest.raises(FrozenInstanceError):
            rejects[0].text = "changed"
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_already_queued_fill_legitimately_beats_later_cancel():
    exchange = _exchange()
    sessions, delivered = await _start(exchange, "CLIENT", "CLIENT2")
    session = sessions["CLIENT"]
    aggressor_session = sessions["CLIENT2"]
    try:
        exchange.fix_app.fromApp(
            _new_order(
                "PASSIVE",
                1,
                side=fix.Side_SELL,
                qty=5,
                price=100,
            ),
            session,
        )
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        exchange.fix_app.fromApp(
            _new_order("AGGRESSOR", 2, qty=5, price=100),
            aggressor_session,
        )
        await exchange.fix_app.wait_for_ingress()
        exchange.fix_app.fromApp(
            _cancel(
                "C-TOO-LATE",
                "PASSIVE",
                3,
                side=fix.Side_SELL,
                qty=5,
            ),
            session,
        )
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        passive = next(
            order
            for order in exchange.engine.orders.values()
            if order.cl_ord_id == "PASSIVE"
        )
        assert passive.status == OrderStatus.FILLED
        reject = next(
            report
            for report in delivered
            if isinstance(report, CancelReject)
            and report.cancel_cl_ord_id == "C-TOO-LATE"
        )
        assert reject.reason == CancelRejectReason.TOO_LATE_TO_CANCEL
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_cancel_reject_uses_all_four_cancel_latency_series():
    exchange = _exchange(
        inbound_ms=5,
        engine_ms=3,
        outbound_ms=7,
        session_delay_ms=4,
    )
    sessions, delivered = await _start(exchange)
    start = exchange.scheduler.current_time
    try:
        exchange.fix_app.fromApp(
            _cancel("C-UNKNOWN", "MISSING", 1),
            sessions["CLIENT"],
        )
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(
            start + timedelta(milliseconds=19)
        )

        assert len(delivered) == 1
        assert isinstance(delivered[0], CancelReject)
        snapshot = exchange.metrics.snapshot()
        assert (
            snapshot.average_sim_cancel_inbound_latency_ms,
            snapshot.average_sim_cancel_engine_latency_ms,
            snapshot.average_sim_cancel_outbound_latency_ms,
            snapshot.average_sim_cancel_end_to_end_latency_ms,
        ) == (9.0, 3.0, 7.0, 19.0)
        samples = [
            sample
            for sample in exchange.latency_tracker.measurements
            if "sim_cancel_" in sample.metric
        ]
        assert len(samples) == 4
        assert all(sample.run_generation == 1 for sample in samples)
        assert all(sample.ingress_sequence == 0 for sample in samples)
        assert all(sample.cl_ord_id == "C-UNKNOWN" for sample in samples)
        assert all(sample.orig_cl_ord_id == "MISSING" for sample in samples)
        assert all(sample.order_id is None for sample in samples)
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_cancel_delivery_failure_is_retained_for_bounded_retry():
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    attempts = []

    def fail_delivery(_transport, report):
        attempts.append(report)
        raise RuntimeError("disconnected")

    exchange.fix_app.transport_sender = fail_delivery
    try:
        exchange.fix_app.fromApp(
            _cancel("C-FAIL", "MISSING", 1),
            sessions["CLIENT"],
        )
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        assert len(attempts) == 1
        assert exchange.scheduler.get_pending_count() == 1
        assert exchange.metrics.snapshot().report_delivery_failures == 1
        cancel_metrics = {
            sample.metric
            for sample in exchange.latency_tracker.measurements
            if "sim_cancel_" in sample.metric
        }
        assert cancel_metrics == {
            "mockx_sim_cancel_inbound_latency_seconds",
            "mockx_sim_cancel_engine_latency_seconds",
        }
        assert exchange.latency_tracker.cancels
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_explicit_barrier_excludes_commands_beyond_captured_high_water():
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    session = sessions["CLIENT"]
    try:
        exchange.fix_app.fromApp(_new_order("FIRST-BARRIER", 1), session)
        captured = exchange.fix_app.ingress_high_water_mark()
        exchange.fix_app.fromApp(_new_order("SECOND-BARRIER", 2), session)

        reached = await exchange.fix_app.wait_for_ingress(
            through_sequence=captured.through_sequence,
        )
        assert reached == captured
        assert [
            event.data.cl_ord_id
            for event in exchange.scheduler.event_queue
            if event.event_type == "new_order_received"
        ] == ["FIRST-BARRIER"]

        async with exchange.lifecycle.lock:
            await exchange.scheduler.run_until(
                exchange.scheduler.current_time
            )
        assert {
            order.cl_ord_id for order in exchange.engine.orders.values()
        } == {"FIRST-BARRIER"}

        final_barrier = await exchange.fix_app.wait_for_ingress()
        assert final_barrier.through_sequence == 1
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert {
            order.cl_ord_id for order in exchange.engine.orders.values()
        } == {"FIRST-BARRIER", "SECOND-BARRIER"}
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_fresh_generations_repeat_ingress_and_scheduler_priorities():
    exchange = _exchange()
    await exchange.start_components()
    delivered = []
    exchange.fix_app.transport_sender = (
        lambda _transport, report: delivered.append(report)
    )
    sessions = {}
    for client in ("CLIENT-A", "CLIENT-B"):
        session = _session(client)
        exchange.fix_app.register_session(
            SessionIdentity(
                session.toString(),
                sender_comp_id="MOCKX",
                target_comp_id=client,
                alias=client,
            ),
            object(),
        )
        sessions[client] = session

    observed = []
    try:
        for generation in (1, 2):
            await exchange.lifecycle.start_session(f"repeat-{generation}")
            delivered.clear()
            exchange.fix_app.fromApp(
                _new_order(f"G{generation}-B", 1, price=99),
                sessions["CLIENT-B"],
            )
            exchange.fix_app.fromApp(
                _new_order(f"G{generation}-A", 1, price=100),
                sessions["CLIENT-A"],
            )
            barrier = await exchange.fix_app.wait_for_ingress()
            receives = sorted(
                (
                    event
                    for event in exchange.scheduler.event_queue
                    if event.event_type == "new_order_received"
                ),
                key=lambda event: event.sequence,
            )
            await exchange.lifecycle.advance_to(
                exchange.scheduler.current_time
            )
            observed.append(
                (
                    barrier.through_sequence,
                    tuple(
                        (
                            event.data.session.session_id,
                            event.data.msg_seq_num,
                            event.data.ingress_sequence,
                            event.sequence,
                        )
                        for event in receives
                    ),
                    tuple(
                        order.price_ticks
                        for _, order in sorted(
                            exchange.engine.orders.items()
                        )
                    ),
                    tuple(
                        report.exec_type
                        for report in delivered
                        if isinstance(report, ExecutionReport)
                    ),
                )
            )
            assert barrier.run_generation == generation
            await exchange.lifecycle.end_session()

        assert observed[0] == observed[1]
    finally:
        await exchange.stop_components()

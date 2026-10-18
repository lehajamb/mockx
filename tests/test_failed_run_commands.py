"""Pending command settlement when persisted run metrics become incomplete."""

import asyncio
from datetime import timedelta

import pytest

from src.mockx.configuration import validate_config
from src.mockx.fix_fields import cancel_reject_fields, execution_report_fields
from src.mockx.gateway import fix
from src.mockx.main import MockExchange
from src.mockx.lifecycle import LifecycleIngressRejected
from src.mockx.metrics import BufferedMetricsWriter, InMemoryMetricsSink
from src.mockx.models import (
    CancelOrderCommand,
    OrderCancelReject,
    ExecutionReport,
    ExecutionType,
    FixDeliveryDueCommand,
    OrderStatus,
    ProcessOrderCommand,
    Side,
)
from tests.lifecycle_helpers import make_engine, submit
from tests.api_helpers import api_config
from tests.test_fix_ingress_pipeline import _cancel, _exchange, _new_order, _start, _stop


FAILURE = "METRICS_INCOMPLETE: Metrics were lost; this simulation must be rerun."


def test_extract_mutation_events_preserves_delivery_order_and_queue_depth():
    engine, _, _, _ = make_engine()
    scheduler = engine.scheduler
    depths = []
    scheduler.on_queue_depth_change = depths.append
    due_at = scheduler.current_time + timedelta(milliseconds=5)
    first_delivery = FixDeliveryDueCommand("first", 1, due_at)
    second_delivery = FixDeliveryDueCommand("second", 2, due_at)
    scheduler.schedule_event("order_process", "later", 8)
    scheduler.schedule_event("fix_delivery_due", first_delivery, 5)
    scheduler.schedule_event("order_process", "earlier", 2)
    scheduler.schedule_event("fix_delivery_due", second_delivery, 5)
    removed = scheduler.extract_events({"order_process"})
    assert [event.data for event in removed] == ["earlier", "later"]
    assert [event.data for event in sorted(scheduler.event_queue)] == [
        first_delivery, second_delivery,
    ]
    assert depths[-1] == 2


def test_pending_order_rejection_is_idempotent_and_does_not_touch_resting_orders():
    engine, reports, trades, _ = make_engine()
    pending = submit(engine, cl_ord_id="pending", participant_id="a", side=Side.BUY,
                     qty=3, price=99, process=False)
    resting = submit(engine, cl_ord_id="resting", participant_id="b", side=Side.SELL,
                     qty=5, price=100)
    command = ProcessOrderCommand(pending, expected_version=0)
    assert engine.reject_pending_order(command, FAILURE)
    assert not engine.reject_pending_order(command, FAILURE)
    assert not engine.reject_pending_order(ProcessOrderCommand(resting, 0), FAILURE)
    engine.process_order(command)
    assert engine.orders[pending].status == OrderStatus.REJECTED
    assert engine.orders[pending].canceled_qty_lots == 3
    assert engine.orders[resting].status == OrderStatus.NEW
    assert engine.books["TEST"].is_resting(resting)
    assert [r.exec_type for r in reports if r.order_id == pending] == [ExecutionType.REJECTED]
    assert trades == []
    engine.books["TEST"].assert_invariants()


@pytest.mark.parametrize("processed", [False, True])
def test_unavailable_cancel_reject_preserves_target_and_serializes_reason_99(processed):
    engine, reports, _, _ = make_engine()
    order_id = submit(engine, cl_ord_id="resting", participant_id="a", side=Side.BUY,
                      qty=5, price=99, process=processed)
    order = engine.orders[order_id]
    outcomes = []
    rejects = []
    engine.on_cancel_outcome = lambda command, outcome: outcomes.append(outcome)
    engine.on_cancel_reject = rejects.append
    command = CancelOrderCommand(
        cancel_cl_ord_id="cancel", orig_cl_ord_id="resting", symbol="TEST",
        side=Side.BUY, order_qty_lots=5, requested_at=engine.scheduler.current_time,
        session_id=order.session_id, run_generation=engine.identifier_scope.generation,
    )
    rejection = engine.reject_cancel_unavailable(command, FAILURE)
    assert rejection.reason.value == "EXCHANGE_UNAVAILABLE"
    assert rejection.order_id == order_id
    expected_status = OrderStatus.NEW if processed else OrderStatus.PENDING_NEW
    assert rejection.ord_status == expected_status
    assert cancel_reject_fields(rejection)["CxlRejReason"] == 99
    assert cancel_reject_fields(rejection)["OrdStatus"] == ("0" if processed else "A")
    assert [outcome.value for outcome in outcomes] == ["exchange_unavailable"]
    assert rejects == [rejection]
    assert order.status == expected_status
    assert order.leaves_qty_lots == 5
    assert len(reports) == int(processed)
    engine.books["TEST"].assert_invariants()


@pytest.mark.asyncio
async def test_fail_pending_commands_settles_each_staged_order_and_cancel_once():
    exchange = _exchange(inbound_ms=5, engine_ms=3, outbound_ms=7)
    sessions, delivered = await _start(exchange)
    try:
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("pending", 1), session)
        exchange.fix_app.fromApp(_cancel("cancel", "pending", 2), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert {event.event_type for event in exchange.scheduler.event_queue} == {
            "order_arrived", "order_process", "cancel_arrived_at_exchange", "cancel_engine_process"
        }
        exchange.fix_app.fail_pending_commands(FAILURE)
        exchange.fix_app.fail_pending_commands(FAILURE)
        assert delivered == []
        assert {event.event_type for event in exchange.scheduler.event_queue} == {"fix_delivery_due"}
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time + timedelta(milliseconds=7))
        assert len(delivered) == 2
        assert isinstance(delivered[0], ExecutionReport)
        assert delivered[0].exec_type == ExecutionType.REJECTED
        assert isinstance(delivered[1], OrderCancelReject)
        assert delivered[1].reason.value == "EXCHANGE_UNAVAILABLE"
        assert next(iter(exchange.engine.orders.values())).status == OrderStatus.REJECTED
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_loss_during_detached_ingress_batch_rejects_every_request_once():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        writer = BufferedMetricsWriter(
            InMemoryMetricsSink(), flush_batch_size=1,
            max_buffer_records=1, max_buffer_bytes=100_000,
        )
        exchange.metrics.attach_persistence(writer)
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("before-loss", 1), session)
        exchange.fix_app.fromApp(_new_order("at-loss", 2), session)
        exchange.fix_app.fromApp(_cancel("after-loss", "before-loss", 3), session)
        await exchange.fix_app.wait_for_ingress()
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert exchange.engine.orders == {}
        assert exchange.fix_app._ingress_receipts == {}
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert len(delivered) == 3
        assert [r.cl_ord_id for r in delivered if isinstance(r, ExecutionReport)] == [
            "before-loss", "at-loss"
        ]
        assert [r.cl_ord_id for r in delivered if isinstance(r, OrderCancelReject)] == [
            "after-loss"
        ]
        assert all(
            r.exec_type == ExecutionType.REJECTED
            for r in delivered if isinstance(r, ExecutionReport)
        )
        assert exchange.scheduler.event_queue == []
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_fail_pending_commands_rejects_enqueued_but_unscheduled_ingress():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("unsubmitted", 1), session)
        exchange.fix_app.fromApp(_cancel("cancel", "unsubmitted", 2), session)
        barrier = await exchange.fix_app.await_ingress_enqueued()
        assert barrier.through_sequence == 1
        exchange.fix_app.fail_pending_commands(FAILURE)
        exchange.fix_app.fail_pending_commands(FAILURE)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert exchange.engine.orders == {}
        assert len(delivered) == 2
        assert delivered[0].cl_ord_id == "unsubmitted"
        assert delivered[1].cl_ord_id == "cancel"
        assert exchange.fix_app._pending_ingress == []
        assert exchange.fix_app._ingress_receipts == {}
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_failed_ingress_rejection_uses_the_orders_instrument():
    config = api_config()
    config["instruments"]["OTHER"] = {
        "tick_size": "0.25", "lot_size": "0.1",
        "price_display_precision": 2, "quantity_display_precision": 1,
    }
    config["fix"]["session_participants"] = {
        "FIX.4.4:MOCKX->CLIENT": "participant-client"
    }
    exchange = MockExchange(validate_config(config, environ={}))
    sessions, delivered = await _start(exchange)
    try:
        message = _new_order("other", 1, qty=1.5, price=10.25)
        message.setField(fix.Symbol("OTHER"))
        exchange.fix_app.fromApp(message, sessions["CLIENT"])
        await exchange.fix_app.await_ingress_enqueued()
        exchange.fix_app.fail_pending_commands(FAILURE)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert len(delivered) == 1
        fields = execution_report_fields(delivered[0])
        assert fields["Symbol"] == "OTHER"
        assert fields["OrderQty"] == "1.5"
        assert fields["Price"] == "10.25"
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier_already_waiting", [False, True])
async def test_uncommitted_ingress_rejected_at_failure_does_not_block_report_drain(
    barrier_already_waiting, monkeypatch
):
    exchange = _exchange(outbound_ms=10)
    sessions, delivered = await _start(exchange)
    rejected = []
    monkeypatch.setattr(
        exchange.fix_app, "send_business_reject",
        lambda *args, **kwargs: rejected.append((args, kwargs)),
    )
    barrier = None
    try:
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("committed", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert delivered == []
        writer = BufferedMetricsWriter(
            InMemoryMetricsSink(), flush_batch_size=1,
            max_buffer_records=1, max_buffer_bytes=100_000,
        )
        exchange.metrics.attach_persistence(writer)
        async with exchange.lifecycle.lock:
            exchange.fix_app.fromApp(_new_order("uncommitted", 2), session)
            if barrier_already_waiting:
                barrier = asyncio.create_task(exchange.fix_app.await_ingress_enqueued())
                await asyncio.sleep(0)
                assert not barrier.done()
            exchange.metrics.record_fix_message("D", "received", 0)
            exchange.metrics.record_fix_message("D", "received", 0)
            assert exchange.lifecycle.check_persistence_failure()
        # Let the scheduled enqueue acquire the lock and reject its stale token.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if barrier is not None:
            await barrier
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time + timedelta(milliseconds=10))
        assert len(rejected) == 1
        assert "METRICS_INCOMPLETE" in str(rejected[0])
        assert exchange.fix_app._ingress_receipts == {}
        assert len(delivered) == 1
        assert delivered[0].cl_ord_id == "committed"
        assert [order.cl_ord_id for order in exchange.engine.orders.values()] == ["committed"]
        assert exchange.lifecycle.state.value == "RUN_FAILED"
    finally:
        if barrier is not None and not barrier.done():
            barrier.cancel()
            await asyncio.gather(barrier, return_exceptions=True)
        await _stop(exchange)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["enqueue", "rejection_send"])
async def test_unexpected_ingress_failure_remains_visible_to_barriers(failure_site, monkeypatch):
    exchange = _exchange()
    sessions, _ = await _start(exchange)

    async def fail_enqueue(token, action):
        if failure_site == "rejection_send":
            raise LifecycleIngressRejected("METRICS_INCOMPLETE")
        raise RuntimeError("unexpected ingress failure")

    def fail_rejection(*args, **kwargs):
        raise RuntimeError("unexpected rejection send failure")

    monkeypatch.setattr(exchange.lifecycle, "execute_ingress", fail_enqueue)
    monkeypatch.setattr(exchange.fix_app, "send_business_reject", fail_rejection)
    try:
        exchange.fix_app.fromApp(_new_order("uncommitted", 1), sessions["CLIENT"])
        with pytest.raises(RuntimeError, match="unexpected"):
            await exchange.fix_app.await_ingress_enqueued()
        assert len(exchange.fix_app._ingress_receipts) == 1
        assert exchange.engine.orders == {}
    finally:
        await _stop(exchange)

"""Engine cancellation and immutable cancel-reject regression tests."""

from dataclasses import FrozenInstanceError

import pytest

from src.mockx.models import (
    CancelOrderCommand,
    CancelRejectReason,
    ExecutionType,
    OrderStatus,
    ProcessOrderCommand,
    Side,
)
from tests.lifecycle_helpers import NOW, cancel, make_engine, submit


def successful_cancels(reports, order_id):
    return [
        report
        for report in reports
        if (
            report.order_id == order_id
            and report.exec_type == ExecutionType.CANCELED
        )
    ]


def test_cancel_resting_unfilled_order_removes_live_quantity_once():
    engine, reports, _, _ = make_engine()
    order_id = submit(
        engine,
        cl_ord_id="CANCEL-NEW",
        side=Side.BUY,
        qty=10,
        price=99,
    )

    assert cancel(
        engine,
        order_id,
        cancel_cl_ord_id="CXL-CANCEL-NEW",
    ) is None

    order = engine.orders[order_id]
    cancel_reports = successful_cancels(reports, order_id)
    assert order.status == OrderStatus.CANCELLED
    assert order.filled_qty == 0
    assert order.remaining_qty == 0
    assert order.canceled_qty == 10
    assert not engine.books["TEST"].is_resting(order_id)
    assert len(cancel_reports) == 1
    assert cancel_reports[0].cl_ord_id == "CXL-CANCEL-NEW"
    assert cancel_reports[0].orig_cl_ord_id == "CANCEL-NEW"
    assert cancel_reports[0].cum_qty == 0
    assert cancel_reports[0].leaves_qty == 0
    engine.books["TEST"].assert_invariants()


def test_cancel_partially_filled_resting_order_preserves_fills():
    engine, reports, _, _ = make_engine()
    resting_id = submit(
        engine,
        cl_ord_id="CANCEL-PARTIAL",
        side=Side.SELL,
        qty=10,
        price=100,
    )
    submit(
        engine,
        cl_ord_id="CANCEL-PARTIAL-HIT",
        side=Side.BUY,
        qty=4,
        price=100,
    )
    before_avg = engine.orders[resting_id].avg_fill_price

    assert cancel(engine, resting_id) is None

    order = engine.orders[resting_id]
    cancel_report = successful_cancels(reports, resting_id)[0]
    assert order.status == OrderStatus.CANCELLED
    assert order.filled_qty == 4
    assert order.avg_fill_price == before_avg == 100
    assert order.canceled_qty == 6
    assert order.remaining_qty == 0
    assert cancel_report.cum_qty == 4
    assert cancel_report.avg_px == 100
    assert cancel_report.leaves_qty == 0
    assert cancel_report.canceled_qty == 6
    assert not engine.books["TEST"].is_resting(resting_id)
    engine.books["TEST"].assert_invariants()


@pytest.mark.asyncio
async def test_cancel_before_processing_invalidates_scheduled_command():
    engine, reports, trades, _ = make_engine(
        network_latency_ms=20,
        engine_latency_ms=10,
    )
    order_id = submit(
        engine,
        cl_ord_id="CANCEL-PENDING",
        side=Side.BUY,
        qty=5,
        price=100,
        process=False,
    )

    assert cancel(engine, order_id) is None
    await engine.scheduler.run_until_idle()

    order = engine.orders[order_id]
    own_reports = [
        report for report in reports if report.order_id == order_id
    ]
    assert order.status == OrderStatus.CANCELLED
    assert order.version == 1
    assert order.processed is False
    assert trades == []
    assert not engine.books["TEST"].is_resting(order_id)
    assert [report.exec_type for report in own_reports] == [
        ExecutionType.CANCELED
    ]


def test_unknown_cancel_returns_immutable_routable_rejection():
    engine, reports, trades, _ = make_engine()
    command = CancelOrderCommand(
        cancel_cl_ord_id="CXL-UNKNOWN",
        orig_cl_ord_id="UNKNOWN",
        symbol="TEST",
        side=Side.BUY,
        qty=1,
        requested_at=NOW,
        session_id="FIX.4.4:CLIENT->MOCKX",
        session_alias="CLIENT",
    )

    rejection = engine.cancel_order(command)

    assert rejection.reason == CancelRejectReason.UNKNOWN_ORDER
    assert rejection.cancel_cl_ord_id == "CXL-UNKNOWN"
    assert rejection.orig_cl_ord_id == "UNKNOWN"
    assert rejection.session_id == "FIX.4.4:CLIENT->MOCKX"
    assert reports == []
    assert trades == []
    assert not engine.orders
    with pytest.raises(FrozenInstanceError):
        rejection.text = "changed"


def test_filled_order_cancel_is_too_late_without_terminal_duplication():
    engine, reports, _, _ = make_engine()
    submit(
        engine,
        cl_ord_id="FILLED-PASSIVE",
        side=Side.SELL,
        qty=5,
        price=100,
    )
    filled_id = submit(
        engine,
        cl_ord_id="FILLED-AGGRESSOR",
        side=Side.BUY,
        qty=5,
        price=100,
    )
    command = ProcessOrderCommand(filled_id, expected_version=0)
    engine.process_order(command)

    rejection = cancel(engine, filled_id)
    engine.process_order(command)

    own_reports = [
        report for report in reports if report.order_id == filled_id
    ]
    assert rejection.reason == CancelRejectReason.TOO_LATE_TO_CANCEL
    assert engine.orders[filled_id].status == OrderStatus.FILLED
    assert len(
        [
            report
            for report in own_reports
            if report.ord_status == OrderStatus.FILLED
        ]
    ) == 1
    assert successful_cancels(reports, filled_id) == []


def test_duplicate_cancel_rejects_and_does_not_mutate_book_twice():
    engine, reports, _, _ = make_engine()
    survivor_id = submit(
        engine,
        cl_ord_id="CANCEL-SURVIVOR",
        side=Side.BUY,
        qty=7,
        price=98,
    )
    order_id = submit(
        engine,
        cl_ord_id="CANCEL-TWICE",
        side=Side.BUY,
        qty=5,
        price=99,
    )
    original_version = engine.orders[order_id].version

    assert cancel(
        engine,
        order_id,
        expected_version=original_version,
    ) is None
    rejection = cancel(
        engine,
        order_id,
        expected_version=original_version,
    )

    assert rejection.reason == CancelRejectReason.ALREADY_CANCELED
    assert len(successful_cancels(reports, order_id)) == 1
    assert engine.orders[order_id].canceled_qty == 5
    assert engine.books["TEST"].is_resting(survivor_id)
    assert not engine.books["TEST"].is_resting(order_id)
    assert engine.books["TEST"].bids[0].qty == 7
    engine.books["TEST"].assert_invariants()


def test_session_scoped_duplicate_cl_ord_id_cancels_only_owner():
    engine, reports, _, _ = make_engine()
    session_a = "FIX.4.4:CLIENT-A->MOCKX"
    session_b = "FIX.4.4:CLIENT-B->MOCKX"
    order_a = submit(
        engine,
        cl_ord_id="SAME-ID",
        side=Side.BUY,
        qty=5,
        price=99,
        session_id=session_a,
        session_alias="CLIENT-A",
    )
    order_b = submit(
        engine,
        cl_ord_id="SAME-ID",
        side=Side.BUY,
        qty=7,
        price=98,
        session_id=session_b,
        session_alias="CLIENT-B",
    )

    assert cancel(engine, order_a, session_id=session_a) is None

    assert engine.orders[order_a].status == OrderStatus.CANCELLED
    assert engine.orders[order_b].status == OrderStatus.NEW
    assert not engine.books["TEST"].is_resting(order_a)
    assert engine.books["TEST"].is_resting(order_b)
    assert len(successful_cancels(reports, order_a)) == 1
    engine.books["TEST"].assert_invariants()


def test_cross_session_cancel_command_is_rejected():
    engine, _, _, _ = make_engine()
    order_id = submit(
        engine,
        cl_ord_id="SESSION-OWNER",
        side=Side.SELL,
        qty=5,
        price=101,
        session_id="FIX.4.4:OWNER->MOCKX",
        session_alias="OWNER",
    )

    rejection = cancel(
        engine,
        order_id,
        session_id="FIX.4.4:OTHER->MOCKX",
    )

    assert rejection.reason == CancelRejectReason.SESSION_MISMATCH
    assert engine.orders[order_id].status == OrderStatus.NEW
    assert engine.books["TEST"].is_resting(order_id)

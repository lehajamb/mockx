"""Native amendments preserve chain accounting and queue priority."""

from dataclasses import FrozenInstanceError, replace

import pytest

from src.mockx.fix_fields import cancel_reject_fields, execution_report_fields
from src.mockx.models import (
    CancelOrderCommand, CancelReject, ClientOrderKey, DisplayedOrderAdded,
    DisplayedOrderDeleted, DisplayedOrderExecuted, DisplayedOrderReduced,
    ExecutionType, OrderStatus, ProcessOrderCommand, ReplaceOrderCommand, Side,
    TERMINAL_ORDER_STATUSES, TimeInForce,
)
from tests.lifecycle_helpers import make_engine, submit


def amendment(engine, order_id, *, cl_ord_id="R-1", total=5, price=None, **values):
    order = engine.orders[order_id]
    return ReplaceOrderCommand(
        replace_cl_ord_id=cl_ord_id, orig_cl_ord_id=order.cl_ord_id,
        symbol=order.symbol, side=order.side, order_qty_lots=total,
        price_ticks=price if price is not None else order.price_ticks,
        requested_at=engine.scheduler.current_time, session_id=order.session_id,
        participant_id=order.participant_id, session_alias=order.session_alias,
        run_generation=order.run_generation, list_id=order.list_id, **values,
    )


def seed(engine, cl_ord_id="N-1", qty=10, side=Side.BUY, price=100, process=True):
    return submit(engine, cl_ord_id=cl_ord_id, participant_id=cl_ord_id,
                  side=side, qty=qty, price=price, process=process)


def assert_counts(engine):
    assert engine.active_order_count() == sum(
        order.status not in TERMINAL_ORDER_STATUSES for order in engine.orders.values()
    )
    assert engine.resting_order_count() == sum(
        len(level.orders) for book in engine.books.values()
        for level in (*book.bids, *book.asks)
    )
    for book in engine.books.values():
        book.assert_invariants()


def test_reduction_keeps_fifo_identity_and_previous_reports():
    engine, reports, _, _ = make_engine()
    engine.run_id = "original-run"
    first = seed(engine)
    second = seed(engine, "N-2")
    original_report = reports[0]
    original_reference = engine.orders[first].displayed_order_ref
    events = []
    engine.on_displayed_order_event = events.append

    result = engine.replace_order(amendment(engine, first, total=6))

    assert result.exec_type == ExecutionType.REPLACED
    assert result.cl_ord_id == "R-1" and result.orig_cl_ord_id == "N-1"
    assert result.exchange_order_id == original_report.exchange_order_id
    assert result.run_id == "original-run"
    assert execution_report_fields(result)["ExecType"] == "5"
    assert [order.id for order in engine.books["TEST"].bids[0].orders] == [first, second]
    assert engine.books["TEST"].bids[0].level_qty_lots == 16
    assert engine.orders[first].displayed_order_ref == original_reference
    assert len(events) == 1 and isinstance(events[0], DisplayedOrderReduced)
    assert events[0].qty_lots == 4 and events[0].remaining_qty_lots == 6
    assert events[0].logical_order_id == first
    assert original_report.cl_ord_id == "N-1" and original_report.order_qty_lots == 10
    with pytest.raises(FrozenInstanceError):
        result.cl_ord_id = "changed"
    assert ClientOrderKey(result.session_id, "N-1") not in engine.client_order_index
    assert engine.client_order_index[ClientOrderKey(result.session_id, "R-1")] == first
    assert_counts(engine)


def test_increase_loses_priority_with_fresh_displayed_reference():
    engine, _, _, _ = make_engine()
    first = seed(engine)
    second = seed(engine, "N-2")
    old_ref = engine.orders[first].displayed_order_ref
    events = []
    engine.on_displayed_order_event = events.append
    engine.replace_order(amendment(engine, first, total=12))
    assert [order.id for order in engine.books["TEST"].bids[0].orders] == [second, first]
    assert [type(event) for event in events] == [DisplayedOrderDeleted, DisplayedOrderAdded]
    assert events[0].order_id == old_ref
    assert events[1].order_id != old_ref and events[1].logical_order_id == first
    assert events[1].qty_lots == 12
    assert_counts(engine)


def test_reprice_matches_existing_passive_fifo_and_reports_new_identity():
    engine, reports, trades, _ = make_engine()
    first = seed(engine, qty=10, price=99)
    passive_a = seed(engine, "SELL-A", qty=2, side=Side.SELL, price=101)
    passive_b = seed(engine, "SELL-B", qty=3, side=Side.SELL, price=101)
    events = []
    engine.on_displayed_order_event = events.append
    offset = len(reports)
    engine.replace_order(amendment(engine, first, total=7, price=102))
    assert [trade.seller_order_id for trade in trades] == [passive_a, passive_b]
    assert [type(event) for event in events] == [
        DisplayedOrderDeleted, DisplayedOrderExecuted, DisplayedOrderExecuted, DisplayedOrderAdded
    ]
    assert reports[offset].exec_type == ExecutionType.REPLACED
    own = [report for report in reports[offset:] if report.order_id == first]
    assert [report.exec_type for report in own] == [
        ExecutionType.REPLACED, ExecutionType.TRADE, ExecutionType.TRADE
    ]
    assert all(report.cl_ord_id == "R-1" for report in own)
    assert engine.orders[first].leaves_qty_lots == 2
    assert_counts(engine)


@pytest.mark.parametrize("total", [1, 4])
def test_total_at_or_below_cumulative_quantity_stops_without_a_trade(total):
    engine, _, trades, _ = make_engine()
    resting = seed(engine, side=Side.SELL)
    seed(engine, "TAKER", qty=4, price=100)
    old_notional = engine.orders[resting].filled_notional_tick_lots
    events = []
    engine.on_displayed_order_event = events.append
    result = engine.replace_order(amendment(engine, resting, total=total))
    assert result.exec_type == ExecutionType.REPLACED
    assert result.ord_status == OrderStatus.FILLED
    assert result.order_qty_lots == result.cum_qty_lots == 4
    assert result.leaves_qty_lots == result.canceled_qty_lots == 0
    assert result.last_qty_lots is None
    assert result.filled_notional_tick_lots == old_notional
    assert len(trades) == 1
    assert len(events) == 1 and isinstance(events[0], DisplayedOrderDeleted)
    assert_counts(engine)


def test_pending_replace_rejection_leaves_initial_processing_valid():
    engine, _, _, _ = make_engine()
    order_id = seed(engine, process=False)
    result = engine.replace_order(amendment(engine, order_id))
    assert isinstance(result, CancelReject) and result.response_to == "2"
    assert engine.orders[order_id].version == 0
    engine.process_order(ProcessOrderCommand(order_id, 0))
    assert engine.orders[order_id].status == OrderStatus.NEW
    assert_counts(engine)


def test_noop_changes_client_identity_without_book_delta_or_priority_loss():
    engine, _, _, _ = make_engine()
    first = seed(engine)
    second = seed(engine, "N-2")
    events = []
    engine.on_displayed_order_event = events.append
    engine.replace_order(amendment(engine, first, total=10))
    assert events == []
    assert [order.id for order in engine.books["TEST"].bids[0].orders] == [first, second]
    assert_counts(engine)


def test_diagnostic_book_sequence_advances_per_validated_change_without_serialization(monkeypatch):
    engine, _, _, _ = make_engine()
    def forbidden(*args, **kwargs):
        raise AssertionError("A command serialized the full diagnostic book")
    monkeypatch.setattr(engine, "_create_market_data_snapshot", forbidden)
    first = seed(engine)
    assert engine.market_data_sequence == 1
    engine.replace_order(amendment(engine, first, total=10))
    assert engine.market_data_sequence == 1
    engine.replace_order(amendment(engine, first, total=6, cl_ord_id="R-2"))
    assert engine.market_data_sequence == 2
    engine.replace_order(amendment(engine, first, total=8, cl_ord_id="R-3"))
    assert engine.market_data_sequence == 3  # D+A is one completed mutation.
    order = engine.orders[first]
    engine.cancel_order(CancelOrderCommand("C-1", order.cl_ord_id, "TEST", Side.BUY,
                                          order.order_qty_lots, engine.scheduler.current_time,
                                          order.session_id))
    assert engine.market_data_sequence == 4
    assert_counts(engine)


@pytest.mark.parametrize("changed", [
    {"symbol": "OTHER"}, {"side": Side.SELL}, {"list_id": "wrong"},
    {"exchange_order_id": "wrong"}, {"participant_id": "wrong"},
    {"orig_cl_ord_id": "missing"}, {"session_id": "different"},
    {"duplicate_request": True}, {"run_generation": 999},
])
def test_invalid_replace_is_atomic(changed):
    engine, _, _, _ = make_engine()
    order_id = seed(engine)
    original = engine.orders[order_id]
    before = (original.cl_ord_id, original.price_ticks, original.order_qty_lots,
              original.version, original.displayed_order_ref)
    result = engine.replace_order(replace(amendment(engine, order_id), **changed))
    assert isinstance(result, CancelReject)
    assert cancel_reject_fields(result)["CxlRejResponseTo"] == "2"
    assert before == (original.cl_ord_id, original.price_ticks, original.order_qty_lots,
                      original.version, original.displayed_order_ref)
    assert engine.books["TEST"].bids[0].orders[0] is original
    assert_counts(engine)


def test_list_membership_is_required_for_subsequent_cancel_and_replace():
    engine, _, _, _ = make_engine()
    order_id = seed(engine)
    order = engine.orders[order_id]
    order.list_id = "LIST-1"
    missing = replace(amendment(engine, order_id), list_id=None)
    assert isinstance(engine.replace_order(missing), CancelReject)
    accepted = engine.replace_order(amendment(engine, order_id, cl_ord_id="R-2"))
    assert execution_report_fields(accepted)["ListID"] == "LIST-1"
    cancel = CancelOrderCommand("C-1", "R-2", "TEST", Side.BUY, 5,
                               engine.scheduler.current_time, order.session_id)
    assert isinstance(engine.cancel_order(cancel), CancelReject)
    accepted_cancel = engine.cancel_order(replace(cancel, cancel_cl_ord_id="C-2", list_id="LIST-1"))
    assert accepted_cancel.exec_type == ExecutionType.CANCELED
    assert execution_report_fields(accepted_cancel)["ListID"] == "LIST-1"
    assert_counts(engine)


def test_active_counts_are_independent_of_retained_terminal_history():
    engine, _, _, _ = make_engine()
    for index in range(20):
        seed(engine, f"IOC-{index}", qty=1, side=Side.BUY, price=100)
        seed(engine, f"HIT-{index}", qty=1, side=Side.SELL, price=100)
        assert_counts(engine)
    pending = seed(engine, "PENDING", process=False)
    assert engine.active_order_count() == 1
    assert engine.resting_order_count() == 0
    engine.reject_pending_order(ProcessOrderCommand(pending, 0), "unavailable")
    assert_counts(engine)
    class NoHistoryScan(dict):
        def values(self):
            raise AssertionError("count queried retained order history")
    engine.orders = NoHistoryScan(engine.orders)
    assert engine.active_order_count() == engine.resting_order_count() == 0
    engine.reset_run_state()
    assert engine.active_order_count() == 0

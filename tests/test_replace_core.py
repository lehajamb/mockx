"""Execution-time replacement, FIFO, identity, and displayed-book contracts."""

from dataclasses import FrozenInstanceError, replace

import pytest

from src.mockx.engine import MatchingEngine
from src.mockx.latency import LatencyScheduler
from src.mockx.models import (
    CancelOrderCommand, ClientOrderKey, DisplayedOrderAdded,
    DisplayedOrderDeleted, DisplayedOrderExecuted, DisplayedOrderReduced,
    ExecutionParams, ExecutionReport, ExecutionType, InvalidBookMutation,
    OrderCancelReject, OrderBookInvariantError, OrderStatus, OrderType, ProcessOrderCommand,
    ReplaceOrderCommand, Side, TimeInForce,
)
from tests.exact_helpers import TEST_INSTRUMENTS, exact_command


@pytest.fixture
def core():
    engine = MatchingEngine(ExecutionParams(0, 0, reject_prob=0, rand_seed=42),
                            LatencyScheduler(), TEST_INSTRUMENTS)
    engine.reports, engine.events, engine.snapshots = [], [], []
    engine.on_execution_report = engine.reports.append
    engine.on_displayed_order_event = engine.events.append
    engine.on_market_data_update = engine.snapshots.append
    return engine


def submit(engine, name="a", *, side=Side.BUY, qty=10, price=100,
           participant=None, process=True, session="owner"):
    order_id = engine.submit_order(exact_command(
        cl_ord_id=name, symbol="TEST", side=side, order_qty_lots=qty,
        price_ticks=price, tif=TimeInForce.GTC,
        submitted_at=engine.scheduler.current_time,
        participant_id=participant or name, session_id=session,
    ))
    if process:
        engine.process_order(ProcessOrderCommand(order_id, 0))
    return engine.orders[order_id]


def command(engine, order, *, name="r1", qty=None, price=None, **values):
    return ReplaceOrderCommand(
        replace_cl_ord_id=name, orig_cl_ord_id=order.cl_ord_id,
        symbol=order.symbol, side=order.side, order_type=order.order_type,
        order_qty_lots=order.order_qty_lots if qty is None else qty,
        price_ticks=order.price_ticks if price is None else price,
        tif=order.tif, requested_at=engine.scheduler.current_time,
        session_id=order.session_id, participant_id=order.participant_id,
        run_generation=engine.identifier_scope.generation, **values,
    )


def queue(engine):
    return [o.id for level in engine.books["TEST"].bids for o in level.orders]


def test_reduction_preserves_fifo_identity_and_immutable_reports(core):
    first = submit(core)
    second = submit(core, "b")
    old = core.reports[0]
    reference = first.displayed_order_ref
    report = core.replace_order(command(core, first, qty=6, ingress_sequence=17))
    assert report.exec_type == ExecutionType.REPLACED
    assert report.orig_cl_ord_id == "a" and report.cl_ord_id == "r1"
    assert report.replace_ingress_sequence == 17
    assert report.order_id == first.id and report.exchange_order_id == old.exchange_order_id
    assert report.submitted_at == old.submitted_at
    assert queue(core) == [first.id, second.id]
    assert first.displayed_order_ref == reference
    assert first.canceled_qty_lots == 0
    assert core.books["TEST"].bids[0].level_qty_lots == 16
    assert isinstance(core.events[-1], DisplayedOrderReduced)
    assert core.events[-1].canceled_qty_lots == 4
    assert old.order_qty_lots == 10 and old.cl_ord_id == "a"
    with pytest.raises(FrozenInstanceError):
        report.cl_ord_id = "changed"
    core.books["TEST"].assert_invariants()


def test_noop_advances_revision_without_publishing_or_rng_draw(core):
    order = submit(core)
    counts = len(core.events), len(core.snapshots)
    state = core.rng.getstate()
    report = core.replace_order(command(core, order))
    assert report.exec_type == ExecutionType.REPLACED and order.cl_ord_id == "r1"
    assert (len(core.events), len(core.snapshots)) == counts
    assert core.rng.getstate() == state


def test_increase_loses_fifo_and_allocates_fresh_display_reference(core):
    first, second = submit(core), submit(core, "b")
    old_ref = first.displayed_order_ref
    core.replace_order(command(core, first, qty=11))
    assert queue(core) == [second.id, first.id]
    assert first.displayed_order_ref not in {old_ref, second.displayed_order_ref}
    assert isinstance(core.events[-2], DisplayedOrderDeleted)
    assert core.events[-2].order_ref_num == old_ref
    assert isinstance(core.events[-1], DisplayedOrderAdded)


def test_reprice_matches_multiple_levels_acknowledges_before_fills(core):
    order = submit(core)
    submit(core, "s1", side=Side.SELL, qty=2, price=101)
    submit(core, "s2", side=Side.SELL, qty=3, price=102)
    old_ref = order.displayed_order_ref
    before = len(core.reports)
    ack = core.replace_order(command(core, order, qty=7, price=102))
    reports = core.reports[before:]
    assert reports[0] is ack
    assert ack.ord_status == OrderStatus.NEW and ack.cum_qty_lots == 0
    assert [r.last_qty_lots for r in reports if r.order_id == order.id][1:] == [2, 3]
    assert all(r.cl_ord_id == "r1" for r in reports if r.order_id == order.id)
    assert order.filled_qty_lots == 5 and order.leaves_qty_lots == 2
    assert [type(e) for e in core.events[-4:]] == [
        DisplayedOrderDeleted, DisplayedOrderExecuted,
        DisplayedOrderExecuted, DisplayedOrderAdded,
    ]
    assert core.events[-4].order_ref_num == old_ref
    assert order.displayed_order_ref != old_ref
    core.books["TEST"].assert_invariants()


@pytest.mark.parametrize("total", [1, 4])
def test_total_at_or_below_cumulative_clamps_without_fake_fill(core, total):
    order = submit(core)
    submit(core, "s", side=Side.SELL, qty=4)
    before = len(core.reports)
    notional = order.filled_notional_tick_lots
    report = core.replace_order(command(core, order, qty=total))
    assert report.ord_status == OrderStatus.FILLED
    assert report.order_qty_lots == report.cum_qty_lots == 4
    assert report.leaves_qty_lots == report.canceled_qty_lots == 0
    assert order.filled_notional_tick_lots == notional
    assert len(core.reports) == before + 1
    assert not core.books["TEST"].is_resting(order.id)
    assert isinstance(core.events[-1], DisplayedOrderReduced)


def test_partial_total_is_inclusive_of_fills(core):
    order = submit(core)
    submit(core, "s", side=Side.SELL, qty=4)
    report = core.replace_order(command(core, order, qty=7))
    assert report.ord_status == OrderStatus.PARTIAL
    assert (report.order_qty_lots, report.cum_qty_lots, report.leaves_qty_lots) == (7, 4, 3)


def test_stale_references_rejected_after_success_and_latest_can_cancel(core):
    order = submit(core)
    original = command(core, order)
    core.replace_order(original)
    stale = core.replace_order(replace(original, replace_cl_ord_id="stale"))
    assert isinstance(stale, OrderCancelReject)
    assert "latest" in stale.text.lower() or "stale" in stale.text.lower()
    cancel = CancelOrderCommand("cancel-old", "a", "TEST", Side.BUY, 10,
                                core.scheduler.current_time, "owner")
    assert isinstance(core.cancel_order(cancel), OrderCancelReject)
    accepted = core.cancel_order(replace(cancel, cancel_cl_ord_id="cancel-new", orig_cl_ord_id="r1"))
    assert accepted.exec_type == ExecutionType.CANCELED
    assert core.client_order_index[ClientOrderKey("owner", "a")] == order.id
    assert core.client_order_index[ClientOrderKey("owner", "r1")] == order.id


@pytest.mark.parametrize("change", [
    {"symbol": "OTHER"}, {"side": Side.SELL}, {"order_type": OrderType.MARKET, "price_ticks": None},
    {"tif": TimeInForce.IOC}, {"participant_id": "different"},
    {"duplicate_request": True}, {"run_generation": 99},
    {"order_qty_lots": 0}, {"price_ticks": 0},
])
def test_rejected_terms_leave_book_and_active_revision_unchanged(core, change):
    order = submit(core)
    initial = core.get_market_data_snapshot("TEST")
    report = core.replace_order(replace(command(core, order), **change))
    assert isinstance(report, OrderCancelReject)
    assert order.cl_ord_id == "a" and order.order_qty_lots == 10 and order.price_ticks == 100
    assert core.get_market_data_snapshot("TEST") == initial
    assert ClientOrderKey("owner", "r1") not in core.client_order_index
    core.books["TEST"].assert_invariants()


def test_pending_and_terminal_rejected_at_execution_time(core):
    order = submit(core, process=False)
    request = command(core, order)
    reject = core.replace_order(request)
    assert isinstance(reject, OrderCancelReject) and reject.ord_status == OrderStatus.PENDING_NEW
    core.process_order(ProcessOrderCommand(order.id, 0))
    submit(core, "s", side=Side.SELL)
    reject = core.replace_order(replace(request, replace_cl_ord_id="r2"))
    assert isinstance(reject, OrderCancelReject) and reject.ord_status == OrderStatus.FILLED


def test_wrong_session_does_not_take_ownership(core):
    order = submit(core)
    reject = core.replace_order(replace(command(core, order), session_id="foreign"))
    assert isinstance(reject, OrderCancelReject)
    assert "session" in reject.text.lower() and order.cl_ord_id == "a"


def test_reprice_reuses_self_trade_prevention(core):
    order = submit(core, participant="same")
    passive = submit(core, "s", side=Side.SELL, price=101, participant="same")
    before = len(core.reports)
    ack = core.replace_order(command(core, order, price=101))
    assert core.reports[before] is ack
    assert order.status == passive.status == OrderStatus.CANCELLED
    assert order.filled_qty_lots == passive.filled_qty_lots == 0
    assert not core.books["TEST"].bids and not core.books["TEST"].asks


def test_atomic_resize_validates_before_mutation(core):
    order = submit(core)
    book = core.books["TEST"]
    with pytest.raises(InvalidBookMutation):
        book.resize_order(order, -1)
    assert order.order_qty_lots == 10 and book.bids[0].level_qty_lots == 10
    book.bids[0].market_qty_lots = 0
    with pytest.raises(OrderBookInvariantError):
        book.resize_order(order, 5)
    assert order.order_qty_lots == 10 and book.bids[0].level_qty_lots == 10


def test_fill_while_replace_waits_uses_execution_time_cumulative(core):
    order = submit(core)
    request = command(core, order, qty=3)
    submit(core, "s", side=Side.SELL, qty=5)
    report = core.replace_order(request)
    assert report.order_qty_lots == report.cum_qty_lots == 5
    assert report.ord_status == OrderStatus.FILLED


def test_successful_revisions_chain_and_rejected_id_never_resolves(core):
    order = submit(core)
    core.replace_order(command(core, order))
    invalid = replace(command(core, order, name="bad"), price_ticks=0)
    reject = core.replace_order(invalid)
    next_ack = core.replace_order(command(core, order, name="r2", qty=8))
    assert next_ack.orig_cl_ord_id == "r1" and next_ack.cl_ord_id == "r2"
    missing = core.replace_order(replace(command(core, order, name="r3"), orig_cl_ord_id="bad"))
    assert isinstance(missing, OrderCancelReject) and missing.order_id is None
    assert reject.ord_status == OrderStatus.NEW and reject.orig_cl_ord_id == "r1"


def test_rejected_id_remains_claimed_across_resets(core):
    order = submit(core)
    request = replace(command(core, order), price_ticks=0)
    core.replace_order(request)
    core.reset_run_state()
    next_order = submit(core, "next")
    duplicate = core.replace_order(command(core, next_order))
    assert isinstance(duplicate, OrderCancelReject)
    assert "duplicate" in duplicate.text.lower()


def test_unavailable_replacement_preserves_order_and_immutable_status(core):
    order = submit(core)
    request = command(core, order)
    reject = core.reject_replace_unavailable(request, "Exchange unavailable")
    assert isinstance(reject, OrderCancelReject)
    assert reject.cl_ord_id == "r1" and reject.ord_status == OrderStatus.NEW
    assert order.cl_ord_id == "a" and order.leaves_qty_lots == 10
    submit(core, "s", side=Side.SELL)
    assert reject.ord_status == OrderStatus.NEW and order.status == OrderStatus.FILLED


def test_foreign_historical_cancel_reference_is_recognized_as_wrong_owner(core):
    order = submit(core)
    core.replace_order(command(core, order))
    request = CancelOrderCommand("foreign-cancel", "a", "TEST", Side.BUY, 10,
                                  core.scheduler.current_time, "foreign")
    reject = core.cancel_order(request)
    assert isinstance(reject, OrderCancelReject)
    assert "session" in reject.text.lower()


def test_replace_correlation_only_belongs_to_the_replaced_ack(core):
    order = submit(core)
    with pytest.raises(ValueError, match="REPLACED"):
        replace(core.reports[0], replace_ingress_sequence=123)
    submit(core, "s", side=Side.SELL, qty=2, price=101)
    core.replace_order(command(core, order, price=101, ingress_sequence=123))
    correlated = [report for report in core.reports if report.replace_ingress_sequence is not None]
    assert len(correlated) == 1 and correlated[0].exec_type == ExecutionType.REPLACED


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("failure", ["duplicate_flag", "claimed_id", "stale_generation"])
def test_early_replace_rejections_snapshot_authoritative_owned_order(core, partial, failure):
    order = submit(core)
    if partial:
        submit(core, "passive-fill", side=Side.SELL, qty=3)
    request = command(core, order)
    if failure == "duplicate_flag":
        request = replace(request, duplicate_request=True)
    elif failure == "claimed_id":
        request = replace(request, replace_cl_ord_id=order.cl_ord_id)
    else:
        request = replace(request, run_generation=99)
    before = core.get_market_data_snapshot("TEST")
    reject = core.replace_order(request)
    expected_status = OrderStatus.PARTIAL if partial else OrderStatus.NEW
    assert isinstance(reject, OrderCancelReject)
    assert reject.order_id == order.id
    assert reject.exchange_order_id == order.exchange_order_id
    assert reject.ord_status == expected_status
    assert core.get_market_data_snapshot("TEST") == before
    assert order.cl_ord_id == "a" and order.order_qty_lots == 10
    submit(core, "remaining-fill", side=Side.SELL)
    assert order.status == OrderStatus.FILLED
    assert reject.ord_status == expected_status

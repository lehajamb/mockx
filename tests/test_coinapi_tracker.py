"""FIX replay accounting follows actual fills without resurrecting liquidity."""

from types import SimpleNamespace

import pytest

from examples.coinapi_replay.tracker import PendingCommandError, ReplayLedger
from src.mockx.models import Side
from tests.exact_helpers import TEST_INSTRUMENT


def event(
    index, operation="SET", *, source_id="a#1", remaining=7, matched=0, quantity=7
):
    return SimpleNamespace(
        index=index,
        operation=operation,
        source_id=source_id,
        side=Side.SELL,
        price_ticks=101,
        quantity_lots=quantity,
        source_remaining_lots=remaining,
        historical_matched_lots=matched,
    )


def report(
    clid,
    *,
    status="NEW",
    kind="0",
    cum=0,
    leaves=10,
    total=10,
    orig=None,
    message="ExecutionReport",
):
    return SimpleNamespace(
        cl_ord_id=clid,
        orig_cl_ord_id=orig,
        status=status,
        exec_type=kind,
        cum_qty=str(cum),
        leaves_qty=str(leaves),
        quantity=str(total),
        order_id="native",
        text=None,
        message_type=message,
    )


def ledger():
    state = ReplayLedger("TEST", TEST_INSTRUMENT, "R")
    state.register_seed(
        SimpleNamespace(
            source_id="a#1", side=Side.SELL, price_ticks=101, quantity_lots=10
        ),
        "seed",
        "native",
    )
    return state


def test_new_delete_is_pipelined_without_new_ack():
    state = ReplayLedger("TEST", TEST_INSTRUMENT, "R")
    new = state.translate(event(1, "ADD", remaining=10, quantity=10))
    cancel = state.translate(event(2, "DELETE", remaining=0, quantity=0))
    assert cancel.orig_cl_ord_id == new.cl_ord_id
    assert len(state.pending) == 2
    assert state.record_report(report(new.cl_ord_id)).intent == new
    assert (
        state.record_report(
            report(cancel.cl_ord_id, status="CANCELLED", kind="4", leaves=0)
        ).intent
        == cancel
    )
    assert not state.orders


def test_cancel_before_processing_settles_new_without_fabricating_new_ack():
    state = ReplayLedger("TEST", TEST_INSTRUMENT, "R")
    new = state.translate(event(1, "ADD", remaining=10, quantity=10))
    cancel = state.translate(event(2, "DELETE", remaining=0, quantity=0))
    outcome = state.record_report(
        report(cancel.cl_ord_id, status="CANCELLED", kind="4", leaves=0)
    )
    assert outcome.also_completed == (new,)
    assert not state.pending
    assert not state.orders


def test_replace_waits_for_previous_replace_and_preserves_strategy_fills():
    state = ledger()
    state.record_report(report("seed", status="PARTIAL", kind="F", cum=5, leaves=5))
    first = state.translate(event(1, remaining=6, matched=2))
    assert first.quantity_lots == 8
    with pytest.raises(PendingCommandError):
        state.translate(event(2, remaining=4, matched=2))
    assert (
        state.record_report(report("seed", status="PARTIAL", kind="F", cum=6, leaves=4))
        is None
    )
    outcome = state.record_report(
        report(
            first.cl_ord_id,
            orig="seed",
            status="PARTIAL",
            kind="5",
            cum=6,
            leaves=2,
            total=8,
        )
    )
    assert outcome.accepted
    cancel = state.translate(event(2, remaining=4, matched=2))
    assert cancel.kind == "CANCEL"


def test_match_once_terminal_passive_is_not_resurrected():
    state = ledger()
    state.record_report(report("seed", status="FILLED", kind="F", cum=10, leaves=0))
    match = event(1, "MATCH", remaining=0, matched=10, quantity=4)
    intent = state.translate(match)
    assert (intent.side, intent.tif, intent.price_ticks, intent.quantity_lots) == (
        "BUY",
        "IOC",
        101,
        4,
    )
    assert state.translate(match) is None
    assert (
        state.record_report(
            report(
                intent.cl_ord_id, status="PARTIAL", kind="F", cum=2, leaves=2, total=4
            )
        )
        is None
    )
    assert state.record_report(
        report(intent.cl_ord_id, status="CANCELLED", kind="4", cum=2, leaves=0, total=4)
    ).accepted
    assert not state.orders


def test_duplicate_and_old_report_cannot_double_fill_or_regress_order():
    state = ledger()
    fill = report("seed", status="PARTIAL", kind="F", cum=3, leaves=7)
    state.record_report(fill)
    state.record_report(fill)
    assert state.filled_quantity_lots == 3
    intent = state.translate(event(1, remaining=6))
    state.record_report(
        report(
            intent.cl_ord_id,
            orig="seed",
            status="PARTIAL",
            kind="5",
            cum=3,
            leaves=3,
            total=6,
        )
    )
    state.record_report(report("seed", status="NEW", kind="0", cum=0, leaves=10))
    assert state.orders["a#1"].cl_ord_id == intent.cl_ord_id
    assert state.orders["a#1"].leaves_qty_lots == 3


def test_out_of_order_dispatch_dedup_is_compacted():
    state = ledger()
    state.translate(event(3, remaining=10))
    state.translate(event(5, "MATCH", remaining=9, matched=1, quantity=1))
    state.translate(event(4, "MATCH", remaining=8, matched=2, quantity=1))
    assert state.translate(event(5, "MATCH", quantity=1)) is None
    assert not state._seen_ahead


def test_replace_reject_keeps_accepted_identity_and_live_state():
    state = ledger()
    first = state.translate(event(1, remaining=8))
    outcome = state.record_report(
        report(first.cl_ord_id, status="NEW", kind=None, message="OrderCancelReject")
    )
    assert not outcome.accepted
    assert outcome.order.cl_ord_id == "seed"
    assert outcome.order.leaves_qty_lots == 10
    second = state.translate(event(2, remaining=7))
    assert second.orig_cl_ord_id == "seed"


def test_pending_replace_cannot_resurrect_a_filled_order():
    state = ledger()
    first = state.translate(event(1, remaining=12))
    state.record_report(report("seed", status="FILLED", kind="F", cum=10, leaves=0))
    state.record_report(
        report(
            first.cl_ord_id,
            orig="seed",
            status="PARTIAL",
            kind="5",
            cum=10,
            leaves=2,
            total=12,
        )
    )
    assert state.orders["a#1"].status == "FILLED"
    assert not state.live_orders()
    assert state.translate(event(2, "DELETE", remaining=0, quantity=0)) is None
    assert not state.orders


def test_pending_new_replace_settles_old_id_and_delete_waits_for_g():
    state = ReplayLedger("TEST", TEST_INSTRUMENT, "R")
    new = state.translate(event(1, "ADD", remaining=10, quantity=10))
    amendment = state.translate(event(2, remaining=7))
    with pytest.raises(PendingCommandError):
        state.translate(event(3, "DELETE", remaining=0, quantity=0))
    outcome = state.record_report(
        report(amendment.cl_ord_id, orig=new.cl_ord_id, kind="5", leaves=7, total=7)
    )
    assert outcome.also_completed == (new,)
    cancel = state.translate(event(3, "DELETE", remaining=0, quantity=0))
    assert cancel.orig_cl_ord_id == amendment.cl_ord_id


def test_terminal_lifetimes_do_not_accumulate_in_the_ledger():
    state = ReplayLedger("TEST", TEST_INSTRUMENT, "R")
    for index in range(1, 2001, 2):
        source_id = f"a#{index}"
        state.translate(
            event(index, "ADD", source_id=source_id, remaining=10, quantity=10)
        )
        cancel = state.translate(
            event(index + 1, "DELETE", source_id=source_id, remaining=0, quantity=0)
        )
        state.record_report(
            report(cancel.cl_ord_id, status="CANCELLED", kind="4", leaves=0)
        )
    assert (
        not state.orders
        and not state.pending
        and not state._aliases
        and not state._seen_ahead
    )


def test_delayed_amendment_cannot_rewind_source_state_after_due_match():
    state = ledger()
    amendment = state.translate(event(1, remaining=8))
    with pytest.raises(PendingCommandError):
        state.translate(event(2, remaining=6))
    state.translate(event(3, "MATCH", remaining=0, matched=6, quantity=6))
    state.record_report(
        report(amendment.cl_ord_id, orig="seed", kind="5", leaves=8, total=8)
    )
    delayed = state.translate(event(2, remaining=6))
    assert delayed.quantity_lots == 6
    assert state.orders["a#1"].source_remaining_lots == 0
    assert state.orders["a#1"].historical_matched_lots == 6


def test_terminal_lineage_remains_until_its_deferred_event_is_consumed():
    state = ledger()
    amendment = state.translate(event(1, remaining=8))
    with pytest.raises(PendingCommandError):
        state.translate(event(2, remaining=6))
    state.translate(event(3, "MATCH", remaining=0, matched=6, quantity=6))
    state.record_report(report("seed", status="FILLED", kind="F", cum=10, leaves=0))
    state.record_report(
        report(
            amendment.cl_ord_id,
            orig="seed",
            status="FILLED",
            kind=None,
            message="OrderCancelReject",
        )
    )
    assert state.translate(event(2, remaining=6)) is None
    assert not state.orders

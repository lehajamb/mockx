"""FIX replay bookkeeping follows acknowledgements, not assumed source state."""

from types import SimpleNamespace

import pytest

from examples.coinapi_replay.tracker import PendingCommandError, ReplayLedger
from examples.fix_support import FixReport
from src.mockx.models import Side
from tests.exact_helpers import TEST_INSTRUMENT


def source_order(source_id="a", side=Side.SELL, quantity=10, price=101):
    return SimpleNamespace(source_id=source_id, side=side, quantity_lots=quantity,
                           price_ticks=price)


def event(index, operation="SET", *, remaining=7, matched=0, quantity=7,
          source_id="a", side=Side.SELL, price=101):
    return SimpleNamespace(index=index, operation=operation, source_id=source_id,
                           side=side, price_ticks=price, quantity_lots=quantity,
                           source_remaining_lots=remaining,
                           historical_matched_lots=matched)


def report(clid="seed", *, orig=None, status="PARTIAL", kind="F", cum=3,
           leaves=7, total=10, exec_id="exec-1", message="ExecutionReport"):
    return FixReport(message_type=message, sender_comp_id="replay_sell",
                     cl_ord_id=clid, orig_cl_ord_id=orig, status=status,
                     exec_type=kind, leaves_qty=str(leaves), last_price=None,
                     last_qty=None, text=None, cum_qty=str(cum), quantity=str(total),
                     exec_id=exec_id, order_id="G00000001-O00000001")


def ledger():
    result = ReplayLedger("TEST", TEST_INSTRUMENT)
    result.register_seed(source_order(), "seed", order_id="exchange-seed")
    return result


def test_replace_uses_historical_total_and_actual_cum_without_replenishing_fills():
    state = ledger()
    state.record_report(report(cum=5, leaves=5))
    intent = state.translate(event(1, remaining=6, matched=2))
    assert intent.kind == "REPLACE"
    assert intent.quantity_lots == 8  # max(actual CumQty, source leaves + historic fills)
    assert intent.orig_cl_ord_id == "seed"


def test_passive_fill_does_not_acknowledge_pending_replace():
    state = ledger()
    intent = state.translate(event(1))
    state.record_report(report(cum=2, leaves=8))
    assert not state.is_complete(intent.cl_ord_id)
    assert state.orders_snapshot()["a"].cl_ord_id == "seed"
    with pytest.raises(PendingCommandError):
        state.translate(event(2))
    state.record_report(report(intent.cl_ord_id, orig="seed", kind="5", cum=2,
                               leaves=5, total=7, exec_id="replace-ack"))
    assert state.is_complete(intent.cl_ord_id)
    assert state.outcome(intent.cl_ord_id).accepted
    assert state.orders_snapshot()["a"].cl_ord_id == intent.cl_ord_id
    assert state.translate(event(2, remaining=4)).orig_cl_ord_id == intent.cl_ord_id


def test_replace_rejection_keeps_accepted_id_and_actual_order():
    state = ledger()
    intent = state.translate(event(1))
    state.record_report(report(intent.cl_ord_id, orig="seed", kind=None, cum=0,
                               leaves=0, total=0, exec_id=None,
                               message="OrderCancelReject", status="NEW"))
    assert not state.outcome(intent.cl_ord_id).accepted
    current = state.orders_snapshot()["a"]
    assert (current.cl_ord_id, current.leaves_qty_lots) == ("seed", 10)
    assert state.translate(event(2)).orig_cl_ord_id == "seed"


def test_cancel_when_desired_total_is_already_filled_and_never_resurrect():
    state = ledger()
    state.record_report(report(cum=8, leaves=2))
    cancel = state.translate(event(1, remaining=4, matched=2))
    assert cancel.kind == "CANCEL"
    state.record_report(report(cancel.cl_ord_id, orig="seed", kind="4", cum=8,
                               leaves=0, status="CANCELLED", exec_id="cancel"))
    assert state.live_orders() == ()
    assert state.translate(event(2, remaining=100)) is None


def test_match_sends_full_opposite_side_ioc_once_even_if_source_is_terminal():
    state = ledger()
    state.record_report(report(status="FILLED", cum=10, leaves=0))
    match = event(1, "MATCH", remaining=0, matched=10, quantity=4)
    intent = state.translate(match)
    assert (intent.kind, intent.role, intent.side, intent.tif) == (
        "NEW", "replay_buy", "BUY", "IOC")
    assert (intent.quantity_lots, intent.price_ticks) == (4, 101)
    assert state.translate(match) is None
    state.record_report(report(intent.cl_ord_id, status="PARTIAL", cum=2,
                               leaves=2, total=4, exec_id="ioc-partial"))
    assert not state.is_complete(intent.cl_ord_id)
    state.record_report(report(intent.cl_ord_id, status="CANCELLED", kind="4",
                               cum=2, leaves=0, total=4, exec_id="ioc-cancel"))
    assert state.is_complete(intent.cl_ord_id)
    assert state.orders_snapshot()["a"].status == "FILLED"


def test_duplicate_reports_and_stale_fills_cannot_regress_accepted_replacement():
    state = ledger()
    old = report(cum=3, leaves=7)
    assert state.record_report(old)
    assert not state.record_report(old)
    intent = state.translate(event(1, remaining=5))
    state.record_report(report(intent.cl_ord_id, orig="seed", kind="5", cum=3,
                               leaves=2, total=5, exec_id="replacement"))
    state.record_report(report(cum=2, leaves=8, exec_id="late-old-fill"))
    current = state.orders_snapshot()["a"]
    assert (current.cum_qty_lots, current.leaves_qty_lots, current.order_qty_lots) == (3, 2, 5)


def test_add_is_owned_by_side_and_only_ack_accepts_it():
    state = ReplayLedger("TEST", TEST_INSTRUMENT)
    intent = state.translate(event(1, "ADD", side=Side.BUY, quantity=4, remaining=4))
    assert intent.role == "replay_buy"
    assert not state.is_complete(intent.cl_ord_id)
    state.record_report(report(intent.cl_ord_id, status="NEW", kind="0", cum=0,
                               leaves=4, total=4))
    assert state.outcome(intent.cl_ord_id).accepted
    assert state.live_orders()[0].order_id == "G00000001-O00000001"


def test_delayed_old_replace_ack_cannot_undo_a_newer_accepted_replace():
    state = ledger()
    first = state.translate(event(1, remaining=8))
    state.record_report(report(first.cl_ord_id, orig="seed", kind="5", cum=0,
                               leaves=8, total=8, exec_id="first"))
    second = state.translate(event(2, remaining=6))
    state.record_report(report(second.cl_ord_id, orig=first.cl_ord_id, kind="5", cum=0,
                               leaves=6, total=6, exec_id="second"))
    state.record_report(report(first.cl_ord_id, orig="seed", kind="5", cum=0,
                               leaves=8, total=8, exec_id="old-ack"))
    current = state.orders_snapshot()["a"]
    assert (current.cl_ord_id, current.order_qty_lots, current.leaves_qty_lots) == (
        second.cl_ord_id, 6, 6)


@pytest.mark.parametrize("operation", ["SET", "SUB"])
def test_zero_source_leaves_cancel_even_when_historical_matches_exceed_actual(operation):
    state = ledger()
    intent = state.translate(event(1, operation, remaining=0, matched=4, quantity=0))
    assert intent.kind == "CANCEL"
    assert intent.orig_cl_ord_id == "seed"


def test_source_quantities_advance_only_when_rows_are_released():
    state = ledger()
    seeded = state.orders_snapshot()["a"]
    assert (seeded.source_remaining_lots, seeded.historical_matched_lots) == (10, 0)

    amendment = state.translate(event(1, remaining=7))
    with pytest.raises(PendingCommandError):
        state.translate(event(2, remaining=5, matched=2))
    current = state.orders_snapshot()["a"]
    assert (current.source_remaining_lots, current.historical_matched_lots) == (7, 0)

    state.record_report(report(amendment.cl_ord_id, orig="seed", kind="5", cum=3,
                               leaves=4, total=7, exec_id="replace"))
    state.translate(event(2, "MATCH", remaining=5, matched=2, quantity=2))
    current = state.orders_snapshot()["a"]
    assert (current.source_remaining_lots, current.historical_matched_lots) == (5, 2)
    assert (current.cum_qty_lots, current.leaves_qty_lots) == (3, 4)

    cancel = state.translate(event(3, "DELETE", remaining=0, matched=2))
    state.record_report(report(cancel.cl_ord_id, orig=amendment.cl_ord_id, kind="4",
                               status="CANCELLED", cum=3, leaves=0, total=7,
                               exec_id="cancel"))
    assert state.translate(event(4, remaining=1, matched=2)) is None
    terminal = state.orders_snapshot()["a"]
    assert (terminal.source_remaining_lots, terminal.historical_matched_lots) == (1, 2)
    assert terminal.leaves_qty_lots == 0


def test_added_order_retains_source_quantities_before_fix_acknowledgement():
    state = ReplayLedger("TEST", TEST_INSTRUMENT)
    state.translate(event(1, "ADD", quantity=4, remaining=4))
    pending = state.orders_snapshot()["a"]
    assert (pending.source_remaining_lots, pending.historical_matched_lots) == (4, 0)
    assert pending.leaves_qty_lots == 0



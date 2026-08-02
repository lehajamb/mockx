"""Authoritative displayed-order event semantics."""

from datetime import datetime, timezone

import pytest

from src.mockx.latency import LatencyScheduler
from src.mockx.identifiers import RunIdentifierScope
from src.mockx.models import (
    CancelOrderCommand,
    DisplayedOrderAdded,
    DisplayedOrderDeleted,
    DisplayedOrderExecuted,
    ExecutionParams,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)

from .exact_helpers import exact_command, exact_engine


NOW = datetime(2025, 1, 15, 9, 30, tzinfo=timezone.utc)


def engine_and_events(*, reject_prob=0.0):
    scheduler = LatencyScheduler()
    scheduler.set_time(NOW)
    engine = exact_engine(
        ExecutionParams(0, 0, reject_prob, 7),
        scheduler,
    )
    engine.install_identifier_scope(RunIdentifierScope(1))
    events = []
    engine.on_displayed_order_event = events.append
    return engine, events


def submit(
    engine, *, cl_ord_id, side, qty, price, participant="P1", tif=TimeInForce.GTC
):
    order_id = engine.submit_order(
        exact_command(
            cl_ord_id=cl_ord_id,
            symbol="TEST",
            side=side,
            order_qty_lots=qty,
            price_ticks=price,
            tif=tif,
            submitted_at=NOW,
            session_id=participant,
            participant_id=participant,
        )
    )
    engine.process_order(ProcessOrderCommand(order_id, 0))
    return order_id


def test_passive_fill_has_one_match_event_and_partial_aggressor_rests_last():
    engine, events = engine_and_events()
    passive_id = submit(
        engine,
        cl_ord_id="S1",
        side=Side.SELL,
        qty=2,
        price=100,
        participant="SELLER",
    )
    assert events == [
        DisplayedOrderAdded(1, passive_id, "TEST", Side.SELL, 100, 2, NOW)
    ]

    events.clear()
    aggressor_id = submit(
        engine,
        cl_ord_id="B1",
        side=Side.BUY,
        qty=3,
        price=100,
        participant="BUYER",
    )

    assert events == [
        DisplayedOrderExecuted(1, passive_id, "TEST", 100, 100, 2, 1, NOW),
        DisplayedOrderAdded(1, aggressor_id, "TEST", Side.BUY, 100, 1, NOW),
    ]


def test_resting_cancel_emits_delete_but_pending_cancel_emits_nothing():
    engine, events = engine_and_events()
    resting_id = submit(
        engine,
        cl_ord_id="B1",
        side=Side.BUY,
        qty=2,
        price=99,
        participant="P1",
    )
    events.clear()
    engine.cancel_order(
        CancelOrderCommand(
            session_id="P1",
            participant_id="P1",
            cancel_cl_ord_id="C1",
            orig_cl_ord_id="B1",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=2,
            requested_at=NOW,
            run_generation=1,
            ingress_sequence=1,
        )
    )
    assert events == [DisplayedOrderDeleted(1, resting_id, "TEST", NOW)]

    pending_id = engine.submit_order(
        exact_command(
            cl_ord_id="B2",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=1,
            price_ticks=98,
            tif=TimeInForce.GTC,
            submitted_at=NOW,
            session_id="P1",
            participant_id="P1",
        )
    )
    engine.cancel_order(
        CancelOrderCommand(
            session_id="P1",
            participant_id="P1",
            cancel_cl_ord_id="C2",
            orig_cl_ord_id="B2",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=1,
            requested_at=NOW,
            run_generation=1,
            ingress_sequence=2,
        )
    )
    assert pending_id > 0
    assert events == [DisplayedOrderDeleted(1, resting_id, "TEST", NOW)]


def test_ioc_and_market_remainders_never_emit_add_or_delete():
    engine, events = engine_and_events()
    submit(
        engine,
        cl_ord_id="I1",
        side=Side.BUY,
        qty=2,
        price=100,
        participant="P1",
        tif=TimeInForce.IOC,
    )
    submit(
        engine,
        cl_ord_id="M1",
        side=Side.BUY,
        qty=2,
        price=None,
        participant="P1",
    )
    assert events == []


def test_multi_fill_uses_fill_ids_once_and_preserves_mutation_order():
    engine, events = engine_and_events()
    first_id = submit(
        engine,
        cl_ord_id="S1",
        side=Side.SELL,
        qty=1,
        price=99,
        participant="S1",
    )
    second_id = submit(
        engine,
        cl_ord_id="S2",
        side=Side.SELL,
        qty=1,
        price=100,
        participant="S2",
    )
    events.clear()
    submit(
        engine,
        cl_ord_id="B1",
        side=Side.BUY,
        qty=2,
        price=100,
        participant="BUYER",
    )
    assert events == [
        DisplayedOrderExecuted(1, first_id, "TEST", 99, 99, 1, 1, NOW),
        DisplayedOrderExecuted(1, second_id, "TEST", 100, 100, 1, 2, NOW),
    ]


def test_self_trade_prevention_deletes_only_the_displayed_passive_order():
    engine, events = engine_and_events()
    passive_id = submit(
        engine,
        cl_ord_id="S1",
        side=Side.SELL,
        qty=2,
        price=100,
        participant="OWNER",
    )
    events.clear()
    aggressor_id = submit(
        engine,
        cl_ord_id="B1",
        side=Side.BUY,
        qty=2,
        price=100,
        participant="OWNER",
    )
    assert aggressor_id != passive_id
    assert events == [DisplayedOrderDeleted(1, passive_id, "TEST", NOW)]


def test_observer_failure_does_not_change_engine_result():
    engine, _ = engine_and_events()

    def fail(_event):
        raise RuntimeError("feed unavailable")

    engine.on_displayed_order_event = fail
    order_id = submit(
        engine,
        cl_ord_id="B1",
        side=Side.BUY,
        qty=1,
        price=99,
        participant="P1",
    )
    assert engine.books["TEST"].is_resting(order_id)


def test_rejected_order_emits_no_displayed_event():
    engine, events = engine_and_events(reject_prob=1.0)
    order_id = submit(
        engine,
        cl_ord_id="R1",
        side=Side.BUY,
        qty=1,
        price=99,
        participant="P1",
    )
    assert engine.orders[order_id].status.value == "REJECTED"
    assert events == []


def test_events_are_not_published_when_final_book_invariant_fails(monkeypatch):
    engine, events = engine_and_events()
    book_type = type(engine.books["TEST"])

    def fail_invariant(_book):
        raise RuntimeError("forced invariant failure")

    monkeypatch.setattr(book_type, "assert_invariants", fail_invariant)
    with pytest.raises(RuntimeError, match="forced invariant failure"):
        submit(
            engine,
            cl_ord_id="B1",
            side=Side.BUY,
            qty=1,
            price=99,
            participant="P1",
        )
    assert events == []

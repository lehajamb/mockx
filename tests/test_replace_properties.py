"""Generated amendment/cancel/fill sequences preserve the displayed book."""

from hypothesis import given, settings, strategies as st

from src.mockx.identifiers import RunIdentifierScope
from src.mockx.latency import LatencyScheduler
from src.mockx.models import (
    CancelOrderCommand,
    DisplayedOrderAdded,
    DisplayedOrderDeleted,
    DisplayedOrderExecuted,
    DisplayedOrderReduced,
    ExecutionParams,
    OrderType,
    ProcessOrderCommand,
    ReplaceOrderCommand,
    Side,
    TERMINAL_ORDER_STATUSES,
    TimeInForce,
)

from .exact_helpers import exact_command, exact_engine


@settings(max_examples=50, deadline=None)
@given(st.lists(st.tuples(
    st.integers(0, 2), st.integers(0, 100),
    st.integers(1, 20), st.integers(95, 105),
), min_size=1, max_size=50))
def test_generated_revisions_preserve_invariants_and_displayed_book(commands):
    scheduler = LatencyScheduler()
    engine = exact_engine(ExecutionParams(0, 0, 0, 42), scheduler)
    engine.install_identifier_scope(RunIdentifierScope(1))
    events = []
    engine.on_displayed_order_event = events.append
    originals = {}
    displayed = {}
    seen_references = set()
    cursor = 0
    for sequence, (action, selector, quantity, price) in enumerate(commands, start=1):
        if action == 0 or not engine.orders:
            owner = f"P{selector % 3}"
            order_id = engine.submit_order(exact_command(
                cl_ord_id=f"N{sequence}", symbol="TEST",
                side=Side.BUY if selector % 2 else Side.SELL,
                order_qty_lots=quantity, price_ticks=price, tif=TimeInForce.GTC,
                submitted_at=scheduler.current_time,
                session_id=owner, participant_id=owner,
            ))
            originals[order_id] = f"N{sequence}"
            engine.process_order(ProcessOrderCommand(order_id, 0))
        else:
            order = list(engine.orders.values())[selector % len(engine.orders)]
            original = originals[order.id] if selector % 5 == 0 else order.cl_ord_id
            common = dict(
                orig_cl_ord_id=original, symbol=order.symbol, side=order.side,
                order_qty_lots=quantity, requested_at=scheduler.current_time,
                session_id=order.session_id, participant_id=order.participant_id,
                run_generation=1, ingress_sequence=sequence,
            )
            if action == 1:
                engine.replace_order(ReplaceOrderCommand(
                    replace_cl_ord_id=f"R{sequence}", order_type=OrderType.LIMIT,
                    price_ticks=price, tif=TimeInForce.GTC, **common,
                ))
            else:
                engine.cancel_order(CancelOrderCommand(cancel_cl_ord_id=f"C{sequence}", **common))

        for event in events[cursor:]:
            reference = event.order_ref_num
            if isinstance(event, DisplayedOrderAdded):
                assert reference not in seen_references
                seen_references.add(reference)
                displayed[reference] = [event.side, event.price_ticks, event.qty_lots]
            elif isinstance(event, DisplayedOrderDeleted):
                del displayed[reference]
            else:
                assert isinstance(event, (DisplayedOrderExecuted, DisplayedOrderReduced))
                delta = (event.executed_qty_lots if isinstance(event, DisplayedOrderExecuted)
                         else event.canceled_qty_lots)
                displayed[reference][2] -= delta
                assert displayed[reference][2] >= 0
                if displayed[reference][2] == 0:
                    del displayed[reference]
        cursor = len(events)
        book = engine.books["TEST"]
        book.assert_invariants()
        assert displayed == {
            order.displayed_order_ref: [order.side, order.price_ticks, order.leaves_qty_lots]
            for order in engine.orders.values() if book.is_resting(order.id)
        }
        assert all(not book.is_resting(order.id) for order in engine.orders.values()
                   if order.status in TERMINAL_ORDER_STATUSES)

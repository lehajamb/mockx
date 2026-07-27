"""Focused tests for immutable commands and the order lifecycle."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from src.mockx.engine import MatchingEngine
from src.mockx.latency import LatencyScheduler
from src.mockx.models import (
    CancelOrderCommand,
    ExecutionParams,
    InvalidOrderTransition,
    NewOrderCommand,
    Order,
    OrderStatus,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def new_command(
    cl_ord_id: str,
    side: Side = Side.BUY,
    qty: float = 10.0,
    price: float = 100.0,
) -> NewOrderCommand:
    return NewOrderCommand(
        cl_ord_id=cl_ord_id,
        symbol="TEST",
        side=side,
        qty=qty,
        price=price,
        tif=TimeInForce.GTC,
        submitted_at=NOW,
        session_id="FIX.4.4:CLIENT->MOCKX",
        session_alias="CLIENT",
    )


def make_engine() -> MatchingEngine:
    scheduler = LatencyScheduler()
    engine = MatchingEngine(
        ExecutionParams(
            network_latency_ms=10,
            engine_latency_ms=5,
            reject_prob=0.0,
            rand_seed=42,
        ),
        scheduler,
    )
    engine.add_symbol("TEST")
    scheduler.register_handler("order_arrived", lambda command: None)
    scheduler.register_handler("order_process", engine.process_order)
    scheduler.register_handler("exec_report", lambda report: None)
    return engine


def make_order() -> Order:
    return Order(
        id=1,
        cl_ord_id="STATE",
        symbol="TEST",
        side=Side.BUY,
        qty=10.0,
        price=100.0,
        tif=TimeInForce.GTC,
        ts_submitted=NOW,
    )


@pytest.mark.parametrize(
    ("command", "field", "value"),
    [
        (new_command("IMMUTABLE"), "qty", 5.0),
        (ProcessOrderCommand(1, 0), "expected_version", 1),
        (
            CancelOrderCommand(
                order_id=1,
                expected_version=0,
                cancel_cl_ord_id="CXL-1",
                orig_cl_ord_id="IMMUTABLE",
                requested_at=NOW,
            ),
            "order_id",
            2,
        ),
    ],
)
def test_order_commands_are_immutable(command, field, value):
    with pytest.raises(FrozenInstanceError):
        setattr(command, field, value)


@pytest.mark.parametrize(
    "transitions",
    [
        [OrderStatus.NEW],
        [OrderStatus.REJECTED],
        [OrderStatus.FILLED],
        [OrderStatus.NEW, OrderStatus.PARTIAL],
        [OrderStatus.NEW, OrderStatus.FILLED],
        [OrderStatus.NEW, OrderStatus.CANCELLED],
        [OrderStatus.PARTIAL, OrderStatus.PARTIAL],
        [OrderStatus.PARTIAL, OrderStatus.FILLED],
        [OrderStatus.PARTIAL, OrderStatus.CANCELLED],
    ],
)
def test_valid_order_state_transitions(transitions):
    order = make_order()

    for status in transitions:
        order.transition_to(status)

    assert order.status == transitions[-1]


@pytest.mark.parametrize(
    ("terminal", "target"),
    [
        (OrderStatus.FILLED, OrderStatus.NEW),
        (OrderStatus.FILLED, OrderStatus.CANCELLED),
        (OrderStatus.REJECTED, OrderStatus.NEW),
        (OrderStatus.REJECTED, OrderStatus.FILLED),
        (OrderStatus.CANCELLED, OrderStatus.NEW),
        (OrderStatus.CANCELLED, OrderStatus.FILLED),
    ],
)
def test_terminal_order_transitions_are_invalid(terminal, target):
    order = make_order()
    order.transition_to(terminal)

    with pytest.raises(InvalidOrderTransition):
        order.transition_to(target)


def test_partial_order_cannot_regress_to_new():
    order = make_order()
    order.transition_to(OrderStatus.PARTIAL)

    with pytest.raises(InvalidOrderTransition):
        order.transition_to(OrderStatus.NEW)


@pytest.mark.asyncio
async def test_cancelled_order_ignores_stale_processing_command(caplog):
    engine = make_engine()
    reports = []
    trades = []
    engine.on_execution_report = reports.append
    engine.on_trade = trades.append

    passive_id = engine.submit_order(
        new_command("PASSIVE", side=Side.SELL, qty=10.0, price=100.0)
    )
    engine.process_order(ProcessOrderCommand(passive_id, expected_version=0))
    passive = engine.orders[passive_id]

    aggressive_id = engine.submit_order(
        new_command("AGGRESSIVE", side=Side.BUY, qty=10.0, price=100.0)
    )
    queued_command = next(
        event.data
        for event in engine.scheduler.event_queue
        if event.event_type == "order_process"
        and event.data.order_id == aggressive_id
    )
    assert queued_command == ProcessOrderCommand(aggressive_id, expected_version=0)
    rejection_reason = engine.cancel_order(
        CancelOrderCommand(
            order_id=aggressive_id,
            expected_version=0,
            cancel_cl_ord_id="CXL-AGGRESSIVE",
            orig_cl_ord_id="AGGRESSIVE",
            requested_at=engine.scheduler.current_time,
            session_id="FIX.4.4:CLIENT->MOCKX",
            session_alias="CLIENT",
        )
    )

    assert rejection_reason is None
    assert engine.orders[aggressive_id].version == 1

    with caplog.at_level("INFO"):
        await engine.scheduler.run_until_idle()

    aggressive = engine.orders[aggressive_id]
    book = engine.books["TEST"]
    booked_order_ids = {
        order.id
        for level in (*book.bids, *book.asks)
        for order in level.orders
    }

    assert aggressive.status == OrderStatus.CANCELLED
    assert aggressive.filled_qty == 0.0
    assert aggressive_id not in booked_order_ids
    assert passive.status == OrderStatus.NEW
    assert passive.filled_qty == 0.0
    assert trades == []
    assert not any(
        report.order_id == aggressive_id
        and report.ord_status in {OrderStatus.PARTIAL, OrderStatus.FILLED}
        for report in reports
    )
    assert "Ignoring stale process command" in caplog.text


def test_duplicate_processing_command_is_idempotent():
    engine = make_engine()
    reports = []
    trades = []
    engine.on_execution_report = reports.append
    engine.on_trade = trades.append

    order_id = engine.submit_order(new_command("DUPLICATE"))
    command = ProcessOrderCommand(order_id, expected_version=0)

    engine.process_order(command)
    first_state = (
        engine.orders[order_id].status,
        engine.orders[order_id].filled_qty,
        len(reports),
        len(trades),
    )
    engine.process_order(command)

    book = engine.books["TEST"]
    copies = sum(
        order.id == order_id
        for level in book.bids
        for order in level.orders
    )
    assert copies == 1
    assert (
        engine.orders[order_id].status,
        engine.orders[order_id].filled_qty,
        len(reports),
        len(trades),
    ) == first_state

"""Deterministic regression tests for atomic order-book accounting."""

from datetime import datetime, timezone

import pytest

from src.mockx.engine import MatchingEngine
from src.mockx.latency import LatencyScheduler
from src.mockx.models import (
    CancelOrderCommand,
    ExecutionParams,
    ExecutionReport,
    InvalidBookMutation,
    NewOrderCommand,
    Order,
    OrderBook,
    OrderStatus,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def make_engine() -> MatchingEngine:
    engine = MatchingEngine(
        ExecutionParams(
            network_latency_ms=0,
            engine_latency_ms=0,
            reject_prob=0.0,
            rand_seed=42,
        ),
        LatencyScheduler(),
    )
    engine.add_symbol("TEST")
    return engine


def submit(
    engine: MatchingEngine,
    *,
    cl_ord_id: str,
    side: Side,
    qty: float,
    price: float,
    session_alias: str = "market_bot",
) -> Order:
    order_id = engine.submit_order(
        NewOrderCommand(
            cl_ord_id=cl_ord_id,
            symbol="TEST",
            side=side,
            qty=qty,
            price=price,
            tif=TimeInForce.GTC,
            submitted_at=NOW,
            session_id="TEST-SESSION",
            session_alias=session_alias,
        )
    )
    engine.process_order(ProcessOrderCommand(order_id, expected_version=0))
    return engine.orders[order_id]


def cancel(engine: MatchingEngine, order: Order) -> str | None:
    result = engine.cancel_order(
        CancelOrderCommand(
            cancel_cl_ord_id=f"CXL-{order.cl_ord_id}",
            orig_cl_ord_id=order.cl_ord_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            requested_at=NOW,
            session_id=order.session_id or "TEST-SESSION",
            session_alias=order.session_alias,
        )
    )
    return None if isinstance(result, ExecutionReport) else result


def level_at(book: OrderBook, side: Side, price: float):
    levels = book.bids if side == Side.BUY else book.asks
    return next(level for level in levels if level.price == price)


def assert_reference_quantities(book: OrderBook) -> None:
    """Calculate expectations directly from individual order fields."""
    for level in (*book.bids, *book.asks):
        expected_total = sum(
            order.qty - order.filled_qty for order in level.orders
        )
        expected_strategy = sum(
            order.qty - order.filled_qty
            for order in level.orders
            if order.session_alias == "strategy_bot"
        )
        expected_market = sum(
            order.qty - order.filled_qty
            for order in level.orders
            if order.session_alias != "strategy_bot"
        )
        assert level.qty == pytest.approx(expected_total, abs=1e-9)
        assert level.strategy_qty == pytest.approx(
            expected_strategy,
            abs=1e-9,
        )
        assert level.market_qty == pytest.approx(expected_market, abs=1e-9)


def test_passive_partial_fill_updates_all_aggregates_and_keeps_fifo():
    engine = make_engine()
    resting = submit(
        engine,
        cl_ord_id="REST-STRATEGY",
        side=Side.SELL,
        qty=10,
        price=100,
        session_alias="strategy_bot",
    )
    book = engine.books["TEST"]
    original_position = list(level_at(book, Side.SELL, 100).orders).index(
        resting
    )

    submit(
        engine,
        cl_ord_id="HIT-4",
        side=Side.BUY,
        qty=4,
        price=100,
    )

    level = level_at(book, Side.SELL, 100)
    assert resting.filled_qty == 4
    assert resting.remaining_qty == 6
    assert level.qty == 6
    assert level.strategy_qty == 6
    assert level.market_qty == 0
    assert list(level.orders) == [resting]
    assert list(level.orders).index(resting) == original_position
    book.assert_invariants()
    assert_reference_quantities(book)


def test_multiple_partial_fills_then_final_fill_remove_level():
    engine = make_engine()
    resting = submit(
        engine,
        cl_ord_id="REST-10",
        side=Side.SELL,
        qty=10,
        price=100,
    )
    book = engine.books["TEST"]

    for index, (fill_qty, expected_remaining) in enumerate(
        ((3, 7), (2, 5), (4, 1)),
        start=1,
    ):
        submit(
            engine,
            cl_ord_id=f"HIT-{index}",
            side=Side.BUY,
            qty=fill_qty,
            price=100,
        )
        level = level_at(book, Side.SELL, 100)
        assert resting.remaining_qty == expected_remaining
        assert level.qty == expected_remaining
        assert list(level.orders) == [resting]
        book.assert_invariants()
        assert_reference_quantities(book)

    submit(
        engine,
        cl_ord_id="HIT-FINAL",
        side=Side.BUY,
        qty=1,
        price=100,
    )
    assert resting.status == OrderStatus.FILLED
    assert not book.asks
    assert not book.is_resting(resting.id)
    book.assert_invariants()


def test_partial_fill_then_cancel_subtracts_only_remainder():
    engine = make_engine()
    resting = submit(
        engine,
        cl_ord_id="REST-CANCEL",
        side=Side.SELL,
        qty=10,
        price=100,
    )
    submit(
        engine,
        cl_ord_id="HIT-CANCEL",
        side=Side.BUY,
        qty=4,
        price=100,
    )

    assert resting.filled_qty == 4
    assert level_at(engine.books["TEST"], Side.SELL, 100).qty == 6
    assert cancel(engine, resting) is None
    assert resting.filled_qty == 4
    assert resting.status == OrderStatus.CANCELLED
    assert not engine.books["TEST"].asks
    engine.books["TEST"].assert_invariants()


def test_full_fill_removes_order_level_and_lookup_index():
    engine = make_engine()
    resting = submit(
        engine,
        cl_ord_id="REST-FULL",
        side=Side.SELL,
        qty=5,
        price=100,
    )
    submit(
        engine,
        cl_ord_id="HIT-FULL",
        side=Side.BUY,
        qty=5,
        price=100,
    )

    book = engine.books["TEST"]
    assert resting.status == OrderStatus.FILLED
    assert not book.asks
    assert not book.is_resting(resting.id)
    book.assert_invariants()


def test_full_fill_with_second_fifo_order_remaining():
    engine = make_engine()
    first = submit(
        engine,
        cl_ord_id="A",
        side=Side.SELL,
        qty=5,
        price=100,
    )
    second = submit(
        engine,
        cl_ord_id="B",
        side=Side.SELL,
        qty=7,
        price=100,
    )

    submit(
        engine,
        cl_ord_id="HIT-A",
        side=Side.BUY,
        qty=5,
        price=100,
    )
    level = level_at(engine.books["TEST"], Side.SELL, 100)
    assert first.status == OrderStatus.FILLED
    assert level.qty == 7
    assert list(level.orders) == [second]

    submit(
        engine,
        cl_ord_id="HIT-B",
        side=Side.BUY,
        qty=3,
        price=100,
    )
    assert second.remaining_qty == 4
    assert level.qty == 4
    engine.books["TEST"].assert_invariants()
    assert_reference_quantities(engine.books["TEST"])


def test_partial_fill_retains_fifo_and_executes_remainder_first():
    engine = make_engine()
    first = submit(
        engine,
        cl_ord_id="A",
        side=Side.SELL,
        qty=10,
        price=100,
    )
    second = submit(
        engine,
        cl_ord_id="B",
        side=Side.SELL,
        qty=10,
        price=100,
    )

    submit(
        engine,
        cl_ord_id="HIT-4",
        side=Side.BUY,
        qty=4,
        price=100,
    )
    level = level_at(engine.books["TEST"], Side.SELL, 100)
    assert list(level.orders) == [first, second]

    submit(
        engine,
        cl_ord_id="HIT-8",
        side=Side.BUY,
        qty=8,
        price=100,
    )
    assert first.status == OrderStatus.FILLED
    assert second.filled_qty == 2
    assert second.remaining_qty == 8
    assert list(level.orders) == [second]
    assert level.qty == 8
    engine.books["TEST"].assert_invariants()


def test_strategy_and_market_category_accounting():
    engine = make_engine()
    strategy = submit(
        engine,
        cl_ord_id="STRATEGY",
        side=Side.SELL,
        qty=5,
        price=100,
        session_alias="strategy_bot",
    )
    market = submit(
        engine,
        cl_ord_id="MARKET",
        side=Side.SELL,
        qty=8,
        price=100,
        session_alias="liquidity_bot",
    )
    book = engine.books["TEST"]
    level = level_at(book, Side.SELL, 100)
    assert (level.qty, level.strategy_qty, level.market_qty) == (13, 5, 8)

    book.apply_fill(strategy, 2, 100)
    strategy.transition_to(OrderStatus.PARTIAL)
    assert (level.qty, level.strategy_qty, level.market_qty) == (11, 3, 8)

    book.apply_fill(market, 3, 100)
    market.transition_to(OrderStatus.PARTIAL)
    assert (level.qty, level.strategy_qty, level.market_qty) == (8, 3, 5)
    book.assert_invariants()
    assert_reference_quantities(book)

    assert cancel(engine, strategy) is None
    assert cancel(engine, market) is None
    assert not book.asks
    book.assert_invariants()


def test_cancellation_is_idempotent():
    engine = make_engine()
    survivor = submit(
        engine,
        cl_ord_id="SURVIVOR",
        side=Side.BUY,
        qty=7,
        price=99,
    )
    cancelled = submit(
        engine,
        cl_ord_id="CANCEL-TWICE",
        side=Side.BUY,
        qty=5,
        price=100,
    )
    command = CancelOrderCommand(
        cancel_cl_ord_id="CXL-TWICE",
        orig_cl_ord_id=cancelled.cl_ord_id,
        symbol=cancelled.symbol,
        side=cancelled.side,
        qty=cancelled.qty,
        requested_at=NOW,
        session_id=cancelled.session_id or "TEST-SESSION",
    )

    assert isinstance(engine.cancel_order(command), ExecutionReport)
    before = [
        (level.price, level.qty, tuple(order.id for order in level.orders))
        for level in engine.books["TEST"].bids
    ]
    reason = engine.cancel_order(command)
    after = [
        (level.price, level.qty, tuple(order.id for order in level.orders))
        for level in engine.books["TEST"].bids
    ]

    assert "Stale cancel command" in reason
    assert after == before == [(99, 7, (survivor.id,))]
    engine.books["TEST"].assert_invariants()


def test_overfill_is_rejected_without_any_partial_mutation():
    book = OrderBook("TEST")
    order = Order(
        id=1,
        cl_ord_id="OVERFILL",
        symbol="TEST",
        side=Side.SELL,
        qty=5,
        price=100,
        tif=TimeInForce.GTC,
        ts_submitted=NOW,
        status=OrderStatus.NEW,
    )
    book.add_order(order)
    level = book.asks[0]

    with pytest.raises(InvalidBookMutation, match="Overfill"):
        book.apply_fill(order, 6, 100)

    assert order.filled_qty == 0
    assert order.remaining_qty == 5
    assert order.avg_fill_price is None
    assert level.qty == 5
    assert list(level.orders) == [order]
    assert book.is_resting(order.id)
    book.assert_invariants()


def test_duplicate_insertion_is_rejected_without_double_counting():
    book = OrderBook("TEST")
    order = Order(
        id=1,
        cl_ord_id="DUPLICATE",
        symbol="TEST",
        side=Side.BUY,
        qty=5,
        price=100,
        tif=TimeInForce.GTC,
        ts_submitted=NOW,
        status=OrderStatus.NEW,
    )
    book.add_order(order)

    with pytest.raises(InvalidBookMutation, match="Duplicate order id 1"):
        book.add_order(order)

    assert list(book.bids[0].orders) == [order]
    assert book.bids[0].qty == 5
    book.assert_invariants()


@pytest.mark.parametrize(
    ("resting_side", "aggressive_side"),
    ((Side.SELL, Side.BUY), (Side.BUY, Side.SELL)),
)
def test_partial_full_and_cancel_accounting_on_both_sides(
    resting_side,
    aggressive_side,
):
    engine = make_engine()
    partial = submit(
        engine,
        cl_ord_id=f"PARTIAL-{resting_side.value}",
        side=resting_side,
        qty=5,
        price=100,
    )
    submit(
        engine,
        cl_ord_id=f"HIT-PARTIAL-{resting_side.value}",
        side=aggressive_side,
        qty=2,
        price=100,
    )
    book = engine.books["TEST"]
    assert partial.remaining_qty == 3
    assert level_at(book, resting_side, 100).qty == 3
    assert cancel(engine, partial) is None
    book.assert_invariants()

    full = submit(
        engine,
        cl_ord_id=f"FULL-{resting_side.value}",
        side=resting_side,
        qty=4,
        price=101 if resting_side == Side.SELL else 99,
    )
    submit(
        engine,
        cl_ord_id=f"HIT-FULL-{resting_side.value}",
        side=aggressive_side,
        qty=4,
        price=101 if resting_side == Side.SELL else 99,
    )
    assert full.status == OrderStatus.FILLED
    assert not book.is_resting(full.id)
    book.assert_invariants()


def test_multi_price_match_removes_empty_levels_and_keeps_remainder():
    engine = make_engine()
    for price, qty in ((100, 3), (101, 4), (102, 5)):
        submit(
            engine,
            cl_ord_id=f"ASK-{price}",
            side=Side.SELL,
            qty=qty,
            price=price,
        )

    submit(
        engine,
        cl_ord_id="WALK-BOOK",
        side=Side.BUY,
        qty=8,
        price=102,
    )

    book = engine.books["TEST"]
    assert [(level.price, level.qty) for level in book.asks] == [(102, 4)]
    assert book.get_best_ask() == 102
    assert all(level.qty > 0 and level.orders for level in book.asks)
    book.assert_invariants()
    assert_reference_quantities(book)


def test_reports_observe_only_complete_book_and_lifecycle_state():
    engine = make_engine()
    observations = []

    def observe(report):
        book = engine.books["TEST"]
        book.assert_invariants()
        observations.append(
            (
                report.cl_ord_id,
                report.exec_type,
                book.is_resting(report.order_id),
                report.leaves_qty,
            )
        )

    engine.on_execution_report = observe
    resting = submit(
        engine,
        cl_ord_id="PASSIVE",
        side=Side.SELL,
        qty=10,
        price=100,
    )
    submit(
        engine,
        cl_ord_id="AGGRESSOR",
        side=Side.BUY,
        qty=4,
        price=100,
    )

    assert ("PASSIVE", "NEW", True, 10) in observations
    assert ("PASSIVE", "TRADE", True, 6) in observations
    assert ("AGGRESSOR", "TRADE", False, 0) in observations
    assert resting.remaining_qty == 6


def test_final_order_removal_excludes_zero_level_from_snapshot():
    engine = make_engine()
    resting = submit(
        engine,
        cl_ord_id="SNAPSHOT",
        side=Side.BUY,
        qty=5,
        price=99,
    )
    assert cancel(engine, resting) is None

    snapshot = engine.get_order_book_snapshot("TEST")
    assert snapshot is not None
    assert snapshot.get_best_bid() is None
    assert list(snapshot.bids) == []
    snapshot.assert_invariants()

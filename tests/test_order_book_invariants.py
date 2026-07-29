from tests.exact_helpers import exact_engine, exact_metrics
from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""Invariant corruption and deterministic stateful order-book tests."""

from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
import random

from hypothesis import given, settings, strategies as st
import pytest

from src.mockx.engine import MatchingEngine
from src.mockx.instruments import Instrument, InstrumentCatalog
from src.mockx.latency import LatencyScheduler
from src.mockx.models import (
    BookLevel,
    CancelOrderCommand,
    ExecutionParams,
    NewOrderCommand,
    Order,
    OrderBook,
    OrderBookInvariantError,
    OrderStatus,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)
RANDOM_SEED = 12345
RANDOM_OPERATION_COUNT = 500
COMMAND_OPERATION = st.one_of(
    st.tuples(
        st.just("submit"),
        st.sampled_from((Side.BUY, Side.SELL)),
        st.integers(min_value=1, max_value=20),
        st.one_of(
            st.none(),
            st.integers(min_value=95, max_value=105),
        ),
        st.sampled_from((TimeInForce.GTC, TimeInForce.IOC)),
    ),
    st.tuples(
        st.just("cancel"),
        st.integers(min_value=0, max_value=100),
    ),
)


def make_order(
    order_id: int,
    *,
    side: Side = Side.BUY,
    qty: int = 5,
    price: int = 100,
    alias: str = "market_bot",
) -> Order:
    return exact_order(
        id=order_id,
        cl_ord_id=f"ORDER-{order_id}",
        participant_id=f"ORDER-{order_id}",
        symbol="TEST",
        side=side,
        order_qty_lots=qty,
        price_ticks=price,
        tif=TimeInForce.GTC,
        ts_submitted=NOW,
        status=OrderStatus.NEW,
        session_alias=alias,
    )


def assert_independent_reference(book: OrderBook) -> None:
    for level in (*book.bids, *book.asks):
        total = sum(order.leaves_qty_lots for order in level.orders)
        strategy = sum(
            order.leaves_qty_lots
            for order in level.orders
            if order.session_alias == "strategy_bot"
        )
        market = sum(
            order.leaves_qty_lots
            for order in level.orders
            if order.session_alias != "strategy_bot"
        )
        assert level.level_qty_lots == total
        assert level.strategy_qty_lots == pytest.approx(strategy, abs=1e-9)
        assert level.market_qty_lots == pytest.approx(market, abs=1e-9)


def test_invariant_checker_detects_corrupt_cached_total_with_context():
    book = OrderBook("TEST")
    book.add_order(make_order(1))
    book.bids[0].level_qty_lots += 1

    with pytest.raises(
        OrderBookInvariantError,
        match=(
            r"Level category mismatch at Side.BUY 100: "
            r"level_qty_lots=6, strategy_qty_lots=0, market_qty_lots=5"
        ),
    ):
        book.assert_invariants()


def test_invariant_checker_detects_duplicate_order_ids():
    book = OrderBook("TEST")
    first = make_order(1, qty=5)
    duplicate_id = make_order(1, qty=3)
    book.add_order(first)
    level = book.bids[0]
    level.orders.append(duplicate_id)
    level.level_qty_lots += 3
    level.market_qty_lots += 3

    with pytest.raises(OrderBookInvariantError, match="Duplicate order id 1"):
        book.assert_invariants()


def test_invariant_checker_detects_wrong_category_total():
    book = OrderBook("TEST")
    book.add_order(make_order(1, alias="strategy_bot"))
    level = book.bids[0]
    level.strategy_qty_lots -= 1
    level.market_qty_lots += 1

    with pytest.raises(
        OrderBookInvariantError,
        match="Level aggregate mismatch",
    ):
        book.assert_invariants()


def test_invariant_checker_detects_retained_empty_level():
    book = OrderBook("TEST")
    book.bids.append(
        BookLevel(
            price_ticks=100,
            level_qty_lots=0,
            orders=deque(),
            side=Side.BUY,
        )
    )

    with pytest.raises(
        OrderBookInvariantError,
        match="Empty price level retained",
    ):
        book.assert_invariants()


@pytest.mark.parametrize(
    ("best_bid", "best_ask"),
    (
        (101, 100),
        (100, 100),
    ),
)
def test_invariant_checker_detects_crossed_or_locked_book(
    best_bid,
    best_ask,
):
    book = OrderBook("TEST")
    book.add_order(make_order(1, side=Side.BUY, price=best_bid))
    book.add_order(make_order(2, side=Side.SELL, price=best_ask))

    with pytest.raises(
        OrderBookInvariantError,
        match=(
            rf"Crossed or locked book in TEST: "
            rf"best_bid={best_bid}, best_ask={best_ask}"
        ),
    ):
        book.assert_invariants()


def make_random_engine() -> MatchingEngine:
    instruments = InstrumentCatalog(
        {
            symbol: Instrument(
                symbol=symbol,
                tick_size=Decimal("1"),
                lot_size=Decimal("1"),
                price_display_precision=2,
                quantity_display_precision=3,
            )
            for symbol in ("ALPHA", "BETA")
        }
    )
    return exact_engine(
        ExecutionParams(
            network_latency_ms=0,
            engine_latency_ms=0,
            reject_prob=0.0,
            rand_seed=777,
        ),
        LatencyScheduler(),
        instruments,
    )


def test_fixed_seed_randomized_operations_preserve_all_invariants():
    rng = random.Random(RANDOM_SEED)
    engine = make_random_engine()
    history = []

    for step in range(RANDOM_OPERATION_COUNT):
        symbol = rng.choice(("ALPHA", "BETA"))
        action = rng.choices(
            ("rest", "cross", "cancel", "repeat_cancel"),
            weights=(45, 30, 20, 5),
            k=1,
        )[0]

        if action in {"cancel", "repeat_cancel"}:
            candidates = [
                order
                for order in engine.orders.values()
                if (
                    order.symbol == symbol
                    and (
                        action == "repeat_cancel"
                        or engine.books[symbol].is_resting(order.id)
                    )
                )
            ]
            if candidates:
                order = rng.choice(candidates)
                command = exact_cancel_command(
                    cancel_cl_ord_id=f"RND-CXL-{step}",
                    orig_cl_ord_id=order.cl_ord_id,
                    symbol=order.symbol,
                    side=order.side,
                    order_qty_lots=order.order_qty_lots,
                    requested_at=NOW,
                    session_id=order.session_id or "INVARIANT-SESSION",
                    session_alias=order.session_alias,
                )
                first_result = engine.cancel_order(command)
                history.append(
                    (
                        step,
                        action,
                        symbol,
                        order.id,
                        first_result,
                    )
                )
                if action == "repeat_cancel":
                    second_result = engine.cancel_order(command)
                    history.append(
                        (
                            step,
                            "repeat_result",
                            symbol,
                            order.id,
                            second_result,
                        )
                    )
            else:
                action = "rest"

        if action in {"rest", "cross"}:
            side = rng.choice((Side.BUY, Side.SELL))
            qty = rng.randint(1, 12)
            if action == "rest":
                price = (
                    rng.randint(95, 99)
                    if side == Side.BUY
                    else rng.randint(101, 105)
                )
            else:
                price = 110 if side == Side.BUY else 90
            alias = rng.choice(
                ("strategy_bot", "liquidity_bot", "noise_bot")
            )
            order_id = engine.submit_order(
                exact_command(
                    cl_ord_id=f"RND-{step}-{action}",
                    participant_id=f"RND-{step}-{action}",
                    symbol=symbol,
                    side=side,
                    order_qty_lots=qty,
                    price_ticks=price,
                    tif=TimeInForce.GTC,
                    submitted_at=NOW,
                    session_id="INVARIANT-SESSION",
                    session_alias=alias,
                )
            )
            engine.process_order(
                ProcessOrderCommand(order_id, expected_version=0)
            )
            history.append(
                (step, action, symbol, order_id, side.value, qty, price, alias)
            )

        try:
            for current_symbol, book in engine.books.items():
                book.assert_invariants()
                assert_independent_reference(book)
                assert all(
                    level.orders and level.level_qty_lots > 0
                    for level in (*book.bids, *book.asks)
                ), current_symbol
        except Exception as exc:
            recent_history = history[-30:]
            raise AssertionError(
                f"Randomized invariant failure: seed={RANDOM_SEED}, "
                f"step={step}, operations={RANDOM_OPERATION_COUNT}, "
                f"recent_history={recent_history}"
            ) from exc


@settings(max_examples=75, deadline=None)
@given(st.lists(COMMAND_OPERATION, min_size=1, max_size=50))
def test_command_sequences_never_leave_crossed_or_locked_book(operations):
    engine = exact_engine(
        ExecutionParams(
            network_latency_ms=0,
            engine_latency_ms=0,
            reject_prob=0.0,
            rand_seed=777,
        ),
        LatencyScheduler(),
    )

    for step, operation in enumerate(operations):
        if operation[0] == "submit":
            _, side, qty, price, tif = operation
            order_id = engine.submit_order(
                exact_command(
                    cl_ord_id=f"PROPERTY-{step}",
                    participant_id=f"PROPERTY-{step}",
                    symbol="TEST",
                    side=side,
                    order_qty_lots=qty,
                    price_ticks=price,
                    tif=tif,
                    submitted_at=NOW,
                    session_id="PROPERTY-SESSION",
                    session_alias="property_test",
                )
            )
            engine.process_order(
                ProcessOrderCommand(order_id, expected_version=0)
            )
        else:
            _, selector = operation
            book = engine.books["TEST"]
            resting_ids = sorted(
                order.id
                for order in engine.orders.values()
                if book.is_resting(order.id)
            )
            if resting_ids:
                order_id = resting_ids[selector % len(resting_ids)]
                order = engine.orders[order_id]
                engine.cancel_order(
                    exact_cancel_command(
                        cancel_cl_ord_id=f"PROPERTY-CXL-{step}",
                        orig_cl_ord_id=order.cl_ord_id,
                        symbol=order.symbol,
                        side=order.side,
                        order_qty_lots=order.order_qty_lots,
                        requested_at=NOW,
                        session_id=order.session_id or "PROPERTY-SESSION",
                        session_alias=order.session_alias,
                    )
                )

        book = engine.books["TEST"]
        book.assert_invariants()
        assert_independent_reference(book)

        best_bid = book.get_best_bid_ticks()
        best_ask = book.get_best_ask_ticks()
        if best_bid is not None and best_ask is not None:
            assert best_bid < best_ask

        for level in (*book.bids, *book.asks):
            for order in level.orders:
                assert order.status in {
                    OrderStatus.NEW,
                    OrderStatus.PARTIAL,
                }

        for order in engine.orders.values():
            if order.status in {
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
            }:
                assert not book.is_resting(order.id)
            if order.price_ticks is None or order.tif == TimeInForce.IOC:
                assert not book.is_resting(order.id)

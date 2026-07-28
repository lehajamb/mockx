"""Core data models for the mock exchange."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, List, Dict, FrozenSet, Mapping, Tuple, Union
from enum import Enum
from types import MappingProxyType
import math


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderStatus(Enum):
    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

class TimeInForce(Enum):
    GTC = "GTC"  # Good Till Cancel
    IOC = "IOC"  # Immediate Or Cancel


class ExecutionType(str, Enum):
    """Business event represented by an execution report."""

    NEW = "NEW"
    TRADE = "TRADE"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class CancelRejectReason(str, Enum):
    """Stable domain reasons for rejecting an order-cancel request."""

    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    TOO_LATE_TO_CANCEL = "TOO_LATE_TO_CANCEL"
    ALREADY_CANCELED = "ALREADY_CANCELED"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    INVALID_REQUEST = "INVALID_REQUEST"


class InvalidOrderTransition(RuntimeError):
    """Raised when engine code attempts an unsupported lifecycle transition."""


class OrderBookInvariantError(RuntimeError):
    """Raised when cached book state disagrees with its live orders."""


class InvalidBookMutation(OrderBookInvariantError):
    """Raised when a requested order-book mutation is invalid."""


class SchedulerState(str, Enum):
    """Explicit lifecycle state for one scheduler generation."""

    RUNNING = "RUNNING"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class SchedulerFailureStatus(str, Enum):
    """Stable reason describing why a scheduled event was dead-lettered."""

    HANDLER_EXCEPTION = "HANDLER_EXCEPTION"
    HANDLER_NOT_REGISTERED = "HANDLER_NOT_REGISTERED"
    EVENT_PROCESSING_LIMIT_EXCEEDED = "EVENT_PROCESSING_LIMIT_EXCEEDED"


QUANTITY_EPSILON = 1e-9
"""Tolerance for float quantity comparisons.

Integer lots remain a future improvement; quantities are intentionally kept as
floats in this remediation to preserve the existing public model.
"""


def _quantities_equal(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=QUANTITY_EPSILON)


def _normalize_quantity(value: float, *, context: str) -> float:
    if not math.isfinite(value):
        raise OrderBookInvariantError(f"{context} is not finite: {value}")
    if value < -QUANTITY_EPSILON:
        raise OrderBookInvariantError(f"{context} is negative: {value}")
    return 0.0 if abs(value) <= QUANTITY_EPSILON else value


TERMINAL_ORDER_STATUSES: FrozenSet[OrderStatus] = frozenset({
    OrderStatus.FILLED,
    OrderStatus.REJECTED,
    OrderStatus.CANCELLED,
})


ORDER_STATE_TRANSITIONS: Mapping[OrderStatus, FrozenSet[OrderStatus]] = MappingProxyType({
    OrderStatus.PENDING_NEW: frozenset({
        OrderStatus.NEW,
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
    }),
    OrderStatus.NEW: frozenset({
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
    }),
    OrderStatus.PARTIAL: frozenset({
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
    }),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
})


ORDER_STATUSES_BY_EXECUTION_TYPE: Mapping[
    ExecutionType, FrozenSet[OrderStatus]
] = MappingProxyType({
    ExecutionType.NEW: frozenset({OrderStatus.NEW}),
    ExecutionType.TRADE: frozenset({
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
    }),
    ExecutionType.CANCELED: frozenset({OrderStatus.CANCELLED}),
    ExecutionType.REJECTED: frozenset({OrderStatus.REJECTED}),
})


class LiquidityCategory(Enum):
    """Stable origin category used by price-level accounting."""

    STRATEGY = "strategy"
    MARKET = "market"


STRATEGY_SESSION_ALIAS = "strategy_bot"


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """Serializable identity for one FIX session.

    Transport-specific objects are deliberately excluded.  The stable
    ``session_id`` is the only routing key; component IDs and aliases are
    descriptive metadata.
    """

    session_id: str
    sender_comp_id: Optional[str] = None
    target_comp_id: Optional[str] = None
    alias: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("Session identity requires a stable session_id")


@dataclass(frozen=True, slots=True)
class ClientOrderKey:
    """A client order identifier scoped to its owning FIX session."""

    session_id: str
    cl_ord_id: str

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("ClientOrderKey requires session_id")
        if not self.cl_ord_id:
            raise ValueError("ClientOrderKey requires ClOrdID")


def liquidity_category_for_session(
    session_alias: Optional[str],
) -> LiquidityCategory:
    """Classify immutable order origin metadata in one place."""
    if session_alias == STRATEGY_SESSION_ALIAS:
        return LiquidityCategory.STRATEGY
    return LiquidityCategory.MARKET


@dataclass(frozen=True, slots=True)
class NewOrderCommand:
    """Immutable client intent used to create an internal order aggregate."""

    cl_ord_id: str
    symbol: str
    side: Side
    qty: float
    price: Optional[float]
    tif: TimeInForce
    submitted_at: datetime
    session_id: Optional[str] = None
    session_alias: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.cl_ord_id:
            raise ValueError("ClOrdID is required")
        if not self.symbol:
            raise ValueError("Symbol is required")
        if not math.isfinite(self.qty) or self.qty <= QUANTITY_EPSILON:
            raise ValueError("Order quantity must be positive and finite")
        if self.price is not None and (
            not math.isfinite(self.price) or self.price <= 0.0
        ):
            raise ValueError("Limit price must be positive and finite")
        if not isinstance(self.tif, TimeInForce):
            raise ValueError(f"Unsupported TimeInForce: {self.tif}")


@dataclass(frozen=True, slots=True)
class ProcessOrderCommand:
    """Immutable token authorizing one generation of initial order processing."""

    order_id: int
    expected_version: int


@dataclass(frozen=True, slots=True)
class CancelOrderCommand:
    """Immutable, replayable cancel intent without FIX or Order references."""

    order_id: int
    expected_version: int
    cancel_cl_ord_id: Optional[str]
    orig_cl_ord_id: Optional[str]
    requested_at: datetime
    session_id: Optional[str] = None
    session_alias: Optional[str] = None


@dataclass(frozen=True, slots=True)
class CancelReject:
    """Immutable cancel-rejection snapshot without live order/FIX objects."""

    cancel_cl_ord_id: str
    orig_cl_ord_id: str
    order_id: Optional[int]
    ord_status: Optional[OrderStatus]
    reason: CancelRejectReason
    text: str
    session_id: Optional[str]
    session_alias: Optional[str]
    timestamp: datetime
    exchange_order_id: Optional[str] = None

    def __contains__(self, value: str) -> bool:
        """Keep legacy ``"text" in result`` callers source-compatible."""
        return value in self.text

    def __str__(self) -> str:
        return self.text


@dataclass(slots=True)
class Order:
    """Mutable engine-owned order aggregate.

    ``version`` advances when a lifecycle command invalidates queued work.
    Initial processing is additionally protected by ``processed`` so replaying
    an otherwise-current command is idempotent.
    """

    id: int
    cl_ord_id: str
    symbol: str
    side: Side
    qty: float
    price: Optional[float]  # None = market order
    tif: TimeInForce
    ts_submitted: datetime  # Exchange timestamp
    status: OrderStatus = OrderStatus.PENDING_NEW
    filled_qty: float = 0.0
    canceled_qty: float = 0.0
    avg_fill_price: Optional[float] = None
    reject_reason: Optional[str] = None
    session_id: Optional[str] = None
    session_alias: Optional[str] = None
    exchange_order_id: Optional[str] = None
    run_generation: int = 0
    version: int = 0
    processed: bool = False
    _liquidity_category: LiquidityCategory = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.cl_ord_id:
            raise ValueError("ClOrdID is required")
        if not self.symbol:
            raise ValueError("Symbol is required")
        if not math.isfinite(self.qty) or self.qty <= QUANTITY_EPSILON:
            raise ValueError("Order quantity must be positive and finite")
        if self.price is not None and (
            not math.isfinite(self.price) or self.price <= 0.0
        ):
            raise ValueError("Limit price must be positive and finite")
        if not isinstance(self.tif, TimeInForce):
            raise ValueError(f"Unsupported TimeInForce: {self.tif}")

        # Capture the origin once. Execution status and later alias changes must
        # never move resting quantity between accounting categories.
        self._liquidity_category = liquidity_category_for_session(
            self.session_alias
        )

    @property
    def liquidity_category(self) -> LiquidityCategory:
        return self._liquidity_category

    @property
    def is_strategy_order(self) -> bool:
        return self._liquidity_category == LiquidityCategory.STRATEGY

    @property
    def remaining_qty(self) -> float:
        """Return live working quantity after fills and terminal cancellation."""
        if not all(
            math.isfinite(value)
            for value in (self.qty, self.filled_qty, self.canceled_qty)
        ):
            raise OrderBookInvariantError(
                f"Order {self.id} has non-finite quantities: "
                f"qty={self.qty}, filled_qty={self.filled_qty}, "
                f"canceled_qty={self.canceled_qty}"
            )
        if self.qty <= QUANTITY_EPSILON:
            raise OrderBookInvariantError(
                f"Order {self.id} has non-positive quantity: {self.qty}"
            )
        if self.filled_qty < -QUANTITY_EPSILON:
            raise OrderBookInvariantError(
                f"Order {self.id} has negative filled quantity: "
                f"{self.filled_qty}"
            )
        if self.canceled_qty < -QUANTITY_EPSILON:
            raise OrderBookInvariantError(
                f"Order {self.id} has negative canceled quantity: "
                f"{self.canceled_qty}"
            )
        if (
            self.filled_qty + self.canceled_qty
            > self.qty + QUANTITY_EPSILON
        ):
            raise OrderBookInvariantError(
                f"Order {self.id} is over-allocated: qty={self.qty}, "
                f"filled_qty={self.filled_qty}, "
                f"canceled_qty={self.canceled_qty}"
            )
        remaining = self.qty - self.filled_qty - self.canceled_qty
        return _normalize_quantity(
            remaining,
            context=f"Order {self.id} remaining quantity",
        )

    def _preview_fill(
        self,
        fill_qty: float,
        fill_price: float,
    ) -> Tuple[float, float, float]:
        if not math.isfinite(fill_qty) or fill_qty <= 0.0:
            raise InvalidBookMutation(
                f"Fill quantity for order {self.id} must be positive and "
                f"finite: {fill_qty}"
            )
        if not math.isfinite(fill_price):
            raise InvalidBookMutation(
                f"Fill price for order {self.id} must be finite: {fill_price}"
            )

        old_remaining = self.remaining_qty
        if fill_qty > old_remaining + QUANTITY_EPSILON:
            raise InvalidBookMutation(
                f"Overfill for order {self.id}: fill_qty={fill_qty}, "
                f"remaining_qty={old_remaining}"
            )

        applied_qty = min(fill_qty, old_remaining)
        new_filled_qty = self.filled_qty + applied_qty
        if _quantities_equal(new_filled_qty, self.qty):
            new_filled_qty = self.qty
        new_remaining_qty = _normalize_quantity(
            self.qty - new_filled_qty - self.canceled_qty,
            context=f"Order {self.id} post-fill remaining quantity",
        )

        if self.avg_fill_price is None:
            new_avg_fill_price = fill_price
        else:
            total_value = (
                self.avg_fill_price * self.filled_qty
            ) + (fill_price * applied_qty)
            new_avg_fill_price = total_value / new_filled_qty

        return new_filled_qty, new_avg_fill_price, new_remaining_qty

    def validate_fill(self, fill_qty: float, fill_price: float) -> None:
        """Validate a fill without changing the order."""
        self._preview_fill(fill_qty, fill_price)

    def apply_fill(self, fill_qty: float, fill_price: float) -> float:
        """Atomically update cumulative quantity and average fill price."""
        old_remaining = self.remaining_qty
        new_filled_qty, new_avg_fill_price, new_remaining_qty = (
            self._preview_fill(fill_qty, fill_price)
        )
        self.filled_qty = new_filled_qty
        self.avg_fill_price = new_avg_fill_price
        return old_remaining - new_remaining_qty

    def cancel_remaining(self) -> float:
        """Move the complete live remainder into terminal canceled quantity."""
        canceled = self.remaining_qty
        if canceled <= QUANTITY_EPSILON:
            return 0.0
        self.canceled_qty += canceled
        if _quantities_equal(
            self.filled_qty + self.canceled_qty,
            self.qty,
        ):
            self.canceled_qty = self.qty - self.filled_qty
        return canceled

    def validate_transition_to(self, new_status: OrderStatus) -> None:
        """Validate one lifecycle transition without changing the order."""
        if new_status not in ORDER_STATE_TRANSITIONS[self.status]:
            raise InvalidOrderTransition(
                f"Invalid order transition for order {self.id}: "
                f"{self.status.value} -> {new_status.value}"
            )

    def transition_to(self, new_status: OrderStatus) -> None:
        """Apply one explicitly supported order lifecycle transition."""
        self.validate_transition_to(new_status)
        self.status = new_status

@dataclass(frozen=True, slots=True)
class ExecutionParams:
    """Configuration for execution behavior."""
    network_latency_ms: int  # Round-trip
    engine_latency_ms: int
    reject_prob: float  # 0..1
    rand_seed: int  # For deterministic replay
    inbound_network_latency_ms: Optional[int] = None
    outbound_network_latency_ms: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.rand_seed, int) or isinstance(
            self.rand_seed,
            bool,
        ):
            raise ValueError("Random seed must be an integer")
        delays = (
            self.network_latency_ms,
            self.engine_latency_ms,
            self.inbound_network_latency_ms,
            self.outbound_network_latency_ms,
        )
        if any(value is not None and value < 0 for value in delays):
            raise ValueError("Configured latencies cannot be negative")

    @property
    def simulated_inbound_latency_ms(self) -> int:
        if self.inbound_network_latency_ms is not None:
            return self.inbound_network_latency_ms
        return self.network_latency_ms // 2

    @property
    def simulated_outbound_latency_ms(self) -> int:
        if self.outbound_network_latency_ms is not None:
            return self.outbound_network_latency_ms
        return self.network_latency_ms // 2

@dataclass(frozen=True, slots=True)
class Fill:
    """One immutable interaction between an aggressor and a passive order."""

    trade_id: int
    symbol: str
    price: float
    qty: float
    aggressor_order_id: int
    passive_order_id: int
    aggressor_side: Side
    timestamp: datetime
    exchange_trade_id: Optional[str] = None
    run_generation: int = 0

    def __post_init__(self) -> None:
        if self.trade_id <= 0:
            raise ValueError("Fill TradeID must be positive")
        if not self.symbol:
            raise ValueError("Fill symbol is required")
        if not math.isfinite(self.price) or self.price <= 0.0:
            raise ValueError("Fill price must be positive and finite")
        if not math.isfinite(self.qty) or self.qty <= QUANTITY_EPSILON:
            raise ValueError("Fill quantity must be positive and finite")
        if (
            self.aggressor_order_id <= 0
            or self.passive_order_id <= 0
            or self.aggressor_order_id == self.passive_order_id
        ):
            raise ValueError("Fill requires two distinct positive order IDs")


@dataclass(frozen=True, slots=True)
class Trade:
    """Represents an executed trade."""
    id: int
    symbol: str
    price: float
    qty: float
    volume: float
    buyer_order_id: int
    seller_order_id: int
    timestamp: datetime
    level: int
    buyer_session_alias: Optional[str]
    seller_session_alias: Optional[str]
    aggressor_order_id: int
    passive_order_id: int
    aggressor_side: Side
    exchange_trade_id: Optional[str] = None
    run_generation: int = 0

@dataclass(frozen=True, slots=True)
class BookFillResult:
    """Result of one authoritative fill mutation on a resting order."""

    order_id: int
    fill_qty: float
    remaining_qty: float
    fully_filled: bool
    level_removed: bool


@dataclass
class BookLevel:
    """Single FIFO price level with incrementally maintained quantities."""

    price: float
    qty: float = 0.0
    orders: deque[Order] = field(default_factory=deque)
    strategy_qty: float = 0.0
    market_qty: float = 0.0
    side: Optional[Side] = None

    def _validate_cached_aggregates(self) -> None:
        values = {
            "qty": self.qty,
            "strategy_qty": self.strategy_qty,
            "market_qty": self.market_qty,
        }
        for name, value in values.items():
            _normalize_quantity(
                value,
                context=f"Level {self.side} {self.price} {name}",
            )
        if not _quantities_equal(
            self.qty,
            self.strategy_qty + self.market_qty,
        ):
            raise OrderBookInvariantError(
                f"Level {self.side} {self.price} category mismatch: "
                f"qty={self.qty}, strategy_qty={self.strategy_qty}, "
                f"market_qty={self.market_qty}"
            )

    def _updated_aggregates(
        self,
        order: Order,
        delta: float,
    ) -> Tuple[float, float, float]:
        """Preview an aggregate delta; positive adds and negative subtracts."""
        self._validate_cached_aggregates()
        new_qty = _normalize_quantity(
            self.qty + delta,
            context=f"Level {self.side} {self.price} qty after mutation",
        )
        new_strategy_qty = self.strategy_qty
        new_market_qty = self.market_qty
        if order.liquidity_category == LiquidityCategory.STRATEGY:
            new_strategy_qty = _normalize_quantity(
                self.strategy_qty + delta,
                context=(
                    f"Level {self.side} {self.price} strategy_qty "
                    "after mutation"
                ),
            )
        else:
            new_market_qty = _normalize_quantity(
                self.market_qty + delta,
                context=(
                    f"Level {self.side} {self.price} market_qty "
                    "after mutation"
                ),
            )

        if not _quantities_equal(
            new_qty,
            new_strategy_qty + new_market_qty,
        ):
            raise OrderBookInvariantError(
                f"Level {self.side} {self.price} post-mutation category "
                f"mismatch for order {order.id}: qty={new_qty}, "
                f"strategy_qty={new_strategy_qty}, "
                f"market_qty={new_market_qty}"
            )
        return new_qty, new_strategy_qty, new_market_qty

    def _order_index(self, order: Order) -> Optional[int]:
        for index, queued_order in enumerate(self.orders):
            if queued_order is order:
                return index
        return None

    def _validate_level_lifecycle(
        self,
        aggregates: Tuple[float, float, float],
        remaining_order_count: int,
        order_id: int,
    ) -> None:
        qty, strategy_qty, market_qty = aggregates
        if remaining_order_count == 0:
            if not all(
                _quantities_equal(value, 0.0)
                for value in (qty, strategy_qty, market_qty)
            ):
                raise OrderBookInvariantError(
                    f"Final order {order_id} would leave nonzero level "
                    f"aggregates: side={self.side}, price={self.price}, "
                    f"qty={qty}, strategy_qty={strategy_qty}, "
                    f"market_qty={market_qty}"
                )
        elif qty <= QUANTITY_EPSILON:
            raise OrderBookInvariantError(
                f"Mutation of order {order_id} would leave a zero-quantity "
                f"level with {remaining_order_count} live orders: "
                f"side={self.side}, price={self.price}, qty={qty}"
            )

    def _validate_resting_order(self, order: Order) -> float:
        if order.status in TERMINAL_ORDER_STATUSES:
            raise InvalidBookMutation(
                f"Terminal order {order.id} cannot rest at "
                f"{self.side} {self.price}: {order.status.value}"
            )
        if order.status not in {
            OrderStatus.PENDING_NEW,
            OrderStatus.NEW,
            OrderStatus.PARTIAL,
        }:
            raise InvalidBookMutation(
                f"Order {order.id} is not eligible to rest in state "
                f"{order.status.value}"
            )
        if order.price is None or not math.isfinite(order.price):
            raise InvalidBookMutation(
                f"Resting order {order.id} must have a finite limit price"
            )
        if order.price != self.price:
            raise InvalidBookMutation(
                f"Order {order.id} price {order.price} does not match "
                f"level price {self.price}"
            )
        if self.side is not None and order.side != self.side:
            raise InvalidBookMutation(
                f"Order {order.id} side {order.side.value} does not match "
                f"level side {self.side.value}"
            )
        remaining_qty = order.remaining_qty
        if remaining_qty <= QUANTITY_EPSILON:
            raise InvalidBookMutation(
                f"Order {order.id} has no positive remaining quantity"
            )
        return remaining_qty

    def add_order(self, order: Order) -> None:
        """Append a live order exactly once and update one category."""
        remaining_qty = self._validate_resting_order(order)
        if any(queued.id == order.id for queued in self.orders):
            raise InvalidBookMutation(
                f"Duplicate order id {order.id} at "
                f"{self.side or order.side} {self.price}"
            )
        if not self.orders and any(
            not _quantities_equal(value, 0.0)
            for value in (self.qty, self.strategy_qty, self.market_qty)
        ):
            raise OrderBookInvariantError(
                f"Cannot add order {order.id} to an empty level with "
                f"nonzero aggregates: side={self.side}, price={self.price}, "
                f"qty={self.qty}, strategy_qty={self.strategy_qty}, "
                f"market_qty={self.market_qty}"
            )
        aggregates = self._updated_aggregates(order, remaining_qty)
        self._validate_level_lifecycle(
            aggregates,
            len(self.orders) + 1,
            order.id,
        )

        # All checks are complete; the remaining assignments cannot fail.
        if self.side is None:
            self.side = order.side
        self.orders.append(order)
        self.qty, self.strategy_qty, self.market_qty = aggregates

    def apply_fill(
        self,
        order: Order,
        fill_qty: float,
        fill_price: Optional[float] = None,
    ) -> Tuple[float, bool]:
        """Apply one fill and its aggregate delta as a single mutation."""
        queue_index = self._order_index(order)
        if queue_index is None:
            raise InvalidBookMutation(
                f"Order {order.id} is not resting at "
                f"{self.side} {self.price}"
            )
        self._validate_resting_order(order)
        resolved_price = order.price if fill_price is None else fill_price
        if resolved_price is None:
            raise InvalidBookMutation(
                f"Resting order {order.id} has no fill price"
            )

        old_remaining_qty = order.remaining_qty
        new_filled_qty, new_avg_fill_price, new_remaining_qty = (
            order._preview_fill(fill_qty, resolved_price)
        )
        applied_qty = old_remaining_qty - new_remaining_qty
        aggregates = self._updated_aggregates(order, -applied_qty)
        fully_filled = new_remaining_qty == 0.0
        self._validate_level_lifecycle(
            aggregates,
            len(self.orders) - int(fully_filled),
            order.id,
        )

        # Commit only after every order and aggregate precondition succeeds.
        order.filled_qty = new_filled_qty
        order.avg_fill_price = new_avg_fill_price
        self.qty, self.strategy_qty, self.market_qty = aggregates
        if fully_filled:
            del self.orders[queue_index]
        return applied_qty, fully_filled

    def remove_order(self, order: Order) -> bool:
        """Remove one live order using only its current remaining quantity."""
        queue_index = self._order_index(order)
        if queue_index is None:
            return False
        remaining_qty = self._validate_resting_order(order)
        aggregates = self._updated_aggregates(order, -remaining_qty)
        self._validate_level_lifecycle(
            aggregates,
            len(self.orders) - 1,
            order.id,
        )

        del self.orders[queue_index]
        self.qty, self.strategy_qty, self.market_qty = aggregates
        return True

    def assert_invariants(self, expected_side: Optional[Side] = None) -> None:
        """Detect, but never repair, price-level corruption."""
        side = expected_side or self.side
        if side is None and self.orders:
            side = self.orders[0].side
        if not self.orders:
            raise OrderBookInvariantError(
                f"Empty price level retained: side={side}, price={self.price}, "
                f"qty={self.qty}, strategy_qty={self.strategy_qty}, "
                f"market_qty={self.market_qty}"
            )
        if self.side is not None and side != self.side:
            raise OrderBookInvariantError(
                f"Level side mismatch at price {self.price}: "
                f"cached={self.side}, expected={side}"
            )

        seen_ids = set()
        expected_qty = 0.0
        expected_strategy_qty = 0.0
        expected_market_qty = 0.0
        for order in self.orders:
            if order.id in seen_ids:
                raise OrderBookInvariantError(
                    f"Duplicate order id {order.id}: side={side}, "
                    f"price={self.price}"
                )
            seen_ids.add(order.id)
            if order.status in TERMINAL_ORDER_STATUSES:
                raise OrderBookInvariantError(
                    f"Terminal order {order.id} retained: side={side}, "
                    f"price={self.price}, status={order.status.value}"
                )
            if order.status not in {
                OrderStatus.PENDING_NEW,
                OrderStatus.NEW,
                OrderStatus.PARTIAL,
            }:
                raise OrderBookInvariantError(
                    f"Non-live order {order.id} retained: side={side}, "
                    f"price={self.price}, status={order.status.value}"
                )
            remaining_qty = order.remaining_qty
            if remaining_qty <= QUANTITY_EPSILON:
                raise OrderBookInvariantError(
                    f"Zero-quantity order {order.id} retained: side={side}, "
                    f"price={self.price}, remaining_qty={remaining_qty}"
                )
            if order.price != self.price:
                raise OrderBookInvariantError(
                    f"Wrong-price order {order.id}: side={side}, "
                    f"level_price={self.price}, order_price={order.price}"
                )
            if side is not None and order.side != side:
                raise OrderBookInvariantError(
                    f"Wrong-side order {order.id}: level_side={side.value}, "
                    f"order_side={order.side.value}, price={self.price}"
                )

            expected_qty += remaining_qty
            if order.liquidity_category == LiquidityCategory.STRATEGY:
                expected_strategy_qty += remaining_qty
            else:
                expected_market_qty += remaining_qty

        self._validate_cached_aggregates()
        if not _quantities_equal(self.qty, expected_qty):
            raise OrderBookInvariantError(
                f"Level quantity mismatch: side={side}, price={self.price}, "
                f"cached_qty={self.qty}, calculated_qty={expected_qty}, "
                f"order_ids={sorted(seen_ids)}"
            )
        if not _quantities_equal(
            self.strategy_qty,
            expected_strategy_qty,
        ):
            raise OrderBookInvariantError(
                f"Strategy quantity mismatch: side={side}, price={self.price}, "
                f"cached_strategy_qty={self.strategy_qty}, "
                f"calculated_strategy_qty={expected_strategy_qty}, "
                f"order_ids={sorted(seen_ids)}"
            )
        if not _quantities_equal(self.market_qty, expected_market_qty):
            raise OrderBookInvariantError(
                f"Market quantity mismatch: side={side}, price={self.price}, "
                f"cached_market_qty={self.market_qty}, "
                f"calculated_market_qty={expected_market_qty}, "
                f"order_ids={sorted(seen_ids)}"
            )


@dataclass
class OrderBook:
    """Limit order book with authoritative mutation and lookup indexes."""

    symbol: str
    bids: deque[BookLevel] = field(default_factory=deque)
    asks: deque[BookLevel] = field(default_factory=deque)
    _order_index: Dict[int, Tuple[Side, BookLevel]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def add_order(self, order: Order) -> None:
        """Insert an eligible order at the correct FIFO price level."""
        if order.symbol != self.symbol:
            raise InvalidBookMutation(
                f"Order {order.id} symbol {order.symbol} does not match "
                f"book {self.symbol}"
            )
        if order.id in self._order_index:
            side, level = self._order_index[order.id]
            raise InvalidBookMutation(
                f"Duplicate order id {order.id}: already resting at "
                f"{side.value} {level.price}"
            )
        if order.tif != TimeInForce.GTC:
            raise InvalidBookMutation(
                f"IOC order {order.id} cannot rest in book {self.symbol}"
            )
        if order.price is None or not math.isfinite(order.price):
            raise InvalidBookMutation(
                f"Resting order {order.id} must have a finite limit price"
            )

        levels = self.bids if order.side == Side.BUY else self.asks
        reverse = order.side == Side.BUY
        insertion_index = len(levels)
        existing_level = None
        for index, level in enumerate(levels):
            if level.price == order.price:
                insertion_index = index
                existing_level = level
                break
            if (
                (reverse and level.price < order.price)
                or (not reverse and level.price > order.price)
            ):
                insertion_index = index
                break

        level = existing_level or BookLevel(
            price=order.price,
            side=order.side,
        )
        level.add_order(order)
        if existing_level is None:
            levels.insert(insertion_index, level)
        self._order_index[order.id] = (order.side, level)

    def add_bid(self, order: Order) -> None:
        if order.side != Side.BUY:
            raise InvalidBookMutation(
                f"Cannot add {order.side.value} order {order.id} as a bid"
            )
        self.add_order(order)

    def add_ask(self, order: Order) -> None:
        if order.side != Side.SELL:
            raise InvalidBookMutation(
                f"Cannot add {order.side.value} order {order.id} as an ask"
            )
        self.add_order(order)

    @staticmethod
    def _level_identity_index(
        levels: deque[BookLevel],
        target: BookLevel,
    ) -> Optional[int]:
        for index, level in enumerate(levels):
            if level is target:
                return index
        return None

    def validate_fill(
        self,
        order: Order,
        fill_qty: float,
        fill_price: Optional[float] = None,
    ) -> None:
        """Validate a resting fill without changing any book state."""
        location = self._order_index.get(order.id)
        if location is None or location[1]._order_index(order) is None:
            raise InvalidBookMutation(
                f"Order {order.id} is not resting in book {self.symbol}"
            )
        side, level = location
        if side != order.side:
            raise OrderBookInvariantError(
                f"Order index side mismatch for order {order.id}: "
                f"indexed={side.value}, order={order.side.value}"
            )
        levels = self.bids if side == Side.BUY else self.asks
        if self._level_identity_index(levels, level) is None:
            raise OrderBookInvariantError(
                f"Indexed level missing for order {order.id}: "
                f"side={side.value}, price={level.price}"
            )
        resolved_price = order.price if fill_price is None else fill_price
        if resolved_price is None:
            raise InvalidBookMutation(
                f"Resting order {order.id} has no fill price"
            )
        level._validate_resting_order(order)
        order.validate_fill(fill_qty, resolved_price)
        old_remaining_qty = order.remaining_qty
        _, _, new_remaining_qty = order._preview_fill(
            fill_qty,
            resolved_price,
        )
        level._updated_aggregates(
            order,
            -(old_remaining_qty - new_remaining_qty),
        )

    def apply_fill(
        self,
        order: Order,
        fill_qty: float,
        fill_price: Optional[float] = None,
    ) -> BookFillResult:
        """Atomically fill one resting order and clean up its level/index."""
        self.validate_fill(order, fill_qty, fill_price)
        side, level = self._order_index[order.id]
        levels = self.bids if side == Side.BUY else self.asks
        level_index = self._level_identity_index(levels, level)
        if level_index is None:
            raise OrderBookInvariantError(
                f"Indexed level vanished for order {order.id}: "
                f"side={side.value}, price={level.price}"
            )

        applied_qty, fully_filled = level.apply_fill(
            order,
            fill_qty,
            fill_price,
        )
        level_removed = False
        if fully_filled:
            del self._order_index[order.id]
            if not level.orders:
                del levels[level_index]
                level_removed = True

        return BookFillResult(
            order_id=order.id,
            fill_qty=applied_qty,
            remaining_qty=order.remaining_qty,
            fully_filled=fully_filled,
            level_removed=level_removed,
        )

    def remove_order(self, order: Order) -> bool:
        """Remove a resting order once; a non-resting removal is a no-op."""
        location = self._order_index.get(order.id)
        if location is None:
            return False
        side, level = location
        if level._order_index(order) is None:
            raise OrderBookInvariantError(
                f"Order index points to a queue without order {order.id}: "
                f"side={side.value}, price={level.price}"
            )
        levels = self.bids if side == Side.BUY else self.asks
        level_index = self._level_identity_index(levels, level)
        if level_index is None:
            raise OrderBookInvariantError(
                f"Indexed level missing while removing order {order.id}: "
                f"side={side.value}, price={level.price}"
            )

        removed = level.remove_order(order)
        if not removed:
            return False
        del self._order_index[order.id]
        if not level.orders:
            del levels[level_index]
        return True

    def update_order_qty(self, order: Order) -> None:
        """Reject the legacy repair path; callers must use ``apply_fill``."""
        raise InvalidBookMutation(
            f"update_order_qty cannot safely infer the fill for order "
            f"{order.id}; use apply_fill"
        )

    def get_best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    def get_best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    def get_mid_price(self) -> Optional[float]:
        bid = self.get_best_bid()
        ask = self.get_best_ask()
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return None

    def is_resting(self, order_id: int) -> bool:
        return order_id in self._order_index

    def assert_invariants(self) -> None:
        """Validate every level, FIFO membership, and lookup entry."""
        seen_orders: Dict[int, Tuple[Side, float, Order, BookLevel]] = {}
        for side, levels in (
            (Side.BUY, self.bids),
            (Side.SELL, self.asks),
        ):
            prices = [level.price for level in levels]
            expected_prices = sorted(
                prices,
                reverse=side == Side.BUY,
            )
            if prices != expected_prices:
                raise OrderBookInvariantError(
                    f"Unsorted {side.value} levels in {self.symbol}: "
                    f"prices={prices}"
                )

            for level in levels:
                level.assert_invariants(side)
                for order in level.orders:
                    if order.symbol != self.symbol:
                        raise OrderBookInvariantError(
                            f"Wrong-symbol order {order.id}: "
                            f"book={self.symbol}, order={order.symbol}, "
                            f"side={side.value}, price={level.price}"
                        )
                    if order.id in seen_orders:
                        other_side, other_price, _, _ = seen_orders[order.id]
                        raise OrderBookInvariantError(
                            f"Order {order.id} appears multiple times: "
                            f"{other_side.value} {other_price} and "
                            f"{side.value} {level.price}"
                        )
                    seen_orders[order.id] = (
                        side,
                        level.price,
                        order,
                        level,
                    )

        if self.bids:
            actual_best_bid = max(level.price for level in self.bids)
            if self.get_best_bid() != actual_best_bid:
                raise OrderBookInvariantError(
                    f"Best bid mismatch in {self.symbol}: "
                    f"cached={self.get_best_bid()}, actual={actual_best_bid}"
                )
        if self.asks:
            actual_best_ask = min(level.price for level in self.asks)
            if self.get_best_ask() != actual_best_ask:
                raise OrderBookInvariantError(
                    f"Best ask mismatch in {self.symbol}: "
                    f"cached={self.get_best_ask()}, actual={actual_best_ask}"
                )

        if self.bids and self.asks:
            best_bid = self.bids[0].price
            best_ask = self.asks[0].price
            if best_bid >= best_ask:
                raise OrderBookInvariantError(
                    f"Crossed or locked book in {self.symbol}: "
                    f"best_bid={best_bid}, best_ask={best_ask}"
                )

        if set(self._order_index) != set(seen_orders):
            raise OrderBookInvariantError(
                f"Order index mismatch in {self.symbol}: "
                f"indexed_ids={sorted(self._order_index)}, "
                f"queued_ids={sorted(seen_orders)}"
            )
        for order_id, (indexed_side, indexed_level) in self._order_index.items():
            side, price, order, level = seen_orders[order_id]
            if (
                indexed_side != side
                or indexed_level is not level
                or level._order_index(order) is None
            ):
                raise OrderBookInvariantError(
                    f"Order index location mismatch for order {order_id}: "
                    f"indexed_side={indexed_side.value}, "
                    f"queued_side={side.value}, queued_price={price}"
                )

@dataclass(frozen=True, slots=True)
class PriceLevelSnapshot:
    """Immutable public view of one non-empty order-book price level."""

    price: float
    qty: float
    strategy_qty: float
    market_qty: float
    order_count: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.price) or self.price <= 0.0:
            raise ValueError("Snapshot price must be positive and finite")
        for name, value in (
            ("qty", self.qty),
            ("strategy_qty", self.strategy_qty),
            ("market_qty", self.market_qty),
        ):
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"Snapshot {name} must be non-negative and finite"
                )
        if self.qty <= QUANTITY_EPSILON:
            raise ValueError("Empty price levels cannot be published")
        if not _quantities_equal(
            self.qty,
            self.strategy_qty + self.market_qty,
        ):
            raise ValueError(
                "Snapshot category quantities must sum to total quantity"
            )
        if self.order_count <= 0:
            raise ValueError("Snapshot order_count must be positive")


@dataclass(frozen=True, slots=True)
class TradeSnapshot:
    """Immutable trade payload published exactly with its committed action."""

    trade_id: str
    price: float
    qty: float
    aggressor_side: Side
    timestamp: datetime

    def __post_init__(self) -> None:
        if not self.trade_id:
            raise ValueError("Trade snapshot requires a trade_id")
        if not math.isfinite(self.price) or self.price <= 0.0:
            raise ValueError("Trade snapshot price must be positive and finite")
        if not math.isfinite(self.qty) or self.qty <= QUANTITY_EPSILON:
            raise ValueError("Trade snapshot quantity must be positive and finite")
        if self.timestamp.tzinfo is None:
            raise ValueError("Trade snapshot timestamp must be timezone-aware")
        if self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("Trade snapshot timestamp must be UTC")


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    """Immutable, globally sequenced view of committed exchange state."""

    symbol: str
    sequence: int
    timestamp: datetime
    bids: Tuple[PriceLevelSnapshot, ...]
    asks: Tuple[PriceLevelSnapshot, ...]
    trades: Tuple[TradeSnapshot, ...] = ()
    run_generation: int = 0

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("Market-data snapshot requires a symbol")
        if self.sequence < 0:
            raise ValueError("Market-data sequence cannot be negative")
        if self.timestamp.tzinfo is None:
            raise ValueError(
                "Market-data snapshot timestamp must be timezone-aware"
            )
        if self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("Market-data snapshot timestamp must be UTC")
        bid_prices = tuple(level.price for level in self.bids)
        ask_prices = tuple(level.price for level in self.asks)
        if bid_prices != tuple(sorted(bid_prices, reverse=True)):
            raise ValueError("Snapshot bids must be sorted best to worst")
        if ask_prices != tuple(sorted(ask_prices)):
            raise ValueError("Snapshot asks must be sorted best to worst")

    @property
    def bid_levels(self) -> Tuple[PriceLevelSnapshot, ...]:
        """Compatibility name for pre-remediation metrics integrations."""
        return self.bids

    @property
    def ask_levels(self) -> Tuple[PriceLevelSnapshot, ...]:
        """Compatibility name for pre-remediation metrics integrations."""
        return self.asks


# Compatibility alias retained for integrations that imported the old name.
# The aliased model is now a deeply immutable snapshot, not a live-book event.
MarketDataUpdate = MarketDataSnapshot

@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Immutable, self-contained execution report captured at creation time."""

    exec_id: str
    order_id: int
    cl_ord_id: str
    symbol: str
    side: Side
    order_qty: float
    order_price: Optional[float]
    tif: TimeInForce
    ord_status: OrderStatus
    exec_type: ExecutionType
    leaves_qty: float
    cum_qty: float
    canceled_qty: float
    avg_px: float
    submitted_at: datetime
    timestamp: datetime
    order_version: int
    exchange_order_id: Optional[str] = None
    run_generation: int = 0
    last_px: Optional[float] = None
    last_qty: Optional[float] = None
    trade_id: Optional[int] = None
    exchange_trade_id: Optional[str] = None
    orig_cl_ord_id: Optional[str] = None
    reject_reason: Optional[str] = None
    text: Optional[str] = None
    session_id: Optional[str] = None
    session_alias: Optional[str] = None

    def __post_init__(self) -> None:
        """Reject inconsistent status, execution type, and quantity snapshots."""
        legacy_exec_types = {
            "PARTIAL": ExecutionType.TRADE,
            "FILLED": ExecutionType.TRADE,
            "CANCELLED": ExecutionType.CANCELED,
        }
        raw_exec_type = self.exec_type
        try:
            resolved_exec_type = ExecutionType(raw_exec_type)
        except ValueError:
            resolved_exec_type = legacy_exec_types.get(str(raw_exec_type))
        if resolved_exec_type is None:
            raise ValueError(f"Unsupported execution type: {self.exec_type}")
        object.__setattr__(self, "exec_type", resolved_exec_type)

        allowed_statuses = ORDER_STATUSES_BY_EXECUTION_TYPE[
            resolved_exec_type
        ]
        if self.ord_status not in allowed_statuses:
            raise ValueError(
                f"Inconsistent execution report: exec_type={self.exec_type}, "
                f"ord_status={self.ord_status.value}"
            )

        quantities = (
            self.order_qty,
            self.cum_qty,
            self.leaves_qty,
            self.canceled_qty,
            self.avg_px,
        )
        if not all(math.isfinite(value) for value in quantities):
            raise ValueError("Execution report quantities must be finite")
        if self.order_qty < 0:
            raise ValueError("Execution report OrderQty cannot be negative")
        if (
            self.cum_qty < 0
            or self.leaves_qty < 0
            or self.canceled_qty < 0
            or self.avg_px < 0
        ):
            raise ValueError("Execution report quantities cannot be negative")
        if (
            self.cum_qty > self.order_qty + QUANTITY_EPSILON
            or self.leaves_qty > self.order_qty + QUANTITY_EPSILON
            or self.canceled_qty > self.order_qty + QUANTITY_EPSILON
        ):
            raise ValueError("Execution report quantities cannot exceed order quantity")
        if not math.isclose(
            self.cum_qty + self.leaves_qty + self.canceled_qty,
            self.order_qty,
            rel_tol=0.0,
            abs_tol=QUANTITY_EPSILON,
        ):
            raise ValueError(
                "CumQty plus LeavesQty plus CanceledQty must equal OrderQty"
            )
        if not self.exec_id:
            raise ValueError("ExecID is required")

        if resolved_exec_type == ExecutionType.TRADE:
            if (
                self.last_qty is None
                or not math.isfinite(self.last_qty)
                or self.last_qty <= 0.0
            ):
                raise ValueError("TRADE reports require positive LastQty")
            if self.last_qty > self.cum_qty + QUANTITY_EPSILON:
                raise ValueError("LastQty cannot exceed CumQty")
            if (
                self.last_px is None
                or not math.isfinite(self.last_px)
                or self.last_px <= 0.0
            ):
                raise ValueError("TRADE reports require positive LastPx")
            if self.trade_id is None:
                raise ValueError("TRADE reports require a TradeID")
        elif self.last_qty is not None or self.last_px is not None:
            raise ValueError(
                "Only TRADE reports may contain LastQty or LastPx"
            )

        if self.ord_status == OrderStatus.NEW:
            if not _quantities_equal(self.cum_qty, 0.0):
                raise ValueError("NEW execution reports must have zero CumQty")
            if not _quantities_equal(self.canceled_qty, 0.0):
                raise ValueError(
                    "NEW execution reports must have zero CanceledQty"
                )
        elif self.ord_status == OrderStatus.PARTIAL:
            if self.cum_qty <= QUANTITY_EPSILON:
                raise ValueError("PARTIAL reports require positive CumQty")
            if self.leaves_qty <= QUANTITY_EPSILON:
                raise ValueError("PARTIAL reports require positive LeavesQty")
            if not _quantities_equal(self.canceled_qty, 0.0):
                raise ValueError(
                    "PARTIAL reports must have zero CanceledQty"
                )
        elif self.ord_status == OrderStatus.FILLED:
            if not _quantities_equal(self.cum_qty, self.order_qty):
                raise ValueError("FILLED reports require CumQty equal OrderQty")
            if not _quantities_equal(self.leaves_qty, 0.0):
                raise ValueError("FILLED execution reports require zero LeavesQty")
            if not _quantities_equal(self.canceled_qty, 0.0):
                raise ValueError(
                    "FILLED execution reports require zero CanceledQty"
                )
        elif self.ord_status in {
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }:
            if not _quantities_equal(self.leaves_qty, 0.0):
                raise ValueError(
                    "Terminal non-fill reports require zero LeavesQty"
                )
            if (
                self.ord_status == OrderStatus.CANCELLED
                and self.canceled_qty <= QUANTITY_EPSILON
            ):
                raise ValueError(
                    "CANCELLED reports require positive CanceledQty"
                )

        if self.cum_qty <= QUANTITY_EPSILON:
            if not _quantities_equal(self.avg_px, 0.0):
                raise ValueError("Unfilled reports require zero AvgPx")
        elif self.avg_px <= 0.0:
            raise ValueError("Filled reports require positive AvgPx")


def create_execution_report(
    order: Order,
    *,
    exec_type: Union[ExecutionType, str],
    timestamp: datetime,
    exec_id: str,
    last_px: Optional[float] = None,
    last_qty: Optional[float] = None,
    trade_id: Optional[int] = None,
    exchange_trade_id: Optional[str] = None,
    cl_ord_id: Optional[str] = None,
    orig_cl_ord_id: Optional[str] = None,
    text: Optional[str] = None,
) -> ExecutionReport:
    """Capture and validate every execution-report value from ``order`` now."""
    cum_qty = order.filled_qty
    leaves_qty = order.remaining_qty
    resolved_exec_type = {
        "PARTIAL": ExecutionType.TRADE,
        "FILLED": ExecutionType.TRADE,
        "CANCELLED": ExecutionType.CANCELED,
    }.get(str(exec_type), exec_type)
    try:
        execution_type = ExecutionType(resolved_exec_type)
    except ValueError as exc:
        raise ValueError(f"Unsupported execution type: {exec_type}") from exc

    return ExecutionReport(
        exec_id=exec_id,
        order_id=order.id,
        cl_ord_id=cl_ord_id or order.cl_ord_id,
        symbol=order.symbol,
        side=order.side,
        order_qty=order.qty,
        order_price=order.price,
        tif=order.tif,
        ord_status=order.status,
        exec_type=exec_type,
        leaves_qty=leaves_qty,
        cum_qty=cum_qty,
        canceled_qty=order.canceled_qty,
        avg_px=order.avg_fill_price or 0.0,
        last_px=last_px,
        last_qty=last_qty,
        trade_id=trade_id,
        orig_cl_ord_id=orig_cl_ord_id,
        reject_reason=order.reject_reason,
        text=text,
        session_id=order.session_id,
        session_alias=order.session_alias,
        submitted_at=order.ts_submitted,
        timestamp=timestamp,
        order_version=order.version,
        exchange_order_id=(
            order.exchange_order_id
            or f"G{order.run_generation:08d}-O{order.id:08d}"
        ),
        run_generation=order.run_generation,
        exchange_trade_id=(
            exchange_trade_id
            or f"G{order.run_generation:08d}-T{trade_id:08d}"
            if trade_id is not None
            else None
        ),
    )


def create_cancel_reject(
    command: CancelOrderCommand,
    *,
    reason: CancelRejectReason,
    text: str,
    timestamp: datetime,
    order: Optional[Order] = None,
) -> CancelReject:
    """Capture a cancel rejection from immutable command/current scalar state."""
    return CancelReject(
        cancel_cl_ord_id=command.cancel_cl_ord_id or "",
        orig_cl_ord_id=command.orig_cl_ord_id or "",
        order_id=order.id if order else None,
        ord_status=order.status if order else None,
        reason=reason,
        text=text,
        session_id=command.session_id,
        session_alias=command.session_alias,
        timestamp=timestamp,
        exchange_order_id=(
            (
                order.exchange_order_id
                or f"G{order.run_generation:08d}-O{order.id:08d}"
            )
            if order
            else None
        ),
    )

SchedulerMetadataScalar = Optional[Union[str, int, float, bool]]
SchedulerEventMetadata = Tuple[Tuple[str, SchedulerMetadataScalar], ...]


@dataclass(frozen=True, slots=True)
class SchedulerFailure:
    """Immutable scalar diagnostic for one terminal scheduler failure."""

    event_id: str
    event_type: str
    scheduled_timestamp: datetime
    scheduler_sequence: int
    failure_timestamp: datetime
    exception_type: str
    exception_message: str
    retry_attempted: bool
    status: SchedulerFailureStatus
    event_metadata: SchedulerEventMetadata

    def to_dict(self) -> Dict[str, object]:
        """Return the stable JSON-safe representation used by diagnostics."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "scheduled_timestamp": self.scheduled_timestamp.isoformat(),
            "scheduler_sequence": self.scheduler_sequence,
            "failure_timestamp": self.failure_timestamp.isoformat(),
            "exception_type": self.exception_type,
            "exception_message": self.exception_message,
            "retry_attempted": self.retry_attempted,
            "status": self.status.value,
            "event_metadata": dict(self.event_metadata),
        }


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    """Event scheduled for future execution with immutable diagnostics."""

    event_id: str
    timestamp: datetime
    sequence: int
    event_type: str
    data: object
    metadata: SchedulerEventMetadata

    def __lt__(self, other: "ScheduledEvent") -> bool:
        """Order events by timestamp, preserving scheduling order for ties."""
        return (self.timestamp, self.sequence) < (other.timestamp, other.sequence)
    
@dataclass(slots=True)
class OrderLatencyTrace:
    """Latency tracker-owned mutable state for one accepted order."""

    order_id: int
    session_id: Optional[str]
    sim_submitted_at: datetime
    sim_received_at: Optional[datetime] = None
    sim_processed_at: Optional[datetime] = None
    sim_report_created_at: Optional[datetime] = None
    sim_report_delivered_at: Optional[datetime] = None
    real_matching_duration_ns: Optional[int] = None


@dataclass(frozen=True, slots=True)
class LatencyMeasurement:
    """Immutable latency sample emitted at a well-defined clock boundary."""

    order_id: int
    session_id: Optional[str]
    metric: str
    value: float
    unit: str
    timestamp: datetime
    report_exec_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """Immutable process-local observability snapshot used by ``/stats``."""

    generated_at: datetime
    orders_submitted: int
    orders_accepted: int
    orders_rejected: int
    orders_canceled: int
    orders_filled: int
    partial_fill_events: int
    cancel_requests: int
    cancel_rejects: int
    execution_reports: int
    trades: int
    traded_quantity: float
    traded_notional: float
    canceled_quantity: float
    active_orders: int
    resting_orders: int
    order_book_levels: int
    connected_sessions: int
    average_real_matching_duration_us: Optional[float]
    average_real_report_serialization_duration_us: Optional[float]
    average_sim_inbound_latency_ms: Optional[float]
    average_sim_engine_latency_ms: Optional[float]
    average_sim_outbound_latency_ms: Optional[float]
    average_sim_end_to_end_latency_ms: Optional[float]
    report_delivery_failures: int
    invalid_latency_measurements: int
    metric_sink_failures: int
    buffered_metric_records: int

    def to_dict(self, *, run_id: Optional[str] = None) -> Dict[str, object]:
        """Return the stable, JSON-safe public ``/stats`` representation."""
        return {
            "generated_at": self.generated_at.isoformat(),
            "run_id": run_id,
            "orders": {
                "submitted": self.orders_submitted,
                "accepted": self.orders_accepted,
                "rejected": self.orders_rejected,
                "canceled": self.orders_canceled,
                "filled": self.orders_filled,
                "partial_fill_events": self.partial_fill_events,
                "cancel_requests": self.cancel_requests,
                "cancel_rejects": self.cancel_rejects,
                "active": self.active_orders,
                "resting": self.resting_orders,
            },
            "trades": {
                "count": self.trades,
                "quantity": self.traded_quantity,
                "notional": self.traded_notional,
                "canceled_quantity": self.canceled_quantity,
            },
            "reports": {
                "created": self.execution_reports,
                "delivery_failures": self.report_delivery_failures,
            },
            "sessions": {
                "connected": self.connected_sessions,
            },
            "book": {
                "levels": self.order_book_levels,
            },
            "latency": {
                "average_real_matching_duration_us": (
                    self.average_real_matching_duration_us
                ),
                "average_real_report_serialization_duration_us": (
                    self.average_real_report_serialization_duration_us
                ),
                "average_sim_inbound_latency_ms": (
                    self.average_sim_inbound_latency_ms
                ),
                "average_sim_engine_latency_ms": (
                    self.average_sim_engine_latency_ms
                ),
                "average_sim_outbound_latency_ms": (
                    self.average_sim_outbound_latency_ms
                ),
                "average_sim_end_to_end_latency_ms": (
                    self.average_sim_end_to_end_latency_ms
                ),
                "invalid_measurements": self.invalid_latency_measurements,
            },
            "persistence": {
                "failures": self.metric_sink_failures,
                "buffered_records": self.buffered_metric_records,
            },
        }


@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    """Complete exchange configuration."""
    exchange_name: str
    symbols: List[str]
    latency: Dict[str, int]
    execution: Dict[str, float]
    fix: Dict[str, Any]
    admin_api: Dict[str, int]
    prometheus: Dict[str, Any]
    tsdb: Optional[Dict[str, Any]] = None

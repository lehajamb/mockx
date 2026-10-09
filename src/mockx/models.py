"""Core data models for the mock exchange."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Dict, FrozenSet, Mapping, Tuple, Union
from enum import Enum
from types import MappingProxyType
import math

from .instruments import Instrument


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


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


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
    EXCHANGE_UNAVAILABLE = "EXCHANGE_UNAVAILABLE"


class FixCommandType(str, Enum):
    """Supported state-mutating FIX application commands."""

    NEW_ORDER = "NEW_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"


class FixDeliveryState(str, Enum):
    """Explicit application-level state for one outbound FIX message."""

    PENDING = "PENDING"
    DUE = "DUE"
    DELIVERED = "DELIVERED"
    RETRY_PENDING = "RETRY_PENDING"
    PERMANENTLY_FAILED = "PERMANENTLY_FAILED"


class FixDeliveryMessageType(str, Enum):
    """FIX application message types covered by reliable delivery."""

    EXECUTION_REPORT = "8"
    ORDER_CANCEL_REJECT = "9"


class CancellationOutcome(str, Enum):
    """Bounded cancellation outcomes used by process metrics."""

    ACCEPTED = "accepted"
    UNKNOWN_ORDER = "unknown_order"
    ALREADY_TERMINAL = "already_terminal"
    WRONG_OWNER = "wrong_owner"
    DUPLICATE_REQUEST = "duplicate_request"
    STALE_RUN_GENERATION = "stale_run_generation"
    EXCHANGE_UNAVAILABLE = "exchange_unavailable"


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


TERMINAL_ORDER_STATUSES: FrozenSet[OrderStatus] = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
    }
)


ORDER_STATE_TRANSITIONS: Mapping[OrderStatus, FrozenSet[OrderStatus]] = (
    MappingProxyType(
        {
            OrderStatus.PENDING_NEW: frozenset(
                {
                    OrderStatus.NEW,
                    OrderStatus.PARTIAL,
                    OrderStatus.FILLED,
                    OrderStatus.REJECTED,
                    OrderStatus.CANCELLED,
                }
            ),
            OrderStatus.NEW: frozenset(
                {
                    OrderStatus.PARTIAL,
                    OrderStatus.FILLED,
                    OrderStatus.CANCELLED,
                }
            ),
            OrderStatus.PARTIAL: frozenset(
                {
                    OrderStatus.PARTIAL,
                    OrderStatus.FILLED,
                    OrderStatus.CANCELLED,
                }
            ),
            OrderStatus.FILLED: frozenset(),
            OrderStatus.REJECTED: frozenset(),
            OrderStatus.CANCELLED: frozenset(),
        }
    )
)


ORDER_STATUSES_BY_EXECUTION_TYPE: Mapping[ExecutionType, FrozenSet[OrderStatus]] = (
    MappingProxyType(
        {
            ExecutionType.NEW: frozenset({OrderStatus.NEW}),
            ExecutionType.TRADE: frozenset(
                {
                    OrderStatus.PARTIAL,
                    OrderStatus.FILLED,
                }
            ),
            ExecutionType.CANCELED: frozenset({OrderStatus.CANCELLED}),
            ExecutionType.REJECTED: frozenset({OrderStatus.REJECTED}),
        }
    )
)


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
    participant_id: Optional[str] = None

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
    order_type: OrderType
    order_qty_lots: int
    price_ticks: Optional[int]
    tif: TimeInForce
    submitted_at: datetime
    participant_id: str
    session_id: Optional[str] = None
    session_alias: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.cl_ord_id:
            raise ValueError("ClOrdID is required")
        if not self.symbol:
            raise ValueError("Symbol is required")
        if (
            not isinstance(self.order_qty_lots, int)
            or isinstance(self.order_qty_lots, bool)
            or self.order_qty_lots <= 0
        ):
            raise ValueError("Order quantity lots must be a positive integer")
        if not isinstance(self.order_type, OrderType):
            raise ValueError(f"Unsupported OrderType: {self.order_type}")
        if self.order_type == OrderType.LIMIT:
            if (
                not isinstance(self.price_ticks, int)
                or isinstance(self.price_ticks, bool)
                or self.price_ticks <= 0
            ):
                raise ValueError("LIMIT orders require positive integer price ticks")
        elif self.price_ticks is not None:
            raise ValueError("MARKET orders cannot contain price ticks")
        if not isinstance(self.tif, TimeInForce):
            raise ValueError(f"Unsupported TimeInForce: {self.tif}")
        if self.session_id is not None and not self.session_id:
            raise ValueError("session_id cannot be empty")
        if not self.participant_id:
            raise ValueError("participant_id is required")


@dataclass(frozen=True, slots=True)
class ParsedFixCommand:
    """Immutable normalized FIX data captured by a QuickFIX callback."""

    command_type: FixCommandType
    session: SessionIdentity
    msg_seq_num: int
    cl_ord_id: str
    symbol: str
    side: Side
    order_type: Optional[OrderType] = None
    order_qty_lots: int = 0
    price_ticks: Optional[int] = None
    tif: Optional[TimeInForce] = None
    orig_cl_ord_id: Optional[str] = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.msg_seq_num, int)
            or isinstance(self.msg_seq_num, bool)
            or self.msg_seq_num <= 0
        ):
            raise ValueError("FIX MsgSeqNum must be a positive integer")
        if not self.cl_ord_id:
            raise ValueError("ClOrdID is required")
        if not self.symbol:
            raise ValueError("Symbol is required")
        if not isinstance(self.side, Side):
            raise ValueError(f"Unsupported Side: {self.side}")
        if (
            not isinstance(self.order_qty_lots, int)
            or isinstance(self.order_qty_lots, bool)
            or self.order_qty_lots <= 0
        ):
            raise ValueError("Order quantity lots must be positive")
        if self.command_type == FixCommandType.NEW_ORDER:
            if self.tif is None:
                raise ValueError("New orders require TimeInForce")
            if self.order_type is None:
                raise ValueError("New orders require OrderType")
            if self.orig_cl_ord_id is not None:
                raise ValueError("New orders cannot contain OrigClOrdID")
        elif self.command_type == FixCommandType.CANCEL_ORDER:
            if not self.orig_cl_ord_id:
                raise ValueError("Cancel requests require OrigClOrdID")
            if (
                self.price_ticks is not None
                or self.tif is not None
                or self.order_type is not None
            ):
                raise ValueError(
                    "Cancel requests cannot contain order type, price, or TIF"
                )


@dataclass(frozen=True, slots=True)
class FixIngressEnvelope:
    """Run-scoped immutable command at the deterministic ingress boundary."""

    command_type: FixCommandType
    session: SessionIdentity
    msg_seq_num: int
    cl_ord_id: str
    orig_cl_ord_id: Optional[str]
    symbol: str
    side: Side
    order_type: Optional[OrderType]
    order_qty_lots: int
    price_ticks: Optional[int]
    tif: Optional[TimeInForce]
    ingress_sequence: int
    simulated_receive_timestamp: datetime
    run_generation: int

    @property
    def ordering_key(self) -> Tuple[datetime, str, int, int]:
        return (
            self.simulated_receive_timestamp,
            self.session.session_id,
            self.msg_seq_num,
            self.ingress_sequence,
        )


@dataclass(frozen=True, slots=True)
class IngressBarrier:
    """Acknowledged run-local ingress high-water mark."""

    run_generation: int
    through_sequence: int


@dataclass(frozen=True, slots=True)
class ProcessOrderCommand:
    """Immutable token authorizing one generation of initial order processing."""

    order_id: int
    expected_version: int


@dataclass(frozen=True, slots=True)
class ITCHHeartbeatCommand:
    """Generation-qualified deterministic feed heartbeat."""

    run_generation: int


@dataclass(frozen=True, slots=True)
class CancelOrderCommand:
    """Immutable cancel intent resolved against authoritative state at execution."""

    cancel_cl_ord_id: str
    orig_cl_ord_id: str
    symbol: str
    side: Side
    order_qty_lots: int
    requested_at: datetime
    session_id: str
    session_alias: Optional[str] = None
    participant_id: Optional[str] = None
    run_generation: int = 0
    ingress_sequence: int = 0
    msg_seq_num: int = 0
    duplicate_request: bool = False

    def __post_init__(self) -> None:
        if not self.cancel_cl_ord_id:
            raise ValueError("Cancel ClOrdID is required")
        if not self.orig_cl_ord_id:
            raise ValueError("OrigClOrdID is required")
        if not self.symbol:
            raise ValueError("Cancel Symbol is required")
        if not self.session_id:
            raise ValueError("Cancel session_id is required")


@dataclass(frozen=True, slots=True)
class CancelReject:
    """Immutable cancel-rejection snapshot without live order/FIX objects."""

    reject_id: str
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
    run_generation: int = 0
    ingress_sequence: int = 0

    def __post_init__(self) -> None:
        if not self.reject_id:
            raise ValueError("Cancel reject ID is required")


@dataclass(slots=True)
class Order:
    """Mutable engine-owned order aggregate using integer ticks and lots."""

    id: int
    cl_ord_id: str
    symbol: str
    side: Side
    order_type: OrderType
    order_qty_lots: int
    price_ticks: Optional[int]
    instrument: Instrument
    tif: TimeInForce
    ts_submitted: datetime  # Exchange timestamp
    participant_id: str
    status: OrderStatus = OrderStatus.PENDING_NEW
    filled_qty_lots: int = 0
    canceled_qty_lots: int = 0
    filled_notional_tick_lots: int = 0
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
        if self.instrument.symbol != self.symbol:
            raise ValueError("Order instrument does not match symbol")
        if not self.participant_id:
            raise ValueError("participant_id is required")
        if (
            not isinstance(self.order_qty_lots, int)
            or isinstance(self.order_qty_lots, bool)
            or self.order_qty_lots <= 0
        ):
            raise ValueError("Order quantity lots must be positive")
        if not isinstance(self.order_type, OrderType):
            raise ValueError(f"Unsupported OrderType: {self.order_type}")
        if self.order_type == OrderType.LIMIT:
            if (
                not isinstance(self.price_ticks, int)
                or isinstance(self.price_ticks, bool)
                or self.price_ticks <= 0
            ):
                raise ValueError("LIMIT orders require positive price ticks")
        elif self.price_ticks is not None:
            raise ValueError("MARKET orders cannot contain price ticks")
        if not isinstance(self.tif, TimeInForce):
            raise ValueError(f"Unsupported TimeInForce: {self.tif}")
        for name, value in (
            ("filled_qty_lots", self.filled_qty_lots),
            ("canceled_qty_lots", self.canceled_qty_lots),
            (
                "filled_notional_tick_lots",
                self.filled_notional_tick_lots,
            ),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.filled_qty_lots + self.canceled_qty_lots > self.order_qty_lots:
            raise ValueError("Order quantities are over-allocated")

        # Capture the origin once. Execution status and later alias changes must
        # never move resting quantity between accounting categories.
        self._liquidity_category = liquidity_category_for_session(self.session_alias)

    @property
    def liquidity_category(self) -> LiquidityCategory:
        return self._liquidity_category

    @property
    def is_strategy_order(self) -> bool:
        return self._liquidity_category == LiquidityCategory.STRATEGY

    @property
    def leaves_qty_lots(self) -> int:
        """Return exact live working quantity."""
        leaves = self.order_qty_lots - self.filled_qty_lots - self.canceled_qty_lots
        if leaves < 0:
            raise OrderBookInvariantError(
                f"Order {self.id} is over-allocated: "
                f"order_qty_lots={self.order_qty_lots}, "
                f"filled_qty_lots={self.filled_qty_lots}, "
                f"canceled_qty_lots={self.canceled_qty_lots}"
            )
        return leaves

    @property
    def average_fill_price(self) -> Optional[Decimal]:
        """Return the exact derived average price, never a binary float."""
        if self.filled_qty_lots == 0:
            return None
        return (
            self.instrument.tick_size
            * Decimal(self.filled_notional_tick_lots)
            / Decimal(self.filled_qty_lots)
        )

    def _preview_fill(
        self,
        fill_qty_lots: int,
        fill_price_ticks: int,
    ) -> Tuple[int, int, int]:
        if (
            not isinstance(fill_qty_lots, int)
            or isinstance(fill_qty_lots, bool)
            or fill_qty_lots <= 0
        ):
            raise InvalidBookMutation(
                f"Fill lots for order {self.id} must be positive: " f"{fill_qty_lots}"
            )
        if (
            not isinstance(fill_price_ticks, int)
            or isinstance(fill_price_ticks, bool)
            or fill_price_ticks <= 0
        ):
            raise InvalidBookMutation(
                f"Fill price ticks for order {self.id} must be positive: "
                f"{fill_price_ticks}"
            )

        old_remaining = self.leaves_qty_lots
        if fill_qty_lots > old_remaining:
            raise InvalidBookMutation(
                f"Overfill for order {self.id}: "
                f"fill_qty_lots={fill_qty_lots}, "
                f"leaves_qty_lots={old_remaining}"
            )
        new_filled_qty_lots = self.filled_qty_lots + fill_qty_lots
        new_notional = self.filled_notional_tick_lots + fill_price_ticks * fill_qty_lots
        new_leaves = self.order_qty_lots - new_filled_qty_lots - self.canceled_qty_lots
        return new_filled_qty_lots, new_notional, new_leaves

    def validate_fill(
        self,
        fill_qty_lots: int,
        fill_price_ticks: int,
    ) -> None:
        """Validate a fill without changing the order."""
        self._preview_fill(fill_qty_lots, fill_price_ticks)

    def apply_fill(
        self,
        fill_qty_lots: int,
        fill_price_ticks: int,
    ) -> int:
        """Atomically update cumulative lots and exact notional."""
        old_remaining = self.leaves_qty_lots
        new_filled_qty, new_notional, new_remaining = self._preview_fill(
            fill_qty_lots,
            fill_price_ticks,
        )
        self.filled_qty_lots = new_filled_qty
        self.filled_notional_tick_lots = new_notional
        return old_remaining - new_remaining

    def decrement_remaining(self, quantity_lots: int) -> int:
        """Move an exact prevented/canceled quantity out of leaves."""
        if (
            not isinstance(quantity_lots, int)
            or isinstance(quantity_lots, bool)
            or quantity_lots <= 0
        ):
            raise InvalidBookMutation("Decrement quantity must be positive")
        if quantity_lots > self.leaves_qty_lots:
            raise InvalidBookMutation(f"Order {self.id} decrement exceeds leaves")
        self.canceled_qty_lots += quantity_lots
        return quantity_lots

    def cancel_remaining(self) -> int:
        """Move the complete live remainder into terminal canceled quantity."""
        canceled = self.leaves_qty_lots
        if canceled <= 0:
            return 0
        self.canceled_qty_lots += canceled
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
        if (
            isinstance(self.reject_prob, bool)
            or not isinstance(self.reject_prob, (int, float))
            or not math.isfinite(float(self.reject_prob))
            or not 0.0 <= float(self.reject_prob) <= 1.0
        ):
            raise ValueError("Reject probability must be between 0 and 1")

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
    price_ticks: int
    qty_lots: int
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
        if (
            not isinstance(self.price_ticks, int)
            or isinstance(self.price_ticks, bool)
            or self.price_ticks <= 0
        ):
            raise ValueError("Fill price ticks must be positive")
        if (
            not isinstance(self.qty_lots, int)
            or isinstance(self.qty_lots, bool)
            or self.qty_lots <= 0
        ):
            raise ValueError("Fill quantity lots must be positive")
        if (
            self.aggressor_order_id <= 0
            or self.passive_order_id <= 0
            or self.aggressor_order_id == self.passive_order_id
        ):
            raise ValueError("Fill requires two distinct positive order IDs")


@dataclass(frozen=True, slots=True)
class DisplayedOrderAdded:
    """An order became visible in the authoritative displayed book."""

    run_generation: int
    order_id: int
    symbol: str
    side: Side
    price_ticks: int
    qty_lots: int
    timestamp: datetime

    def __post_init__(self) -> None:
        if self.run_generation < 0 or self.order_id <= 0 or not self.symbol:
            raise ValueError("Displayed add requires generation, order, and symbol")
        if self.price_ticks <= 0 or self.qty_lots <= 0:
            raise ValueError("Displayed add price and quantity must be positive")


@dataclass(frozen=True, slots=True)
class DisplayedOrderExecuted:
    """One fill consumed quantity from an already displayed passive order."""

    run_generation: int
    order_id: int
    symbol: str
    displayed_price_ticks: int
    execution_price_ticks: int
    executed_qty_lots: int
    match_number: int
    timestamp: datetime

    def __post_init__(self) -> None:
        if self.run_generation < 0 or self.order_id <= 0 or not self.symbol:
            raise ValueError(
                "Displayed execution requires generation, order, and symbol"
            )
        if (
            self.displayed_price_ticks <= 0
            or self.execution_price_ticks <= 0
            or self.executed_qty_lots <= 0
            or self.match_number <= 0
        ):
            raise ValueError(
                "Displayed execution prices, quantity, and match must be positive"
            )


@dataclass(frozen=True, slots=True)
class DisplayedOrderDeleted:
    """A displayed order's complete remainder left the book without a fill."""

    run_generation: int
    order_id: int
    symbol: str
    timestamp: datetime

    def __post_init__(self) -> None:
        if self.run_generation < 0 or self.order_id <= 0 or not self.symbol:
            raise ValueError("Displayed delete requires generation, order, and symbol")


DisplayedOrderEvent = Union[
    DisplayedOrderAdded,
    DisplayedOrderExecuted,
    DisplayedOrderDeleted,
]


@dataclass(frozen=True, slots=True)
class Trade:
    """Represents an executed trade."""

    id: int
    symbol: str
    price_ticks: int
    qty_lots: int
    notional_tick_lots: int
    buyer_order_id: int
    seller_order_id: int
    timestamp: datetime
    level: int
    buyer_session_alias: Optional[str]
    seller_session_alias: Optional[str]
    buyer_participant_id: str
    seller_participant_id: str
    aggressor_order_id: int
    passive_order_id: int
    aggressor_side: Side
    exchange_trade_id: Optional[str] = None
    run_generation: int = 0

    def __post_init__(self) -> None:
        if not self.buyer_participant_id or not self.seller_participant_id:
            raise ValueError("Trades require buyer and seller participant IDs")
        if self.buyer_participant_id == self.seller_participant_id:
            raise ValueError("Self-trades are forbidden")


@dataclass(frozen=True, slots=True)
class SelfTradePrevention:
    """Immutable audit event for one prevented self-cross."""

    symbol: str
    participant_id: str
    aggressor_order_id: int
    passive_order_id: int
    prevented_qty_lots: int
    timestamp: datetime

    def __post_init__(self) -> None:
        if not self.symbol or not self.participant_id:
            raise ValueError("Self-trade prevention requires symbol and participant")
        if (
            self.aggressor_order_id <= 0
            or self.passive_order_id <= 0
            or self.aggressor_order_id == self.passive_order_id
        ):
            raise ValueError("Self-trade prevention requires distinct order IDs")
        if self.prevented_qty_lots <= 0:
            raise ValueError("Prevented quantity must be positive")


@dataclass(frozen=True, slots=True)
class BookFillResult:
    """Result of one authoritative fill mutation on a resting order."""

    order_id: int
    fill_qty_lots: int
    leaves_qty_lots: int
    fully_filled: bool
    level_removed: bool


@dataclass
class BookLevel:
    """Single FIFO tick level with exact incrementally maintained lots."""

    price_ticks: int
    level_qty_lots: int = 0
    orders: deque[Order] = field(default_factory=deque)
    strategy_qty_lots: int = 0
    market_qty_lots: int = 0
    side: Optional[Side] = None

    def _order_index(self, order: Order) -> Optional[int]:
        for index, queued_order in enumerate(self.orders):
            if queued_order is order:
                return index
        return None

    def _validate_cached_aggregates(self) -> None:
        if (
            min(
                self.level_qty_lots,
                self.strategy_qty_lots,
                self.market_qty_lots,
            )
            < 0
        ):
            raise OrderBookInvariantError(
                f"Negative aggregate at {self.side} {self.price_ticks}"
            )
        if self.level_qty_lots != self.strategy_qty_lots + self.market_qty_lots:
            raise OrderBookInvariantError(
                f"Level category mismatch at {self.side} {self.price_ticks}: "
                f"level_qty_lots={self.level_qty_lots}, "
                f"strategy_qty_lots={self.strategy_qty_lots}, "
                f"market_qty_lots={self.market_qty_lots}"
            )

    def _updated_aggregates(
        self,
        order: Order,
        delta_lots: int,
    ) -> Tuple[int, int, int]:
        self._validate_cached_aggregates()
        level_qty = self.level_qty_lots + delta_lots
        strategy_qty = self.strategy_qty_lots
        market_qty = self.market_qty_lots
        if order.liquidity_category == LiquidityCategory.STRATEGY:
            strategy_qty += delta_lots
        else:
            market_qty += delta_lots
        if min(level_qty, strategy_qty, market_qty) < 0:
            raise InvalidBookMutation(
                f"Aggregate underflow for order {order.id} at "
                f"{self.side} {self.price_ticks}"
            )
        if level_qty != strategy_qty + market_qty:
            raise OrderBookInvariantError("Post-mutation category mismatch")
        return level_qty, strategy_qty, market_qty

    def _validate_resting_order(self, order: Order) -> int:
        if order.status not in {OrderStatus.NEW, OrderStatus.PARTIAL}:
            raise InvalidBookMutation(
                f"Order {order.id} cannot rest in state {order.status.value}"
            )
        if order.order_type != OrderType.LIMIT or order.price_ticks is None:
            raise InvalidBookMutation(f"Resting order {order.id} must be a limit order")
        if order.price_ticks != self.price_ticks:
            raise InvalidBookMutation(
                f"Order {order.id} price ticks do not match level"
            )
        if self.side is not None and order.side != self.side:
            raise InvalidBookMutation(f"Order {order.id} side does not match level")
        if order.leaves_qty_lots <= 0:
            raise InvalidBookMutation(f"Order {order.id} has no positive leaves")
        return order.leaves_qty_lots

    def add_order(self, order: Order) -> None:
        leaves = self._validate_resting_order(order)
        if self._order_index(order) is not None:
            raise InvalidBookMutation(
                f"Duplicate order id {order.id} at level {self.price_ticks}"
            )
        aggregates = self._updated_aggregates(order, leaves)
        if self.side is None:
            self.side = order.side
        self.orders.append(order)
        (
            self.level_qty_lots,
            self.strategy_qty_lots,
            self.market_qty_lots,
        ) = aggregates

    def apply_fill(
        self,
        order: Order,
        fill_qty_lots: int,
        fill_price_ticks: Optional[int] = None,
    ) -> Tuple[int, bool]:
        queue_index = self._order_index(order)
        if queue_index is None:
            raise InvalidBookMutation(
                f"Order {order.id} is not resting at level {self.price_ticks}"
            )
        self._validate_resting_order(order)
        resolved_price = (
            order.price_ticks if fill_price_ticks is None else fill_price_ticks
        )
        if resolved_price is None:
            raise InvalidBookMutation("Fill price ticks are required")
        old_leaves = order.leaves_qty_lots
        new_filled, new_notional, new_leaves = order._preview_fill(
            fill_qty_lots,
            resolved_price,
        )
        applied = old_leaves - new_leaves
        aggregates = self._updated_aggregates(order, -applied)
        fully_filled = new_leaves == 0
        order.filled_qty_lots = new_filled
        order.filled_notional_tick_lots = new_notional
        (
            self.level_qty_lots,
            self.strategy_qty_lots,
            self.market_qty_lots,
        ) = aggregates
        if fully_filled:
            del self.orders[queue_index]
        return applied, fully_filled

    def decrement_order(self, order: Order, quantity_lots: int) -> bool:
        """Apply an STP decrement while retaining a positive survivor."""
        queue_index = self._order_index(order)
        if queue_index is None:
            raise InvalidBookMutation(
                f"Order {order.id} is not resting at level {self.price_ticks}"
            )
        self._validate_resting_order(order)
        if quantity_lots > order.leaves_qty_lots:
            raise InvalidBookMutation("STP decrement exceeds passive leaves")
        aggregates = self._updated_aggregates(order, -quantity_lots)
        order.decrement_remaining(quantity_lots)
        (
            self.level_qty_lots,
            self.strategy_qty_lots,
            self.market_qty_lots,
        ) = aggregates
        removed = order.leaves_qty_lots == 0
        if removed:
            del self.orders[queue_index]
        return removed

    def remove_order(self, order: Order) -> bool:
        queue_index = self._order_index(order)
        if queue_index is None:
            return False
        leaves = self._validate_resting_order(order)
        aggregates = self._updated_aggregates(order, -leaves)
        del self.orders[queue_index]
        (
            self.level_qty_lots,
            self.strategy_qty_lots,
            self.market_qty_lots,
        ) = aggregates
        return True

    def assert_invariants(self, expected_side: Optional[Side] = None) -> None:
        side = expected_side or self.side
        if not self.orders:
            raise OrderBookInvariantError(
                f"Empty price level retained: side={side}, "
                f"price_ticks={self.price_ticks}"
            )
        if self.side is not None and side != self.side:
            raise OrderBookInvariantError("Level side mismatch")
        seen_ids: set[int] = set()
        total = strategy = market = 0
        for order in self.orders:
            if order.id in seen_ids:
                raise OrderBookInvariantError(f"Duplicate order id {order.id} in level")
            seen_ids.add(order.id)
            if order.status not in {OrderStatus.NEW, OrderStatus.PARTIAL}:
                raise OrderBookInvariantError(f"Non-live order {order.id} retained")
            if order.price_ticks != self.price_ticks:
                raise OrderBookInvariantError(f"Wrong-price order {order.id} retained")
            if side is not None and order.side != side:
                raise OrderBookInvariantError(f"Wrong-side order {order.id} retained")
            leaves = order.leaves_qty_lots
            if leaves <= 0:
                raise OrderBookInvariantError(f"Zero-lot order {order.id} retained")
            total += leaves
            if order.liquidity_category == LiquidityCategory.STRATEGY:
                strategy += leaves
            else:
                market += leaves
        self._validate_cached_aggregates()
        if (
            self.level_qty_lots,
            self.strategy_qty_lots,
            self.market_qty_lots,
        ) != (total, strategy, market):
            raise OrderBookInvariantError(
                f"Level aggregate mismatch at {side} {self.price_ticks}"
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
                f"{side.value} {level.price_ticks}"
            )
        if order.tif != TimeInForce.GTC:
            raise InvalidBookMutation(
                f"IOC order {order.id} cannot rest in book {self.symbol}"
            )
        if order.order_type != OrderType.LIMIT or order.price_ticks is None:
            raise InvalidBookMutation(
                f"Resting order {order.id} must have a limit price"
            )

        levels = self.bids if order.side == Side.BUY else self.asks
        reverse = order.side == Side.BUY
        insertion_index = len(levels)
        existing_level = None
        for index, level in enumerate(levels):
            if level.price_ticks == order.price_ticks:
                insertion_index = index
                existing_level = level
                break
            if (reverse and level.price_ticks < order.price_ticks) or (
                not reverse and level.price_ticks > order.price_ticks
            ):
                insertion_index = index
                break

        level = existing_level or BookLevel(
            price_ticks=order.price_ticks,
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
        fill_qty_lots: int,
        fill_price_ticks: Optional[int] = None,
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
                f"side={side.value}, price_ticks={level.price_ticks}"
            )
        resolved_price = (
            order.price_ticks if fill_price_ticks is None else fill_price_ticks
        )
        if resolved_price is None:
            raise InvalidBookMutation(f"Resting order {order.id} has no fill price")
        level._validate_resting_order(order)
        order.validate_fill(fill_qty_lots, resolved_price)
        old_leaves = order.leaves_qty_lots
        _, _, new_leaves = order._preview_fill(
            fill_qty_lots,
            resolved_price,
        )
        level._updated_aggregates(
            order,
            -(old_leaves - new_leaves),
        )

    def apply_fill(
        self,
        order: Order,
        fill_qty_lots: int,
        fill_price_ticks: Optional[int] = None,
    ) -> BookFillResult:
        """Atomically fill one resting order and clean up its level/index."""
        self.validate_fill(order, fill_qty_lots, fill_price_ticks)
        side, level = self._order_index[order.id]
        levels = self.bids if side == Side.BUY else self.asks
        level_index = self._level_identity_index(levels, level)
        if level_index is None:
            raise OrderBookInvariantError(
                f"Indexed level vanished for order {order.id}: "
                f"side={side.value}, price_ticks={level.price_ticks}"
            )

        applied_lots, fully_filled = level.apply_fill(
            order,
            fill_qty_lots,
            fill_price_ticks,
        )
        level_removed = False
        if fully_filled:
            del self._order_index[order.id]
            if not level.orders:
                del levels[level_index]
                level_removed = True

        return BookFillResult(
            order_id=order.id,
            fill_qty_lots=applied_lots,
            leaves_qty_lots=order.leaves_qty_lots,
            fully_filled=fully_filled,
            level_removed=level_removed,
        )

    def decrement_order(self, order: Order, quantity_lots: int) -> bool:
        """Apply an STP decrement to a resting order and clean indexes."""
        location = self._order_index.get(order.id)
        if location is None:
            raise InvalidBookMutation(
                f"Order {order.id} is not resting in book {self.symbol}"
            )
        side, level = location
        levels = self.bids if side == Side.BUY else self.asks
        level_index = self._level_identity_index(levels, level)
        if level_index is None:
            raise OrderBookInvariantError(f"Indexed level missing for order {order.id}")
        removed = level.decrement_order(order, quantity_lots)
        if removed:
            del self._order_index[order.id]
            if not level.orders:
                del levels[level_index]
        return removed

    def remove_order(self, order: Order) -> bool:
        """Remove a resting order once; a non-resting removal is a no-op."""
        location = self._order_index.get(order.id)
        if location is None:
            return False
        side, level = location
        if level._order_index(order) is None:
            raise OrderBookInvariantError(
                f"Order index points to a queue without order {order.id}: "
                f"side={side.value}, price_ticks={level.price_ticks}"
            )
        levels = self.bids if side == Side.BUY else self.asks
        level_index = self._level_identity_index(levels, level)
        if level_index is None:
            raise OrderBookInvariantError(
                f"Indexed level missing while removing order {order.id}: "
                f"side={side.value}, price_ticks={level.price_ticks}"
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

    def get_best_bid_ticks(self) -> Optional[int]:
        return self.bids[0].price_ticks if self.bids else None

    def get_best_ask_ticks(self) -> Optional[int]:
        return self.asks[0].price_ticks if self.asks else None

    def is_resting(self, order_id: int) -> bool:
        return order_id in self._order_index

    def assert_invariants(self) -> None:
        """Validate every level, FIFO membership, and lookup entry."""
        seen_orders: Dict[int, Tuple[Side, int, Order, BookLevel]] = {}
        for side, levels in (
            (Side.BUY, self.bids),
            (Side.SELL, self.asks),
        ):
            prices = [level.price_ticks for level in levels]
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
                            f"side={side.value}, "
                            f"price_ticks={level.price_ticks}"
                        )
                    if order.id in seen_orders:
                        other_side, other_price, _, _ = seen_orders[order.id]
                        raise OrderBookInvariantError(
                            f"Order {order.id} appears multiple times: "
                            f"{other_side.value} {other_price} and "
                            f"{side.value} {level.price_ticks}"
                        )
                    seen_orders[order.id] = (
                        side,
                        level.price_ticks,
                        order,
                        level,
                    )

        if self.bids:
            actual_best_bid = max(level.price_ticks for level in self.bids)
            if self.get_best_bid_ticks() != actual_best_bid:
                raise OrderBookInvariantError(
                    f"Best bid mismatch in {self.symbol}: "
                    f"cached={self.get_best_bid_ticks()}, "
                    f"actual={actual_best_bid}"
                )
        if self.asks:
            actual_best_ask = min(level.price_ticks for level in self.asks)
            if self.get_best_ask_ticks() != actual_best_ask:
                raise OrderBookInvariantError(
                    f"Best ask mismatch in {self.symbol}: "
                    f"cached={self.get_best_ask_ticks()}, "
                    f"actual={actual_best_ask}"
                )

        if self.bids and self.asks:
            best_bid = self.bids[0].price_ticks
            best_ask = self.asks[0].price_ticks
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

    price_ticks: int
    level_qty_lots: int
    strategy_qty_lots: int
    market_qty_lots: int
    order_count: int

    def __post_init__(self) -> None:
        if self.price_ticks <= 0:
            raise ValueError("Snapshot price ticks must be positive")
        for name, value in (
            ("level_qty_lots", self.level_qty_lots),
            ("strategy_qty_lots", self.strategy_qty_lots),
            ("market_qty_lots", self.market_qty_lots),
        ):
            if value < 0:
                raise ValueError(f"Snapshot {name} must be non-negative")
        if self.level_qty_lots <= 0:
            raise ValueError("Empty price levels cannot be published")
        if self.level_qty_lots != self.strategy_qty_lots + self.market_qty_lots:
            raise ValueError("Snapshot category quantities must sum to total quantity")
        if self.order_count <= 0:
            raise ValueError("Snapshot order_count must be positive")


@dataclass(frozen=True, slots=True)
class TradeSnapshot:
    """Immutable trade payload published exactly with its committed action."""

    trade_id: str
    price_ticks: int
    qty_lots: int
    aggressor_side: Side
    timestamp: datetime

    def __post_init__(self) -> None:
        if not self.trade_id:
            raise ValueError("Trade snapshot requires a trade_id")
        if self.price_ticks <= 0:
            raise ValueError("Trade snapshot price ticks must be positive")
        if self.qty_lots <= 0:
            raise ValueError("Trade snapshot quantity lots must be positive")
        if self.timestamp.tzinfo is None:
            raise ValueError("Trade snapshot timestamp must be timezone-aware")
        if self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("Trade snapshot timestamp must be UTC")


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    """Immutable, globally sequenced view of committed exchange state."""

    symbol: str
    instrument: Instrument
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
            raise ValueError("Market-data snapshot timestamp must be timezone-aware")
        if self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("Market-data snapshot timestamp must be UTC")
        if self.instrument.symbol != self.symbol:
            raise ValueError("Snapshot instrument must match its symbol")
        bid_prices = tuple(level.price_ticks for level in self.bids)
        ask_prices = tuple(level.price_ticks for level in self.asks)
        if bid_prices != tuple(sorted(bid_prices, reverse=True)):
            raise ValueError("Snapshot bids must be sorted best to worst")
        if ask_prices != tuple(sorted(ask_prices)):
            raise ValueError("Snapshot asks must be sorted best to worst")


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Immutable, self-contained execution report captured at creation time."""

    exec_id: str
    order_id: int
    cl_ord_id: str
    symbol: str
    instrument: Instrument
    side: Side
    order_type: OrderType
    order_qty_lots: int
    order_price_ticks: Optional[int]
    tif: TimeInForce
    ord_status: OrderStatus
    exec_type: ExecutionType
    leaves_qty_lots: int
    cum_qty_lots: int
    canceled_qty_lots: int
    filled_notional_tick_lots: int
    submitted_at: datetime
    timestamp: datetime
    order_version: int
    exchange_order_id: Optional[str] = None
    run_generation: int = 0
    last_price_ticks: Optional[int] = None
    last_qty_lots: Optional[int] = None
    trade_id: Optional[int] = None
    exchange_trade_id: Optional[str] = None
    orig_cl_ord_id: Optional[str] = None
    reject_reason: Optional[str] = None
    text: Optional[str] = None
    session_id: Optional[str] = None
    session_alias: Optional[str] = None
    participant_id: Optional[str] = None
    cancel_ingress_sequence: Optional[int] = None

    def __post_init__(self) -> None:
        """Reject inconsistent status, execution type, and quantity snapshots."""
        try:
            resolved_exec_type = ExecutionType(self.exec_type)
        except ValueError as exc:
            raise ValueError(f"Unsupported execution type: {self.exec_type}") from exc
        if (
            self.instrument.symbol != self.symbol
            and self.ord_status != OrderStatus.REJECTED
        ):
            raise ValueError("Execution report instrument must match symbol")
        if self.order_type == OrderType.LIMIT:
            if self.ord_status != OrderStatus.REJECTED and (
                self.order_price_ticks is None or self.order_price_ticks <= 0
            ):
                raise ValueError("Limit report requires positive price ticks")
            if self.order_price_ticks is not None and self.order_price_ticks <= 0:
                raise ValueError("Price ticks cannot be non-positive")
        elif self.order_price_ticks is not None:
            raise ValueError("Market report cannot contain price ticks")
        if resolved_exec_type not in ORDER_STATUSES_BY_EXECUTION_TYPE:
            raise ValueError(f"Unsupported execution type: {self.exec_type}")
        object.__setattr__(self, "exec_type", resolved_exec_type)

        allowed_statuses = ORDER_STATUSES_BY_EXECUTION_TYPE[resolved_exec_type]
        if self.ord_status not in allowed_statuses:
            raise ValueError(
                f"Inconsistent execution report: exec_type={self.exec_type}, "
                f"ord_status={self.ord_status.value}"
            )

        if self.order_qty_lots < 0:
            raise ValueError("Execution report OrderQty cannot be negative")
        quantities = (
            self.cum_qty_lots,
            self.leaves_qty_lots,
            self.canceled_qty_lots,
            self.filled_notional_tick_lots,
        )
        if any(value < 0 for value in quantities):
            raise ValueError("Execution report quantities cannot be negative")
        if (
            self.cum_qty_lots > self.order_qty_lots
            or self.leaves_qty_lots > self.order_qty_lots
            or self.canceled_qty_lots > self.order_qty_lots
        ):
            raise ValueError("Execution report quantities cannot exceed order quantity")
        if (
            self.cum_qty_lots + self.leaves_qty_lots + self.canceled_qty_lots
            != self.order_qty_lots
        ):
            raise ValueError(
                "CumQty plus LeavesQty plus CanceledQty must equal OrderQty"
            )
        if not self.exec_id:
            raise ValueError("ExecID is required")

        if resolved_exec_type == ExecutionType.TRADE:
            if self.last_qty_lots is None or self.last_qty_lots <= 0:
                raise ValueError("TRADE reports require positive LastQty")
            if self.last_qty_lots > self.cum_qty_lots:
                raise ValueError("LastQty cannot exceed CumQty")
            if self.last_price_ticks is None or self.last_price_ticks <= 0:
                raise ValueError("TRADE reports require positive LastPx")
            if self.trade_id is None:
                raise ValueError("TRADE reports require a TradeID")
        elif self.last_qty_lots is not None or self.last_price_ticks is not None:
            raise ValueError("Only TRADE reports may contain LastQty or LastPx")

        if self.ord_status == OrderStatus.NEW:
            if self.cum_qty_lots != 0:
                raise ValueError("NEW execution reports must have zero CumQty")
            if self.canceled_qty_lots != 0:
                raise ValueError("NEW execution reports must have zero CanceledQty")
        elif self.ord_status == OrderStatus.PARTIAL:
            if self.cum_qty_lots <= 0:
                raise ValueError("PARTIAL reports require positive CumQty")
            if self.leaves_qty_lots <= 0:
                raise ValueError("PARTIAL reports require positive LeavesQty")
            if self.canceled_qty_lots != 0:
                raise ValueError("PARTIAL reports must have zero CanceledQty")
        elif self.ord_status == OrderStatus.FILLED:
            if self.cum_qty_lots != self.order_qty_lots:
                raise ValueError("FILLED reports require CumQty equal OrderQty")
            if self.leaves_qty_lots != 0:
                raise ValueError("FILLED execution reports require zero LeavesQty")
            if self.canceled_qty_lots != 0:
                raise ValueError("FILLED execution reports require zero CanceledQty")
        elif self.ord_status in {
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }:
            if self.leaves_qty_lots != 0:
                raise ValueError("Terminal non-fill reports require zero LeavesQty")
            if self.ord_status == OrderStatus.CANCELLED and self.canceled_qty_lots <= 0:
                raise ValueError("CANCELLED reports require positive CanceledQty")
        if self.cum_qty_lots == 0 and self.filled_notional_tick_lots != 0:
            raise ValueError("Unfilled reports require zero filled notional")
        if self.cum_qty_lots > 0 and self.filled_notional_tick_lots <= 0:
            raise ValueError("Filled reports require positive filled notional")

    @property
    def average_fill_price(self) -> Decimal:
        """Return the exact unrounded average captured by this report."""
        if self.cum_qty_lots == 0:
            return Decimal(0)
        return (
            self.instrument.tick_size
            * Decimal(self.filled_notional_tick_lots)
            / Decimal(self.cum_qty_lots)
        )


def create_execution_report(
    order: Order,
    *,
    exec_type: Union[ExecutionType, str],
    timestamp: datetime,
    exec_id: str,
    last_price_ticks: Optional[int] = None,
    last_qty_lots: Optional[int] = None,
    trade_id: Optional[int] = None,
    exchange_trade_id: Optional[str] = None,
    cl_ord_id: Optional[str] = None,
    orig_cl_ord_id: Optional[str] = None,
    text: Optional[str] = None,
    cancel_ingress_sequence: Optional[int] = None,
) -> ExecutionReport:
    """Capture and validate every execution-report value from ``order`` now."""
    try:
        execution_type = ExecutionType(exec_type)
    except ValueError as exc:
        raise ValueError(f"Unsupported execution type: {exec_type}") from exc

    return ExecutionReport(
        exec_id=exec_id,
        order_id=order.id,
        cl_ord_id=cl_ord_id or order.cl_ord_id,
        symbol=order.symbol,
        instrument=order.instrument,
        side=order.side,
        order_type=order.order_type,
        order_qty_lots=order.order_qty_lots,
        order_price_ticks=order.price_ticks,
        tif=order.tif,
        ord_status=order.status,
        exec_type=execution_type,
        leaves_qty_lots=order.leaves_qty_lots,
        cum_qty_lots=order.filled_qty_lots,
        canceled_qty_lots=order.canceled_qty_lots,
        filled_notional_tick_lots=order.filled_notional_tick_lots,
        last_price_ticks=last_price_ticks,
        last_qty_lots=last_qty_lots,
        trade_id=trade_id,
        orig_cl_ord_id=orig_cl_ord_id,
        reject_reason=order.reject_reason,
        text=text,
        session_id=order.session_id,
        session_alias=order.session_alias,
        participant_id=order.participant_id,
        cancel_ingress_sequence=cancel_ingress_sequence,
        submitted_at=order.ts_submitted,
        timestamp=timestamp,
        order_version=order.version,
        exchange_order_id=(
            order.exchange_order_id or f"G{order.run_generation:08d}-O{order.id:08d}"
        ),
        run_generation=order.run_generation,
        exchange_trade_id=(
            exchange_trade_id or f"G{order.run_generation:08d}-T{trade_id:08d}"
            if trade_id is not None
            else None
        ),
    )


def create_cancel_reject(
    command: CancelOrderCommand,
    *,
    reject_id: str,
    reason: CancelRejectReason,
    text: str,
    timestamp: datetime,
    order: Optional[Order] = None,
) -> CancelReject:
    """Capture a cancel rejection from immutable command/current scalar state."""
    return CancelReject(
        reject_id=reject_id,
        cancel_cl_ord_id=command.cancel_cl_ord_id or "",
        orig_cl_ord_id=command.orig_cl_ord_id or "",
        order_id=order.id if order else None,
        ord_status=order.status if order else None,
        reason=reason,
        text=text,
        session_id=command.session_id,
        session_alias=command.session_alias,
        timestamp=timestamp,
        run_generation=command.run_generation,
        ingress_sequence=command.ingress_sequence,
        exchange_order_id=(
            (order.exchange_order_id or f"G{order.run_generation:08d}-O{order.id:08d}")
            if order
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class OutboundFixDelivery:
    """Immutable state snapshot for one logical outbound FIX delivery."""

    delivery_id: str
    session_sequence: int
    run_generation: int
    destination: SessionIdentity
    message_type: FixDeliveryMessageType
    payload: Union[ExecutionReport, CancelReject]
    domain_id: str
    simulated_due_timestamp: datetime
    enqueued_at: datetime
    state: FixDeliveryState = FixDeliveryState.PENDING
    attempt_count: int = 0
    cycle_attempt_count: int = 0
    last_attempt_at: Optional[datetime] = None
    last_attempt_simulated_at: Optional[datetime] = None
    last_failure_reason: Optional[str] = None
    next_retry_timestamp: Optional[datetime] = None
    delivered_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.delivery_id:
            raise ValueError("Delivery ID is required")
        if self.session_sequence < 0:
            raise ValueError("Session delivery sequence cannot be negative")
        if self.run_generation < 0:
            raise ValueError("Delivery run generation cannot be negative")
        if not self.domain_id:
            raise ValueError("Delivery domain ID is required")
        if self.attempt_count < 0 or self.cycle_attempt_count < 0:
            raise ValueError("Delivery attempt counts cannot be negative")
        for name, timestamp in (
            ("simulated_due_timestamp", self.simulated_due_timestamp),
            ("enqueued_at", self.enqueued_at),
            ("last_attempt_at", self.last_attempt_at),
            ("last_attempt_simulated_at", self.last_attempt_simulated_at),
            ("next_retry_timestamp", self.next_retry_timestamp),
            ("delivered_at", self.delivered_at),
        ):
            if timestamp is not None and timestamp.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.message_type == FixDeliveryMessageType.EXECUTION_REPORT:
            if not isinstance(self.payload, ExecutionReport):
                raise TypeError("Execution-report delivery requires ExecutionReport")
            if self.domain_id != self.payload.exec_id:
                raise ValueError("Execution-report domain ID must equal ExecID")
        elif not isinstance(self.payload, CancelReject):
            raise TypeError("Cancel-reject delivery requires CancelReject")
        elif self.domain_id != self.payload.reject_id:
            raise ValueError("Cancel-reject domain ID must equal reject_id")


@dataclass(frozen=True, slots=True)
class FixDeliveryQueueSnapshot:
    """Process-lifetime operational snapshot of the FIX delivery queue."""

    enqueued: int
    queue_depth: int
    pending: int
    due: int
    retry_pending: int
    delivered: int
    permanently_failed: int
    abandoned: int
    sessions_with_backlog: int
    attempts: int
    retries: int
    failures: int
    retained_delivered_records: int
    oldest_pending_age_seconds: Optional[float]


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

    run_generation: int
    order_id: int
    session_id: Optional[str]
    sim_submitted_at: datetime
    sim_received_at: Optional[datetime] = None
    sim_processed_at: Optional[datetime] = None
    sim_report_created_at: Optional[datetime] = None
    sim_report_delivered_at: Optional[datetime] = None
    match_loop_duration_ns: Optional[int] = None


@dataclass(slots=True)
class CancelLatencyTrace:
    """Latency tracker-owned mutable state for one cancel request."""

    run_generation: int
    ingress_sequence: int
    session_id: str
    cancel_cl_ord_id: str
    orig_cl_ord_id: str
    sim_received_at: datetime
    order_id: Optional[int] = None
    sim_arrived_at: Optional[datetime] = None
    sim_report_created_at: Optional[datetime] = None
    sim_report_delivered_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class LatencyMeasurement:
    """Immutable latency sample emitted at a well-defined clock boundary."""

    order_id: Optional[int]
    session_id: Optional[str]
    metric: str
    value: float
    unit: str
    timestamp: datetime
    report_exec_id: Optional[str] = None
    run_generation: Optional[int] = None
    ingress_sequence: Optional[int] = None
    cl_ord_id: Optional[str] = None
    orig_cl_ord_id: Optional[str] = None


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
    cancel_accepted: int
    cancel_unknown_order: int
    cancel_already_terminal: int
    cancel_wrong_owner: int
    cancel_duplicate_request: int
    cancel_stale_run_generation: int
    cancel_exchange_unavailable: int
    execution_reports: int
    trades: int
    traded_quantity: str
    traded_notional: str
    canceled_quantity: str
    active_orders: int
    resting_orders: int
    order_book_levels: int
    connected_sessions: int
    average_match_loop_duration_us: Optional[float]
    average_book_commit_duration_us: Optional[float]
    average_engine_command_duration_us: Optional[float]
    average_report_creation_duration_us: Optional[float]
    average_serialization_duration_us: Optional[float]
    average_sim_inbound_latency_ms: Optional[float]
    average_sim_engine_latency_ms: Optional[float]
    average_sim_outbound_latency_ms: Optional[float]
    average_sim_end_to_end_latency_ms: Optional[float]
    average_sim_cancel_inbound_latency_ms: Optional[float]
    average_sim_cancel_engine_latency_ms: Optional[float]
    average_sim_cancel_outbound_latency_ms: Optional[float]
    average_sim_cancel_end_to_end_latency_ms: Optional[float]
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
            "cancellations": {
                "accepted": self.cancel_accepted,
                "unknown_order": self.cancel_unknown_order,
                "already_terminal": self.cancel_already_terminal,
                "wrong_owner": self.cancel_wrong_owner,
                "duplicate_request": self.cancel_duplicate_request,
                "stale_run_generation": self.cancel_stale_run_generation,
                "exchange_unavailable": self.cancel_exchange_unavailable,
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
                "average_match_loop_duration_us": (self.average_match_loop_duration_us),
                "average_book_commit_duration_us": (
                    self.average_book_commit_duration_us
                ),
                "average_engine_command_duration_us": (
                    self.average_engine_command_duration_us
                ),
                "average_report_creation_duration_us": (
                    self.average_report_creation_duration_us
                ),
                "average_serialization_duration_us": (
                    self.average_serialization_duration_us
                ),
                "average_sim_inbound_latency_ms": (self.average_sim_inbound_latency_ms),
                "average_sim_engine_latency_ms": (self.average_sim_engine_latency_ms),
                "average_sim_outbound_latency_ms": (
                    self.average_sim_outbound_latency_ms
                ),
                "average_sim_end_to_end_latency_ms": (
                    self.average_sim_end_to_end_latency_ms
                ),
                "average_sim_cancel_inbound_latency_ms": (
                    self.average_sim_cancel_inbound_latency_ms
                ),
                "average_sim_cancel_engine_latency_ms": (
                    self.average_sim_cancel_engine_latency_ms
                ),
                "average_sim_cancel_outbound_latency_ms": (
                    self.average_sim_cancel_outbound_latency_ms
                ),
                "average_sim_cancel_end_to_end_latency_ms": (
                    self.average_sim_cancel_end_to_end_latency_ms
                ),
                "invalid_measurements": self.invalid_latency_measurements,
            },
            "persistence": {
                "failures": self.metric_sink_failures,
                "buffered_records": self.buffered_metric_records,
            },
        }

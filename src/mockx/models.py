"""Core data models for the mock exchange."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, FrozenSet, Mapping
from enum import Enum
from types import MappingProxyType
import math
import time


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


class InvalidOrderTransition(RuntimeError):
    """Raised when engine code attempts an unsupported lifecycle transition."""


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


EXECUTION_STATUS_BY_TYPE: Mapping[str, OrderStatus] = MappingProxyType({
    "NEW": OrderStatus.NEW,
    "PARTIAL": OrderStatus.PARTIAL,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
})


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
    avg_fill_price: Optional[float] = None
    reject_reason: Optional[str] = None
    session_id: Optional[str] = None
    session_alias: Optional[str] = None
    version: int = 0
    processed: bool = False

    def transition_to(self, new_status: OrderStatus) -> None:
        """Apply one explicitly supported order lifecycle transition."""
        if new_status not in ORDER_STATE_TRANSITIONS[self.status]:
            raise InvalidOrderTransition(
                f"Invalid order transition for order {self.id}: "
                f"{self.status.value} -> {new_status.value}"
            )
        self.status = new_status

@dataclass(frozen=True, slots=True)
class ExecutionParams:
    """Configuration for execution behavior."""
    network_latency_ms: int  # Round-trip
    engine_latency_ms: int
    partial_fill_prob: float  # 0..1
    reject_prob: float  # 0..1
    rand_seed: int  # For deterministic replay

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
    buyer_session_alias: str
    seller_session_alias: str

@dataclass
class BookLevel:
    """Single price level in the order book."""
    price: float
    qty: float
    orders: deque[Order] = field(default_factory=deque)
    strategy_qty: float = 0.0
    market_qty: float = 0.0  # For heatmap visualization

@dataclass
class OrderBook:
    """Limit order book for a single symbol."""
    symbol: str
    bids: deque[BookLevel] = field(default_factory=deque)  # Sorted descending
    asks: deque[BookLevel] = field(default_factory=deque)  # Sorted ascending
    
    def add_bid(self, order: Order) -> None:
        """Add a buy order to the book."""
        self._add_order(order, self.bids, reverse=True)
    
    def add_ask(self, order: Order) -> None:
        """Add a sell order to the book."""
        self._add_order(order, self.asks, reverse=False)
    
    def _add_order(self, order: Order, levels: deque[BookLevel], reverse: bool) -> None:
        """Add an order to the appropriate side of the book."""
        # Find or create the price level
        level_idx = None
        for i, level in enumerate(levels):
            if level.price == order.price:
                level_idx = i
                break
            elif (reverse and level.price < order.price) or (not reverse and level.price > order.price):
                # Insert new level here
                levels.insert(i, BookLevel(price=order.price, qty=0.0))
                level_idx = i
                break

        if level_idx is None:
            # Add at the end
            levels.append(BookLevel(price=order.price, qty=0.0))
            level_idx = len(levels) - 1

        # Add order to the level
        levels[level_idx].orders.append(order)
        levels[level_idx].qty += order.qty - order.filled_qty
        if order.session_alias == 'strategy_bot':
            levels[level_idx].strategy_qty += order.qty - order.filled_qty
        levels[level_idx].market_qty = levels[level_idx].qty - levels[level_idx].strategy_qty

    def remove_order(self, order: Order) -> None:
        """Remove an order from the book."""
        levels = self.bids if order.side == Side.BUY else self.asks
        for i, level in enumerate(levels):
            if order in level.orders:
                level.orders.remove(order)
                level.qty -= (order.qty - order.filled_qty)
                if order.session_alias == 'strategy_bot':
                    level.strategy_qty -= (order.qty - order.filled_qty)
                level.market_qty = level.qty - level.strategy_qty
                # Remove empty levels
                if not level.orders:
                    del levels[i]
                break
    
    def update_order_qty(self, order: Order) -> None:
        """Update order quantity after partial fill."""
        levels = self.bids if order.side == Side.BUY else self.asks
        
        for level in levels:
            if order in level.orders:
                # Recalculate level quantity
                level.qty = sum(o.qty - o.filled_qty for o in level.orders)
                break
    
    def get_best_bid(self) -> Optional[float]:
        """Get the best bid price."""
        return self.bids[0].price if self.bids else None
    
    def get_best_ask(self) -> Optional[float]:
        """Get the best ask price."""
        return self.asks[0].price if self.asks else None
    
    def get_mid_price(self) -> Optional[float]:
        """Get the mid-market price."""
        bid = self.get_best_bid()
        ask = self.get_best_ask()
        
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return None

@dataclass(frozen=True, slots=True)
class MarketDataUpdate:
    """Market data update event."""
    symbol: str
    timestamp: datetime
    bid_levels: List[BookLevel]
    ask_levels: List[BookLevel]
    trades: List[Trade] = field(default_factory=list)

@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Immutable, self-contained execution report captured at creation time."""

    order_id: int
    cl_ord_id: str
    symbol: str
    side: Side
    order_qty: float
    order_price: Optional[float]
    tif: TimeInForce
    ord_status: OrderStatus
    exec_type: str
    leaves_qty: float
    cum_qty: float
    avg_px: float
    submitted_at: datetime
    timestamp: datetime
    order_version: int
    last_px: Optional[float] = None
    last_qty: Optional[float] = None
    reject_reason: Optional[str] = None
    text: Optional[str] = None
    session_id: Optional[str] = None
    session_alias: Optional[str] = None

    def __post_init__(self) -> None:
        """Reject inconsistent status, execution type, and quantity snapshots."""
        expected_status = EXECUTION_STATUS_BY_TYPE.get(self.exec_type)
        if expected_status is None:
            raise ValueError(f"Unsupported execution type: {self.exec_type}")
        if self.ord_status != expected_status:
            raise ValueError(
                f"Inconsistent execution report: exec_type={self.exec_type}, "
                f"ord_status={self.ord_status.value}"
            )

        quantities = (self.order_qty, self.cum_qty, self.leaves_qty)
        if not all(math.isfinite(value) for value in quantities):
            raise ValueError("Execution report quantities must be finite")
        if self.order_qty < 0 or self.cum_qty < 0 or self.leaves_qty < 0:
            raise ValueError("Execution report quantities cannot be negative")
        if self.cum_qty > self.order_qty or self.leaves_qty > self.order_qty:
            raise ValueError("Execution report quantities cannot exceed order quantity")
        if not math.isclose(
            self.cum_qty + self.leaves_qty,
            self.order_qty,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError("CumQty plus LeavesQty must equal OrderQty")
        if self.ord_status == OrderStatus.FILLED and not math.isclose(
            self.leaves_qty, 0.0, abs_tol=1e-9
        ):
            raise ValueError("FILLED execution reports must have zero LeavesQty")
        if self.ord_status == OrderStatus.NEW and not math.isclose(
            self.cum_qty, 0.0, abs_tol=1e-9
        ):
            raise ValueError("NEW execution reports must have zero CumQty")


def create_execution_report(
    order: Order,
    *,
    exec_type: str,
    timestamp: datetime,
    last_px: Optional[float] = None,
    last_qty: Optional[float] = None,
    text: Optional[str] = None,
) -> ExecutionReport:
    """Capture and validate every execution-report value from ``order`` now."""
    cum_qty = order.filled_qty
    leaves_qty = max(order.qty - cum_qty, 0.0)
    return ExecutionReport(
        order_id=order.id,
        cl_ord_id=order.cl_ord_id,
        symbol=order.symbol,
        side=order.side,
        order_qty=order.qty,
        order_price=order.price,
        tif=order.tif,
        ord_status=order.status,
        exec_type=exec_type,
        leaves_qty=leaves_qty,
        cum_qty=cum_qty,
        avg_px=order.avg_fill_price or 0.0,
        last_px=last_px,
        last_qty=last_qty,
        reject_reason=order.reject_reason,
        text=text,
        session_id=order.session_id,
        session_alias=order.session_alias,
        submitted_at=order.ts_submitted,
        timestamp=timestamp,
        order_version=order.version,
    )

@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    """Event scheduled for future execution."""
    timestamp: datetime
    sequence: int
    event_type: str
    data: object

    def __lt__(self, other: "ScheduledEvent") -> bool:
        """Order events by timestamp, preserving scheduling order for ties."""
        return (self.timestamp, self.sequence) < (other.timestamp, other.sequence)
    
@dataclass(frozen=True, slots=True)
class DualTimestamp:
    """Captures both simulated and wall-clock time."""
    simulated: datetime
    real: float = field(default_factory=time.perf_counter)

@dataclass(slots=True)
class OrderTimingData:
    """Complete timing information for an order."""
    submitted: Optional[DualTimestamp] = None
    arrived: Optional[DualTimestamp] = None
    processing_start: Optional[DualTimestamp] = None
    processing_end: Optional[DualTimestamp] = None
    response_sent: Optional[DualTimestamp] = None

@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    """Complete exchange configuration."""
    exchange_name: str
    symbols: List[str]
    latency: Dict[str, int]
    execution: Dict[str, float]
    fix: Dict[str, any]
    admin_api: Dict[str, int]
    prometheus: Dict[str, int]

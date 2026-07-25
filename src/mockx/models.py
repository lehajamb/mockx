"""Core data models for the mock exchange."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional, List, Dict
from enum import Enum
import time


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderStatus(Enum):
    NEW = "NEW"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

class TimeInForce(Enum):
    GTC = "GTC"  # Good Till Cancel
    IOC = "IOC"  # Immediate Or Cancel

@dataclass(slots=True)
class Order:
    """Represents a trading order."""
    id: int
    cl_ord_id: str
    symbol: str
    side: Side
    qty: float
    price: Optional[float]  # None = market order
    tif: TimeInForce
    ts_submitted: datetime  # Exchange timestamp
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_fill_price: Optional[float] = None
    reject_reason: Optional[str] = None
    session_alias: Optional[str] = None

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
    """Execution report to be sent via FIX."""
    order: Order
    exec_type: str  # NEW, PARTIAL, FILLED, REJECTED
    last_px: Optional[float] = None
    last_qty: Optional[float] = None
    cum_qty: Optional[float] = None
    avg_px: Optional[float] = None
    text: Optional[str] = None  # Reject reason or other text
    timestamp: Optional[datetime] = None

@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    """Event scheduled for future execution."""
    timestamp: datetime
    sequence: int
    event_type: str
    data: Dict

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

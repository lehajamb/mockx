"""Core matching engine for the mock exchange."""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from random import Random
import itertools

from .models import (
    Order, OrderBook, Trade, Side, OrderStatus, TimeInForce,
    ExecutionParams, ExecutionReport, MarketDataUpdate, NewOrderCommand,
    ProcessOrderCommand, CancelOrderCommand, TERMINAL_ORDER_STATUSES,
    create_execution_report,
)
from .latency import LatencyScheduler

logger = logging.getLogger(__name__)


class MatchingEngine:
    """
    Deterministic order matching engine with configurable execution behavior.
    
    Features:
    - Price-time priority matching
    - Configurable slippage
    - Partial fill simulation
    - Order rejection simulation
    - Deterministic randomness via seeded PRNG
    """    
    
    def __init__(self, exec_params: ExecutionParams, scheduler: LatencyScheduler):
        """
        Initializes the engine with its dependencies.
        """
        # --- Dependencies (don't change on reset) ---
        self.exec_params = exec_params
        self.scheduler = scheduler
        self.rng = Random(exec_params.rand_seed)
        
        # Order books by symbol
        self.books: Dict[str, OrderBook] = {}
        
        # Order tracking
        self.orders: Dict[int, Order] = {}
        self.next_order_id = 1
        self.next_trade_id = 1
        
        # Callbacks
        self.on_execution_report = None
        self.on_market_data_update = None
        self.on_trade = None
        
    def reset(self):
        """
        Resets the matching engine to a clean state.
        - Clears all order books.
        - Resets order and trade ID counters.
        - Clears the internal order tracking dictionary.
        """
        logger.info("Resetting matching engine state.")
        
        # Re-initialize order books for all known symbols
        for symbol in self.books.keys():
            self.books[symbol] = OrderBook(symbol=symbol)
            
        # Clear order tracking
        self.orders.clear()
        
        # Reset ID counters
        self.next_order_id = 1
        self.next_trade_id = 1
        
        # Reset the scheduler
        self.scheduler.reset()
        
    def add_symbol(self, symbol: str) -> None:
        """Add a tradable symbol."""
        if symbol not in self.books:
            self.books[symbol] = OrderBook(symbol=symbol)
            logger.info(f"Added symbol: {symbol}")
            
    def submit_order(self, command: NewOrderCommand) -> int:
        """
        Create and submit an engine-owned order for processing.

        Returns the assigned order ID.
        """
        if not isinstance(command, NewOrderCommand):
            raise TypeError("submit_order requires NewOrderCommand")

        order = Order(
            id=self.next_order_id,
            cl_ord_id=command.cl_ord_id,
            symbol=command.symbol,
            side=command.side,
            qty=command.qty,
            price=command.price,
            tif=command.tif,
            ts_submitted=command.submitted_at,
            session_id=command.session_id,
            session_alias=command.session_alias,
        )
        self.next_order_id += 1
        self.orders[order.id] = order

        process_command = ProcessOrderCommand(
            order_id=order.id,
            expected_version=order.version,
        )
        self.scheduler.schedule_order_processing(
            process_command,
            self.exec_params.network_latency_ms,
            self.exec_params.engine_latency_ms,
            session_alias=order.session_alias,
        )

        logger.debug(f"Order submitted: {order.cl_ord_id} ({order.side.value} {order.qty} @ {order.price})")
        return order.id

    def process_order(self, command: ProcessOrderCommand) -> None:
        """Process one current, not-yet-consumed initial-order command."""
        if not isinstance(command, ProcessOrderCommand):
            raise TypeError("process_order requires ProcessOrderCommand")

        order = self.orders.get(command.order_id)
        if not order:
            logger.info("Ignoring process command for missing order %s", command.order_id)
            return

        if command.expected_version != order.version:
            logger.info(
                "Ignoring stale process command for order %s: expected version %s, current version %s",
                command.order_id,
                command.expected_version,
                order.version,
            )
            return

        if order.status in TERMINAL_ORDER_STATUSES:
            logger.info(
                "Ignoring process command for terminal order %s in state %s",
                order.id,
                order.status.value,
            )
            return

        if order.status != OrderStatus.PENDING_NEW or order.processed:
            logger.info(
                "Ignoring duplicate process command for order %s in state %s",
                order.id,
                order.status.value,
            )
            return

        # Claim the command before any matching or report side effects.
        order.processed = True

        # Check for rejection
        if self._should_reject(order):
            self._reject_order(order, "Simulated rejection")
            return
            
        # Ensure symbol exists
        if order.symbol not in self.books:
            self._reject_order(order, f"Unknown symbol: {order.symbol}")
            return
            
        book = self.books[order.symbol]
        
        # Dispatch to the correct processor
        if order.price is None:
            self._process_market_order(order, book)
        else:
            self._process_limit_order(order, book)
            
        # Send market data update
        self._send_market_data_update(order.symbol)
        
    def _should_reject(self, order: Order) -> bool:
        """Determine if order should be rejected based on probability."""
        return self.rng.random() < self.exec_params.reject_prob
        
    def _reject_order(self, order: Order, reason: str) -> None:
        """Reject an order."""
        order.transition_to(OrderStatus.REJECTED)
        order.reject_reason = reason

        report = create_execution_report(
            order,
            exec_type="REJECTED",
            text=reason,
            timestamp=self.scheduler.current_time,
        )
        self._send_execution_report(report)
        
        logger.info(f"Order rejected: {order.cl_ord_id} - {reason}")
    
    # [CHANGED] Method simplified due to bug fix
    def _process_market_order(self, order: Order, book: OrderBook) -> None:
        """Process a market order by walking the book."""
        # A market order cannot execute if the opposite side of the book is empty.
        if (order.side == Side.BUY and not book.asks) or (order.side == Side.SELL and not book.bids):
            self._reject_order(order, "No liquidity available")
            return

        self._match_and_report(order, book)

    # [CHANGED] Method simplified due to bug fix and refactoring
    def _process_limit_order(self, order: Order, book: OrderBook) -> None:
        """Process a limit order."""
        # Match: Attempt to match the order against the book.
        self._match_and_report(order, book)
        
        # Add the nonterminal GTC remainder to the book.
        if order.status not in TERMINAL_ORDER_STATUSES:
            # A no-fill order becomes NEW when it is accepted onto the book.
            # A PARTIAL report has already acknowledged a partially filled order.
            if order.status == OrderStatus.PENDING_NEW:
                order.transition_to(OrderStatus.NEW)
                report = create_execution_report(
                    order,
                    exec_type="NEW",
                    timestamp=self.scheduler.current_time,
                )
                self._send_execution_report(report)

            if order.side == Side.BUY:
                book.add_bid(order)
            else:
                book.add_ask(order)

    # [NEW] Helper method created from refactoring
    def _match_and_report(self, aggressor: Order, book: OrderBook):
        """Centralized matching and reporting logic."""
        trades = self._match_aggressive_order(aggressor, book)
        
        # If no trades occurred, there's nothing more to do for the aggressor
        if not trades:
            return

        last_trade = trades[-1]
        
        # Determine the final status and send the correct execution report
        if aggressor.filled_qty >= aggressor.qty:
            aggressor.transition_to(OrderStatus.FILLED)
            exec_type = "FILLED"
            text = None
        elif aggressor.tif == TimeInForce.IOC:
            aggressor.transition_to(OrderStatus.CANCELLED)
            exec_type = "CANCELLED"
            text = "IOC order cancelled - unfilled portion"
        else: # Partially filled GTC order
            aggressor.transition_to(OrderStatus.PARTIAL)
            exec_type = "PARTIAL"
            text = None

        report = create_execution_report(
            aggressor,
            exec_type=exec_type,
            last_px=last_trade.price,
            last_qty=last_trade.qty,
            timestamp=self.scheduler.current_time,
            text=text,
        )
        self._send_execution_report(report)

    def _match_aggressive_order(self, aggressor: Order, book: OrderBook) -> List[Trade]:
        """
        Match an aggressive order against the book, returning a list of trades.
        This version is optimized to avoid creating list copies in hot paths.
        """
        trades: List[Trade] = []
        remaining_qty = aggressor.qty - aggressor.filled_qty
        
        # Determine max fill for partial fill simulation
        max_fill_qty = remaining_qty
        if self._should_partial_fill(aggressor):
            fill_fraction = self.rng.uniform(0.3, 0.9)  # Fill 30-90% of order
            max_fill_qty = round(remaining_qty * fill_fraction, 8)
        
        passive_levels = book.asks if aggressor.side == Side.BUY else book.bids

        # Continue as long as there's quantity and available levels
        level_idx = 0
        while remaining_qty > 0 and passive_levels and aggressor.filled_qty < max_fill_qty:
            best_level = passive_levels[0]

            # Price check for limit orders
            if aggressor.price is not None:
                if aggressor.side == Side.BUY and best_level.price > aggressor.price:
                    break
                if aggressor.side == Side.SELL and best_level.price < aggressor.price:
                    break
            
            # Match against orders at the best price level
            while remaining_qty > 0 and best_level.orders and aggressor.filled_qty < max_fill_qty:
                passive_order = best_level.orders[0]
                
                passive_remaining = passive_order.qty - passive_order.filled_qty
                match_qty = min(remaining_qty, passive_remaining)
                
                # Ensure we don't exceed max fill
                match_qty = min(match_qty, max_fill_qty - aggressor.filled_qty)
                
                if match_qty <= 0:
                    break

                exec_price = passive_order.price
                trade = self._create_trade(aggressor, passive_order, exec_price, match_qty, level_idx)
                trades.append(trade)

                self._update_order_fill(aggressor, exec_price, match_qty)
                self._update_order_fill(passive_order, exec_price, match_qty)
                remaining_qty -= match_qty

                self._report_passive_fill(passive_order, exec_price, match_qty)

                # If passive order is filled, remove it from the level
                if passive_order.filled_qty >= passive_order.qty:
                    best_level.orders.popleft()

            # If the level is now empty, remove it from the book
            if not best_level.orders:
                level_idx += 1
                passive_levels.popleft()

        for trade in trades:
            if self.on_trade:
                self.on_trade(trade)

        return trades
        
    # [NEW] Helper method for DRY code
    def _update_order_fill(self, order: Order, price: float, qty: float):
        """Update an order's filled quantity and average price."""
        if order.avg_fill_price is None:
            order.avg_fill_price = price
        else:
            total_value = (order.avg_fill_price * order.filled_qty) + (price * qty)
            order.avg_fill_price = total_value / (order.filled_qty + qty)
        order.filled_qty += qty
    
    # [NEW] Helper method for DRY code
    def _report_passive_fill(self, passive_order: Order, price: float, qty: float):
        """Send an execution report for a passive order that was hit."""
        if passive_order.filled_qty >= passive_order.qty:
            passive_order.transition_to(OrderStatus.FILLED)
            exec_type = "FILLED"
        else:
            passive_order.transition_to(OrderStatus.PARTIAL)
            exec_type = "PARTIAL"

        report = create_execution_report(
            passive_order,
            exec_type=exec_type,
            last_px=price,
            last_qty=qty,
            timestamp=self.scheduler.current_time,
        )
        self._send_execution_report(report)

    def _should_partial_fill(self, order: Order) -> bool:
        """
        Determine if a partial fill should be simulated.
        This is now only checked once per aggressive order to reduce overhead.
        """
        # Only apply to aggressive orders, not every single passive match
        if order.status == OrderStatus.PENDING_NEW:
             return self.rng.random() < self.exec_params.partial_fill_prob
        return False
        
    def _create_trade(self, aggressor: Order, passive: Order,
                      price: float, qty: float, level: int) -> Trade:
        """Create a trade record."""
        trade = Trade(
            id=self.next_trade_id,
            symbol=aggressor.symbol,
            price=price,
            qty=qty,
            volume=qty * price,
            buyer_order_id=aggressor.id if aggressor.side == Side.BUY else passive.id,
            seller_order_id=passive.id if passive.side == Side.SELL else aggressor.id,
            timestamp=self.scheduler.current_time,
            level=level,
            buyer_session_alias=aggressor.session_alias if aggressor.side == Side.BUY else passive.session_alias,
            seller_session_alias=passive.session_alias if passive.side == Side.SELL else aggressor.session_alias
        )
        logger.debug(f"Trade: {qty} @ {price} ({aggressor.cl_ord_id} x {passive.cl_ord_id}) at level {level}")
        self.next_trade_id += 1
        return trade
        
    def _send_execution_report(self, report: ExecutionReport) -> None:
        """Send execution report with network latency."""
        if self.on_execution_report:
            self.on_execution_report(report)

        self.scheduler.schedule_execution_report(
            report,
            self.exec_params.network_latency_ms,
        )
            
    def _send_market_data_update(self, symbol: str) -> None:
        """Send market data update for a symbol."""
        if symbol not in self.books:
            return
            
        book = self.books[symbol]
        
        # Create snapshot
        update = MarketDataUpdate(
            symbol=symbol,
            timestamp=self.scheduler.current_time,
            bid_levels=list(book.bids),
            ask_levels=list(book.asks)
        )
        
        if self.on_market_data_update:
            # The timestamp is already in the update object
            self.on_market_data_update(update)
            
    def get_order_book_snapshot(self, symbol: str) -> Optional[OrderBook]:
        """Get current order book snapshot."""
        return self.books.get(symbol)
        
    def cancel_order(self, command: CancelOrderCommand) -> Optional[str]:
        """Cancel an order and return a rejection reason when it cannot be done."""
        if not isinstance(command, CancelOrderCommand):
            raise TypeError("cancel_order requires CancelOrderCommand")

        order = self.orders.get(command.order_id)
        if not order:
            reason = f"Order not found: {command.orig_cl_ord_id or command.order_id}"
            logger.warning(reason)
            return reason

        if command.expected_version != order.version:
            reason = (
                f"Stale cancel command for order {order.id}: expected version "
                f"{command.expected_version}, current version {order.version}"
            )
            logger.info(reason)
            return reason

        # If order is already terminated, reject the cancel request
        if order.status in TERMINAL_ORDER_STATUSES:
            reason = f"Order already in terminal state: {order.status.value}"
            logger.info(reason)
            return reason

        # Remove from book if it's there
        if order.symbol in self.books:
            book = self.books[order.symbol]
            book.remove_order(order)

        # A successful cancel invalidates any queued initial-processing command.
        order.version += 1
        order.transition_to(OrderStatus.CANCELLED)

        # Send cancellation report
        report = create_execution_report(
            order,
            exec_type="CANCELLED",
            text="Order cancelled by request",
            timestamp=self.scheduler.current_time,
        )
        self._send_execution_report(report)
        return None

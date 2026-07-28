"""Core matching engine for the mock exchange."""

import logging
from datetime import datetime
from typing import Dict, List, Optional
from random import Random

from .identifiers import RunIdentifierScope
from .models import (
    CancellationOutcome, CancelReject, CancelRejectReason, ClientOrderKey,
    ExecutionType, Fill,
    MarketDataSnapshot, Order, OrderBook, PriceLevelSnapshot, Trade,
    TradeSnapshot, Side, OrderStatus, TimeInForce,
    ExecutionParams, ExecutionReport, NewOrderCommand,
    ProcessOrderCommand, CancelOrderCommand, TERMINAL_ORDER_STATUSES,
    QUANTITY_EPSILON, create_cancel_reject, create_execution_report,
)
from .latency import (
    LatencyScheduler,
    MonotonicClock,
    PerfCounterClock,
    monotonic_duration_ns,
)

logger = logging.getLogger(__name__)


class MatchingEngine:
    """
    Deterministic order matching engine with configurable execution behavior.
    
    Features:
    - Price-time priority matching
    - Configurable slippage
    - Natural partial fills from available liquidity
    - Order rejection simulation
    - Deterministic randomness via seeded PRNG
    """    
    
    def __init__(
        self,
        exec_params: ExecutionParams,
        scheduler: LatencyScheduler,
        monotonic_clock: Optional[MonotonicClock] = None,
    ):
        """
        Initializes the engine with its dependencies.
        """
        # --- Dependencies (don't change on reset) ---
        self.exec_params = exec_params
        self.scheduler = scheduler
        self.monotonic_clock = monotonic_clock or PerfCounterClock()
        self.random_seed = exec_params.rand_seed
        self.rng = Random(self.random_seed)
        self.identifier_scope = RunIdentifierScope(0)

        # Order books by symbol
        self.books: Dict[str, OrderBook] = {}
        
        # Order tracking
        self.orders: Dict[int, Order] = {}
        self.client_order_index: Dict[ClientOrderKey, int] = {}
        self.next_order_id = 1
        self.next_trade_id = 1
        # One deterministic sequence spans all symbols in this exchange.
        # It resets only when ``reset`` starts a new simulation session.
        self.market_data_sequence = 0
        
        # Callbacks
        self.on_execution_report = None
        self.on_market_data_update = None
        self.on_trade = None
        self.on_order_submitted = None
        self.on_cancel_request = None
        self.on_cancel_reject = None
        self.on_cancel_outcome = None
        self.on_matching_duration = None
        
    def reset_run_state(self) -> None:
        """Clear authoritative run state without touching the scheduler."""
        logger.info("Resetting matching engine run state.")

        # Re-initialize order books for all known symbols
        for symbol in tuple(self.books):
            self.books[symbol] = OrderBook(symbol=symbol)
            
        # Clear order tracking
        self.orders.clear()
        self.client_order_index.clear()
        
        # Reset ID counters
        self.next_order_id = 1
        self.next_trade_id = 1
        self.market_data_sequence = 0

    def reseed_rng(self) -> None:
        """Restore the configured deterministic RNG state."""
        self.rng.seed(self.random_seed)

    def install_identifier_scope(
        self,
        identifiers: RunIdentifierScope,
    ) -> None:
        self.identifier_scope = identifiers
        
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
        self.scheduler.ensure_running()

        client_key = None
        if command.session_id is not None:
            client_key = ClientOrderKey(
                session_id=command.session_id,
                cl_ord_id=command.cl_ord_id,
            )
            if client_key in self.client_order_index:
                raise ValueError(
                    "Duplicate ClOrdID in FIX session: "
                    f"{command.cl_ord_id}"
                )

        order = Order(
            id=self.next_order_id,
            exchange_order_id=self.identifier_scope.next_order_id(),
            run_generation=self.identifier_scope.generation,
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
        if client_key is not None:
            self.client_order_index[client_key] = order.id

        self._notify_observer(
            "order submission",
            self.on_order_submitted,
            order.id,
            command,
        )

        process_command = ProcessOrderCommand(
            order_id=order.id,
            expected_version=order.version,
        )
        self.scheduler.schedule_order_processing(
            process_command,
            self.exec_params.network_latency_ms,
            self.exec_params.engine_latency_ms,
            session_alias=order.session_alias,
            inbound_ms=self.exec_params.simulated_inbound_latency_ms,
        )

        logger.debug(f"Order submitted: {order.cl_ord_id} ({order.side.value} {order.qty} @ {order.price})")
        return order.id

    def process_order(self, command: ProcessOrderCommand) -> None:
        """Process one current, not-yet-consumed initial-order command."""
        if not isinstance(command, ProcessOrderCommand):
            raise TypeError("process_order requires ProcessOrderCommand")
        self.scheduler.ensure_running()

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

        # Calculate the deterministic rejection decision before claiming the
        # command. Any remaining exception after the claim is fail-stop at the
        # scheduler boundary; the engine does not claim transactional rollback.
        should_reject = self._should_reject(order)
        order.processed = True

        if should_reject:
            self._reject_order(order, "Simulated rejection")
            return
            
        # Ensure symbol exists
        if order.symbol not in self.books:
            self._reject_order(order, f"Unknown symbol: {order.symbol}")
            return
            
        book = self.books[order.symbol]
        
        # Dispatch to the correct processor
        if order.price is None:
            fills = self._process_market_order(order, book)
        else:
            fills = self._process_limit_order(order, book)

        book.assert_invariants()

        # Send market data update
        self._send_market_data_update(order.symbol, fills)
        
    def _should_reject(self, order: Order) -> bool:
        """Determine if order should be rejected based on probability."""
        return self.rng.random() < self.exec_params.reject_prob
        
    def _reject_order(self, order: Order, reason: str) -> None:
        """Reject an order."""
        order.validate_transition_to(OrderStatus.REJECTED)
        order.cancel_remaining()
        order.transition_to(OrderStatus.REJECTED)
        order.reject_reason = reason

        report = self._create_execution_report(
            order,
            exec_type=ExecutionType.REJECTED,
            text=reason,
        )
        self._send_execution_report(report)

        logger.info(f"Order rejected: {order.cl_ord_id} - {reason}")

    def _process_market_order(
        self,
        order: Order,
        book: OrderBook,
    ) -> List[Fill]:
        """Execute immediately; market orders never rest, regardless of TIF."""
        fills = self._run_matching(order, book)
        self._finalize_unfilled_remainder(order, book)
        return fills

    def _process_limit_order(
        self,
        order: Order,
        book: OrderBook,
    ) -> List[Fill]:
        """Match a limit order and apply its GTC/IOC remainder rule once."""
        fills = self._run_matching(order, book)
        self._finalize_unfilled_remainder(order, book)
        return fills

    def _run_matching(self, order: Order, book: OrderBook) -> List[Fill]:
        """Measure only the real duration of the matching function."""
        start_ns = self.monotonic_clock.now_ns()
        fills = self._match_aggressive_order(order, book)
        end_ns = self.monotonic_clock.now_ns()
        try:
            duration_ns = monotonic_duration_ns(
                start_ns,
                end_ns,
                context=f"matching order {order.id}",
            )
        except ValueError:
            logger.exception(
                "Invalid real matching timestamps",
                extra={"order_id": order.id, "session_id": order.session_id},
            )
        else:
            self._notify_observer(
                "matching duration",
                self.on_matching_duration,
                order.id,
                order.session_id,
                duration_ns,
                self.scheduler.current_time,
            )
        return fills

    def _match_aggressive_order(
        self,
        aggressor: Order,
        book: OrderBook,
    ) -> List[Fill]:
        """
        Match an aggressive order and return one immutable result per match.

        Each fill is committed to both orders and the book before reports are
        captured. Reports are then scheduled passive-first, aggressor-second;
        the scheduler sequence number preserves that order at equal timestamps.
        """
        fills: List[Fill] = []
        remaining_qty = aggressor.remaining_qty

        passive_levels = book.asks if aggressor.side == Side.BUY else book.bids

        # Continue as long as there's quantity and available levels
        level_idx = 0
        while (
            remaining_qty > QUANTITY_EPSILON
            and passive_levels
        ):
            best_level = passive_levels[0]

            # Price check for limit orders
            if aggressor.price is not None:
                if aggressor.side == Side.BUY and best_level.price > aggressor.price:
                    break
                if aggressor.side == Side.SELL and best_level.price < aggressor.price:
                    break
            
            # Match against orders at the best price level
            while (
                remaining_qty > QUANTITY_EPSILON
                and best_level.orders
            ):
                passive_order = best_level.orders[0]

                passive_remaining = passive_order.remaining_qty
                match_qty = min(remaining_qty, passive_remaining)

                if match_qty <= 0:
                    break

                exec_price = passive_order.price
                if exec_price is None:
                    raise RuntimeError(
                        f"Resting order {passive_order.id} has no limit price"
                    )

                # Validate both halves before changing either. The resting half
                # is then committed solely through the book mutation layer.
                aggressor.validate_fill(match_qty, exec_price)
                book.validate_fill(passive_order, match_qty, exec_price)

                aggressor_target = (
                    OrderStatus.FILLED
                    if aggressor.remaining_qty - match_qty
                    <= QUANTITY_EPSILON
                    else OrderStatus.PARTIAL
                )
                passive_target = (
                    OrderStatus.FILLED
                    if passive_remaining - match_qty <= QUANTITY_EPSILON
                    else OrderStatus.PARTIAL
                )
                aggressor.validate_transition_to(aggressor_target)
                passive_order.validate_transition_to(passive_target)
                fill = Fill(
                    trade_id=self.next_trade_id,
                    exchange_trade_id=self.identifier_scope.next_trade_id(),
                    run_generation=self.identifier_scope.generation,
                    symbol=aggressor.symbol,
                    price=exec_price,
                    qty=match_qty,
                    aggressor_order_id=aggressor.id,
                    passive_order_id=passive_order.id,
                    aggressor_side=aggressor.side,
                    timestamp=self.scheduler.current_time,
                )

                aggressor.apply_fill(match_qty, exec_price)
                fill_result = book.apply_fill(
                    passive_order,
                    match_qty,
                    exec_price,
                )
                remaining_qty = aggressor.remaining_qty

                self.next_trade_id += 1
                fills.append(fill)

                passive_order.transition_to(passive_target)
                aggressor.transition_to(aggressor_target)

                trade = self._create_trade(
                    fill,
                    aggressor,
                    passive_order,
                    level_idx,
                )
                self._notify_observer(
                    "trade",
                    self.on_trade,
                    trade,
                )

                self._send_execution_report(
                    self._create_execution_report(
                        passive_order,
                        exec_type=ExecutionType.TRADE,
                        last_px=fill.price,
                        last_qty=fill.qty,
                        trade_id=fill.trade_id,
                        exchange_trade_id=fill.exchange_trade_id,
                    )
                )
                self._send_execution_report(
                    self._create_execution_report(
                        aggressor,
                        exec_type=ExecutionType.TRADE,
                        last_px=fill.price,
                        last_qty=fill.qty,
                        trade_id=fill.trade_id,
                        exchange_trade_id=fill.exchange_trade_id,
                    )
                )

                if fill_result.level_removed:
                    level_idx += 1
                    break

        return fills

    def _finalize_unfilled_remainder(
        self,
        order: Order,
        book: OrderBook,
    ) -> None:
        """Apply the one post-match rule for every supported order variant."""
        if order.remaining_qty <= QUANTITY_EPSILON:
            return

        immediate_only = (
            order.price is None or order.tif == TimeInForce.IOC
        )
        if immediate_only:
            order.validate_transition_to(OrderStatus.CANCELLED)
            canceled_qty = order.cancel_remaining()
            order.transition_to(OrderStatus.CANCELLED)
            order_kind = "Market" if order.price is None else "IOC"
            report = self._create_execution_report(
                order,
                exec_type=ExecutionType.CANCELED,
                text=(
                    f"{order_kind} unexecuted remainder canceled: "
                    f"{canceled_qty}"
                ),
            )
            self._send_execution_report(report)
            return

        # Model A acknowledgement: publish NEW only for a wholly unfilled GTC
        # limit. A partially filled aggressor already emitted TRADE/PARTIAL and
        # rests without regressing to a NEW state or report.
        if order.filled_qty <= QUANTITY_EPSILON:
            order.validate_transition_to(OrderStatus.NEW)
        book.add_order(order)
        if order.filled_qty <= QUANTITY_EPSILON:
            order.transition_to(OrderStatus.NEW)
            report = self._create_execution_report(
                order,
                exec_type=ExecutionType.NEW,
            )
            self._send_execution_report(report)

    def _create_trade(
        self,
        fill: Fill,
        aggressor: Order,
        passive: Order,
        level: int,
    ) -> Trade:
        """Create a trade record."""
        trade = Trade(
            id=fill.trade_id,
            symbol=fill.symbol,
            price=fill.price,
            qty=fill.qty,
            volume=fill.qty * fill.price,
            buyer_order_id=aggressor.id if aggressor.side == Side.BUY else passive.id,
            seller_order_id=passive.id if passive.side == Side.SELL else aggressor.id,
            timestamp=fill.timestamp,
            level=level,
            buyer_session_alias=aggressor.session_alias if aggressor.side == Side.BUY else passive.session_alias,
            seller_session_alias=passive.session_alias if passive.side == Side.SELL else aggressor.session_alias,
            aggressor_order_id=fill.aggressor_order_id,
            passive_order_id=fill.passive_order_id,
            aggressor_side=fill.aggressor_side,
            exchange_trade_id=fill.exchange_trade_id,
            run_generation=fill.run_generation,
        )
        logger.debug(
            "Trade: %s @ %s (%s x %s) at level %s",
            fill.qty,
            fill.price,
            aggressor.cl_ord_id,
            passive.cl_ord_id,
            level,
        )
        return trade

    def _create_execution_report(
        self,
        order: Order,
        *,
        exec_type: ExecutionType,
        last_px: Optional[float] = None,
        last_qty: Optional[float] = None,
        trade_id: Optional[int] = None,
        exchange_trade_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
        orig_cl_ord_id: Optional[str] = None,
        text: Optional[str] = None,
        cancel_ingress_sequence: Optional[int] = None,
    ) -> ExecutionReport:
        """Capture a report with a deterministic, engine-owned ExecID."""
        exec_id = self.identifier_scope.next_exec_id()
        return create_execution_report(
            order,
            exec_type=exec_type,
            timestamp=self.scheduler.current_time,
            exec_id=exec_id,
            last_px=last_px,
            last_qty=last_qty,
            trade_id=trade_id,
            exchange_trade_id=exchange_trade_id,
            cl_ord_id=cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id,
            text=text,
            cancel_ingress_sequence=cancel_ingress_sequence,
        )
        
    def _send_execution_report(self, report: ExecutionReport) -> None:
        """Send execution report with network latency."""
        self._notify_observer(
            "execution report",
            self.on_execution_report,
            report,
        )

        self.scheduler.schedule_execution_report(
            report,
            self.exec_params.network_latency_ms,
            outbound_ms=self.exec_params.simulated_outbound_latency_ms,
        )
            
    @staticmethod
    def _snapshot_levels(book: OrderBook, side: Side):
        levels = book.bids if side == Side.BUY else book.asks
        snapshots = [
            PriceLevelSnapshot(
                price=float(level.price),
                qty=float(level.qty),
                strategy_qty=float(level.strategy_qty),
                market_qty=float(level.market_qty),
                order_count=len(level.orders),
            )
            for level in levels
            if level.qty > QUANTITY_EPSILON and level.orders
        ]
        return tuple(
            sorted(
                snapshots,
                key=lambda level: level.price,
                reverse=side == Side.BUY,
            )
        )

    def _create_market_data_snapshot(
        self,
        symbol: str,
        *,
        sequence: int,
        fills: tuple[Fill, ...] = (),
    ) -> MarketDataSnapshot:
        """Copy authoritative scalar state into a deeply immutable DTO."""
        if symbol not in self.books:
            raise KeyError(f"Unknown symbol: {symbol}")

        book = self.books[symbol]
        return MarketDataSnapshot(
            symbol=symbol,
            sequence=sequence,
            timestamp=self.scheduler.current_time,
            bids=self._snapshot_levels(book, Side.BUY),
            asks=self._snapshot_levels(book, Side.SELL),
            trades=tuple(
                TradeSnapshot(
                    trade_id=(
                        fill.exchange_trade_id
                        or f"G{fill.run_generation:08d}-T{fill.trade_id:08d}"
                    ),
                    price=float(fill.price),
                    qty=float(fill.qty),
                    aggressor_side=fill.aggressor_side,
                    timestamp=fill.timestamp,
                )
                for fill in fills
            ),
            run_generation=self.identifier_scope.generation,
        )

    def _send_market_data_update(
        self,
        symbol: str,
        fills: List[Fill] | tuple[Fill, ...] = (),
    ) -> None:
        """Publish one sequenced snapshot for one committed book action."""
        if symbol not in self.books:
            return

        next_sequence = self.market_data_sequence + 1
        update = self._create_market_data_snapshot(
            symbol,
            sequence=next_sequence,
            fills=tuple(fills),
        )
        self.market_data_sequence = next_sequence

        self._notify_observer(
            "market data",
            self.on_market_data_update,
            update,
        )

    def get_market_data_snapshot(
        self,
        symbol: str,
    ) -> Optional[MarketDataSnapshot]:
        """Return an immutable current snapshot without advancing sequence."""
        if symbol not in self.books:
            return None
        return self._create_market_data_snapshot(
            symbol,
            sequence=self.market_data_sequence,
        )
            
    def get_order_book_snapshot(self, symbol: str) -> Optional[OrderBook]:
        """Get current order book snapshot."""
        return self.books.get(symbol)
        
    def cancel_order(
        self,
        command: CancelOrderCommand,
    ) -> ExecutionReport | CancelReject:
        """Resolve and process one cancel against authoritative execution-time state."""
        if not isinstance(command, CancelOrderCommand):
            raise TypeError("cancel_order requires CancelOrderCommand")
        self.scheduler.ensure_running()

        self._notify_observer(
            "cancel request",
            self.on_cancel_request,
            command,
        )

        if command.run_generation != self.identifier_scope.generation:
            return self._cancel_reject(
                command,
                reason=CancelRejectReason.INVALID_REQUEST,
                text=(
                    "Stale cancel run generation: "
                    f"{command.run_generation}; active generation is "
                    f"{self.identifier_scope.generation}"
                ),
                outcome=CancellationOutcome.STALE_RUN_GENERATION,
            )

        if command.duplicate_request:
            return self._cancel_reject(
                command,
                reason=CancelRejectReason.INVALID_REQUEST,
                text=(
                    "Duplicate cancel request ClOrdID in FIX session: "
                    f"{command.cancel_cl_ord_id}"
                ),
                outcome=CancellationOutcome.DUPLICATE_REQUEST,
            )

        client_key = ClientOrderKey(
            session_id=command.session_id,
            cl_ord_id=command.orig_cl_ord_id,
        )
        order_id = self.client_order_index.get(client_key)
        order = self.orders.get(order_id) if order_id is not None else None
        if order is None:
            foreign_orders = sorted(
                (
                    candidate
                    for candidate in self.orders.values()
                    if (
                        candidate.cl_ord_id == command.orig_cl_ord_id
                        and candidate.session_id != command.session_id
                    )
                ),
                key=lambda candidate: candidate.id,
            )
            if foreign_orders:
                return self._cancel_reject(
                    command,
                    order=foreign_orders[0],
                    reason=CancelRejectReason.SESSION_MISMATCH,
                    text=(
                        f"OrigClOrdID {command.orig_cl_ord_id} belongs to a "
                        "different FIX session"
                    ),
                    outcome=CancellationOutcome.WRONG_OWNER,
                )
            return self._cancel_reject(
                command,
                reason=CancelRejectReason.UNKNOWN_ORDER,
                text=(
                    "Order not found: "
                    f"{command.orig_cl_ord_id}"
                ),
                outcome=CancellationOutcome.UNKNOWN_ORDER,
            )

        if order.status == OrderStatus.CANCELLED:
            return self._cancel_reject(
                command,
                order=order,
                reason=CancelRejectReason.ALREADY_CANCELED,
                text=(
                    f"Stale cancel command: order {order.id} is already canceled"
                ),
                outcome=CancellationOutcome.ALREADY_TERMINAL,
            )
        if order.status in {OrderStatus.FILLED, OrderStatus.REJECTED}:
            return self._cancel_reject(
                command,
                order=order,
                reason=CancelRejectReason.TOO_LATE_TO_CANCEL,
                text=(
                    f"Order already in terminal state: {order.status.value}"
                ),
                outcome=CancellationOutcome.ALREADY_TERMINAL,
            )

        book = self.books.get(order.symbol)
        is_pending = (
            order.status == OrderStatus.PENDING_NEW and not order.processed
        )
        is_resting = book is not None and book.is_resting(order.id)
        if not is_pending and not is_resting:
            return self._cancel_reject(
                command,
                order=order,
                reason=CancelRejectReason.TOO_LATE_TO_CANCEL,
                text=f"Order {order.id} has no live cancellable remainder",
                outcome=CancellationOutcome.ALREADY_TERMINAL,
            )
        if order.remaining_qty <= QUANTITY_EPSILON:
            return self._cancel_reject(
                command,
                order=order,
                reason=CancelRejectReason.TOO_LATE_TO_CANCEL,
                text=f"Order {order.id} has no live quantity",
                outcome=CancellationOutcome.ALREADY_TERMINAL,
            )

        order.validate_transition_to(OrderStatus.CANCELLED)
        if is_resting:
            if not book.remove_order(order):
                raise RuntimeError(
                    f"Resting order {order.id} could not be removed"
                )
            book.assert_invariants()

        # Invalidate any queued initial-processing command before publishing.
        order.version += 1
        order.cancel_remaining()
        order.transition_to(OrderStatus.CANCELLED)

        report = self._create_execution_report(
            order,
            exec_type=ExecutionType.CANCELED,
            cl_ord_id=command.cancel_cl_ord_id or order.cl_ord_id,
            orig_cl_ord_id=order.cl_ord_id,
            text="Order cancelled by request",
            cancel_ingress_sequence=command.ingress_sequence,
        )
        self._notify_observer(
            "execution report",
            self.on_execution_report,
            report,
        )
        self._notify_observer(
            "cancel outcome",
            self.on_cancel_outcome,
            command,
            CancellationOutcome.ACCEPTED,
        )
        if is_resting:
            self._send_market_data_update(order.symbol)
        return report

    def _cancel_reject(
        self,
        command: CancelOrderCommand,
        *,
        reason: CancelRejectReason,
        text: str,
        order: Optional[Order] = None,
        outcome: CancellationOutcome,
    ) -> CancelReject:
        reject = create_cancel_reject(
            command,
            reason=reason,
            text=text,
            timestamp=self.scheduler.current_time,
            order=order,
        )
        self._notify_observer(
            "cancel reject",
            self.on_cancel_reject,
            reject,
        )
        self._notify_observer(
            "cancel outcome",
            self.on_cancel_outcome,
            command,
            outcome,
        )
        logger.info("Cancel rejected: %s", reject.text)
        return reject

    def active_order_count(self) -> int:
        """Return active orders from authoritative lifecycle state."""
        return sum(
            order.status not in TERMINAL_ORDER_STATUSES
            for order in self.orders.values()
        )

    def resting_order_count(self) -> int:
        """Return the number of orders physically resting in all books."""
        return sum(
            len(level.orders)
            for book in self.books.values()
            for level in (*book.bids, *book.asks)
        )

    def order_book_level_count(self) -> int:
        """Return the number of non-empty price levels in all books."""
        return sum(
            len(book.bids) + len(book.asks)
            for book in self.books.values()
        )

    @staticmethod
    def _notify_observer(name: str, callback, *args) -> None:
        """Keep an observability failure outside authoritative engine state."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("%s observer failed", name)

"""Deterministic integer tick/lot matching engine."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from random import Random
from typing import Callable, Dict, List, Optional

from .identifiers import RunIdentifierScope
from .instruments import InstrumentCatalog
from .latency import (
    LatencyScheduler,
    MonotonicClock,
    PerfCounterClock,
    monotonic_duration_ns,
)
from .metrics import (
    BOOK_COMMIT_DURATION,
    ENGINE_COMMAND_DURATION,
    MATCH_LOOP_DURATION,
    REPORT_CREATION_DURATION,
)
from .models import (
    CancellationOutcome,
    CancelOrderCommand,
    ReplaceOrderCommand,
    ReplacementOutcome,
    OrderCancelReject,
    CancelRejectReason,
    ClientOrderKey,
    ExecutionParams,
    ExecutionReport,
    ExecutionType,
    DisplayedOrderAdded,
    DisplayedOrderDeleted,
    DisplayedOrderEvent,
    DisplayedOrderExecuted,
    DisplayedOrderReduced,
    Fill,
    MarketDataSnapshot,
    NewOrderCommand,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    PriceLevelSnapshot,
    ProcessOrderCommand,
    SelfTradePrevention,
    Side,
    TERMINAL_ORDER_STATUSES,
    TimeInForce,
    Trade,
    TradeSnapshot,
    create_order_cancel_reject,
    create_execution_report,
)

logger = logging.getLogger(__name__)


class DuplicateClientOrderID(ValueError):
    """A process-lifetime duplicate within one session namespace."""


@dataclass(frozen=True, slots=True)
class SeededOrder:
    symbol: str
    session_id: str
    cl_ord_id: str
    order_id: str


@dataclass(frozen=True, slots=True)
class InitialBookState:
    """Validated run-local state, prepared without mutating the live engine."""

    identifiers: RunIdentifierScope
    books: Dict[str, OrderBook]
    orders: Dict[int, Order]
    client_order_index: Dict[ClientOrderKey, int]
    displayed_events: tuple[DisplayedOrderAdded, ...]
    seeded_orders: tuple[SeededOrder, ...]


class MatchingEngine:
    """Price-time-priority engine whose authoritative arithmetic is integral."""

    def __init__(
        self,
        exec_params: ExecutionParams,
        scheduler: LatencyScheduler,
        instruments: InstrumentCatalog,
        *,
        monotonic_clock: Optional[MonotonicClock] = None,
    ):
        self.exec_params = exec_params
        self.scheduler = scheduler
        self.instruments = instruments
        self.monotonic_clock = monotonic_clock or PerfCounterClock()
        self.random_seed = exec_params.rand_seed
        self.rng = Random(self.random_seed)
        self.identifier_scope = RunIdentifierScope(0)

        self.books: Dict[str, OrderBook] = {
            symbol: OrderBook(symbol=symbol) for symbol in self.instruments.symbols
        }
        self.orders: Dict[int, Order] = {}
        self.client_order_index: Dict[ClientOrderKey, int] = {}
        self._claimed_client_order_ids: set[ClientOrderKey] = set()
        self.next_order_id = 1
        self.next_trade_id = 1
        self.next_displayed_order_ref = 1
        self.market_data_sequence = 0

        self.on_execution_report = None
        self.on_market_data_update = None
        self.on_trade = None
        self.on_self_trade_prevention = None
        self.on_displayed_order_event: Optional[
            Callable[[DisplayedOrderEvent], object]
        ] = None
        self.on_order_submitted = None
        self.on_cancel_request = None
        self.on_cancel_reject = None
        self.on_cancel_outcome = None
        self.on_replace_request = None
        self.on_replace_reject = None
        self.on_replace_outcome = None
        self.on_cpu_duration = None
        self._displayed_event_buffer: Optional[List[DisplayedOrderEvent]] = None

    def reset_run_state(self) -> None:
        """Clear authoritative run state without touching the scheduler."""
        logger.info("Resetting matching engine run state")
        self.books = {
            symbol: OrderBook(symbol=symbol) for symbol in self.instruments.symbols
        }
        self.orders.clear()
        self.client_order_index.clear()
        self.next_order_id = 1
        self.next_trade_id = 1
        self.next_displayed_order_ref = 1
        self.market_data_sequence = 0

    def reseed_rng(self) -> None:
        self.rng.seed(self.random_seed)

    def prepare_initial_book(
        self,
        commands: tuple[NewOrderCommand, ...],
        identifiers: RunIdentifierScope,
    ) -> InitialBookState:
        """Build a fresh resting book in input FIFO order without matching."""
        books = {symbol: OrderBook(symbol=symbol) for symbol in self.instruments.symbols}
        orders: Dict[int, Order] = {}
        index: Dict[ClientOrderKey, int] = {}
        events: list[DisplayedOrderAdded] = []
        seeded: list[SeededOrder] = []
        for order_id, command in enumerate(commands, start=1):
            if command.order_type != OrderType.LIMIT or command.tif != TimeInForce.GTC:
                raise ValueError("Initial book orders must be GTC limits")
            if command.session_id is None:
                raise ValueError("Initial book orders require a configured FIX session")
            key = ClientOrderKey(command.session_id, command.cl_ord_id)
            if key in index or key in self._claimed_client_order_ids:
                raise DuplicateClientOrderID(f"Duplicate ClOrdID in session: {command.cl_ord_id}")
            order = Order(
                id=order_id,
                exchange_order_id=identifiers.next_order_id(),
                run_generation=identifiers.generation,
                cl_ord_id=command.cl_ord_id,
                symbol=command.symbol,
                side=command.side,
                order_type=command.order_type,
                order_qty_lots=command.order_qty_lots,
                price_ticks=command.price_ticks,
                instrument=self.instruments.require(command.symbol),
                tif=command.tif,
                ts_submitted=command.submitted_at,
                participant_id=command.participant_id,
                session_id=command.session_id,
                session_alias=command.session_alias,
                status=OrderStatus.NEW,
                processed=True,
                displayed_order_ref=order_id,
            )
            books[order.symbol].add_order(order)
            orders[order_id] = order
            index[key] = order_id
            events.append(DisplayedOrderAdded(
                run_generation=order.run_generation, order_id=order.id,
                order_ref_num=order_id, symbol=order.symbol, side=order.side,
                price_ticks=order.price_ticks, qty_lots=order.leaves_qty_lots,
                timestamp=order.ts_submitted,
            ))
            seeded.append(SeededOrder(
                order.symbol, command.session_id, order.cl_ord_id, order.exchange_order_id,
            ))
        for book in books.values():
            book.assert_invariants()
        return InitialBookState(identifiers, books, orders, index, tuple(events), tuple(seeded))

    def validate_initial_book_claims(self, initial: InitialBookState) -> None:
        if self._claimed_client_order_ids.intersection(initial.client_order_index):
            raise DuplicateClientOrderID("Snapshot contains an already claimed ClOrdID")

    def install_initial_book(self, initial: InitialBookState) -> None:
        """Commit prepared state only inside a fresh serialized run transition."""
        if self.orders or any(book.bids or book.asks for book in self.books.values()):
            raise RuntimeError("Initial book installation requires an empty engine")
        if self.identifier_scope is not initial.identifiers:
            raise RuntimeError("Initial book identifier scope does not match the run")
        self.validate_initial_book_claims(initial)
        self.books = initial.books
        self.orders = initial.orders
        self.client_order_index = initial.client_order_index
        self._claimed_client_order_ids.update(initial.client_order_index)
        self.next_order_id = len(initial.orders) + 1
        self.next_displayed_order_ref = len(initial.orders) + 1

    def publish_initial_book(self, initial: InitialBookState) -> None:
        """Publish bootstrap liquidity after the run's feed has started."""
        self._publish_displayed_events(list(initial.displayed_events))
        for symbol in dict.fromkeys(order.symbol for order in initial.orders.values()):
            self._send_market_data_update(symbol)

    def install_identifier_scope(
        self,
        identifiers: RunIdentifierScope,
    ) -> None:
        self.identifier_scope = identifiers

    def submit_order(self, command: NewOrderCommand) -> int:
        """Register one validated command and schedule its initial processing."""
        if not isinstance(command, NewOrderCommand):
            raise TypeError("submit_order requires NewOrderCommand")
        self.scheduler.ensure_running()
        instrument = self.instruments.get(command.symbol)
        if instrument is None:
            raise KeyError(f"Unsupported symbol: {command.symbol}")

        client_key: ClientOrderKey | None = None
        if command.session_id is not None:
            client_key = ClientOrderKey(
                session_id=command.session_id,
                cl_ord_id=command.cl_ord_id,
            )
            if client_key in self._claimed_client_order_ids:
                raise DuplicateClientOrderID(
                    "Duplicate ClOrdID in session: " f"{command.cl_ord_id}"
                )

        order = Order(
            id=self.next_order_id,
            exchange_order_id=self.identifier_scope.next_order_id(),
            run_generation=self.identifier_scope.generation,
            cl_ord_id=command.cl_ord_id,
            symbol=command.symbol,
            side=command.side,
            order_type=command.order_type,
            order_qty_lots=command.order_qty_lots,
            price_ticks=command.price_ticks,
            instrument=instrument,
            tif=command.tif,
            ts_submitted=command.submitted_at,
            session_id=command.session_id,
            session_alias=command.session_alias,
            participant_id=command.participant_id,
        )
        self.next_order_id += 1
        self.orders[order.id] = order
        if client_key is not None:
            self.client_order_index[client_key] = order.id
            self._claimed_client_order_ids.add(client_key)

        self._notify_observer(
            "order submission",
            self.on_order_submitted,
            order.id,
            command,
        )
        self.scheduler.schedule_order_processing(
            ProcessOrderCommand(
                order_id=order.id,
                expected_version=order.version,
            ),
            self.exec_params.network_latency_ms,
            self.exec_params.engine_latency_ms,
            session_alias=order.session_alias,
            inbound_ms=self.exec_params.simulated_inbound_latency_ms,
        )
        logger.debug(
            "Order submitted: %s (%s %s lots @ %s ticks)",
            order.cl_ord_id,
            order.side.value,
            order.order_qty_lots,
            order.price_ticks,
        )
        return order.id

    def process_order(self, command: ProcessOrderCommand) -> None:
        """Process one current, not-yet-consumed initial-order command."""
        if not isinstance(command, ProcessOrderCommand):
            raise TypeError("process_order requires ProcessOrderCommand")
        self.scheduler.ensure_running()
        order = self.orders.get(command.order_id)
        if order is None:
            return
        if (
            command.expected_version != order.version
            or order.status in TERMINAL_ORDER_STATUSES
            or order.status != OrderStatus.PENDING_NEW
            or order.processed
        ):
            return

        command_start_ns = self.monotonic_clock.now_ns()
        displayed_events: List[DisplayedOrderEvent] = []
        self._displayed_event_buffer = displayed_events
        try:
            should_reject = self.rng.random() < self.exec_params.reject_prob
            order.processed = True
            if should_reject:
                self._reject_order(
                    order,
                    "SIMULATED_REJECTION: Simulated rejection",
                )
                return

            book = self.books[order.symbol]
            fills = self._run_matching(order, book)
            self._finalize_unfilled_remainder(order, book)
            book.assert_invariants()
            self._publish_displayed_events(displayed_events)
            self._send_market_data_update(order.symbol, fills)
        finally:
            self._displayed_event_buffer = None
            self._record_cpu_duration(
                ENGINE_COMMAND_DURATION,
                order.id,
                order.session_id,
                command_start_ns,
                context=f"process order command {order.id}",
            )

    def reject_pending_order(self, command: ProcessOrderCommand, reason: str) -> bool:
        """Settle an unprocessed command once without touching live liquidity."""
        if not isinstance(command, ProcessOrderCommand):
            raise TypeError("reject_pending_order requires ProcessOrderCommand")
        order = self.orders.get(command.order_id)
        if (
            order is None
            or command.expected_version != order.version
            or order.status != OrderStatus.PENDING_NEW
            or order.processed
        ):
            return False
        order.processed = True
        self._reject_order(order, reason)
        return True

    def reject_cancel_unavailable(
        self, command: CancelOrderCommand, reason: str
    ) -> OrderCancelReject:
        """Reject a queued cancel against an immutable snapshot of its owner order."""
        if not isinstance(command, CancelOrderCommand):
            raise TypeError("reject_cancel_unavailable requires CancelOrderCommand")
        self._notify_observer("cancel request", self.on_cancel_request, command)
        order_id = self.client_order_index.get(
            ClientOrderKey(command.session_id, command.orig_cl_ord_id)
        )
        order = self.orders.get(order_id) if order_id is not None else None
        return self._cancel_reject(
            command,
            reason=CancelRejectReason.EXCHANGE_UNAVAILABLE,
            text=reason,
            order=order,
            outcome=CancellationOutcome.EXCHANGE_UNAVAILABLE,
        )

    def _reject_order(self, order: Order, reason: str) -> None:
        order.validate_transition_to(OrderStatus.REJECTED)
        order.cancel_remaining()
        order.transition_to(OrderStatus.REJECTED)
        order.reject_reason = reason
        self._send_execution_report(
            self._create_execution_report(
                order,
                exec_type=ExecutionType.REJECTED,
                text=reason,
            )
        )

    def _run_matching(
        self,
        order: Order,
        book: OrderBook,
    ) -> List[Fill]:
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
                "match loop duration",
                self.on_cpu_duration,
                MATCH_LOOP_DURATION,
                order.id,
                order.session_id,
                duration_ns,
                self.scheduler.current_time,
            )
        return fills

    @staticmethod
    def _is_self_cross(aggressor: Order, passive: Order) -> bool:
        return aggressor.participant_id == passive.participant_id

    def _prevent_self_trade(
        self,
        aggressor: Order,
        passive: Order,
        book: OrderBook,
        match_qty_lots: int,
    ) -> None:
        """Cancel both complete remainders before a self-trade can exist."""
        if not self._is_self_cross(aggressor, passive):
            raise RuntimeError("Self-trade prevention requires common ownership")
        if not book.remove_order(passive):
            raise RuntimeError(f"Self-trade passive order {passive.id} was not resting")

        text = (
            "SELF_TRADE_PREVENTION: canceled passive and aggressor; "
            f"prevented_qty={aggressor.instrument.format_quantity(match_qty_lots)}"
        )
        passive.validate_transition_to(OrderStatus.CANCELLED)
        passive.cancel_remaining()
        passive.transition_to(OrderStatus.CANCELLED)
        self._send_execution_report(
            self._create_execution_report(
                passive,
                exec_type=ExecutionType.CANCELED,
                text=text,
            )
        )

        aggressor.validate_transition_to(OrderStatus.CANCELLED)
        aggressor.cancel_remaining()
        aggressor.transition_to(OrderStatus.CANCELLED)
        self._send_execution_report(
            self._create_execution_report(
                aggressor,
                exec_type=ExecutionType.CANCELED,
                text=text,
            )
        )
        self._queue_displayed_event(
            DisplayedOrderDeleted(
                run_generation=passive.run_generation,
                order_id=passive.id,
                order_ref_num=self._displayed_reference(passive),
                symbol=passive.symbol,
                timestamp=self.scheduler.current_time,
            )
        )
        passive.displayed_order_ref = None
        self._notify_observer(
            "self-trade prevention",
            self.on_self_trade_prevention,
            SelfTradePrevention(
                symbol=aggressor.symbol,
                participant_id=aggressor.participant_id,
                aggressor_order_id=aggressor.id,
                passive_order_id=passive.id,
                prevented_qty_lots=match_qty_lots,
                timestamp=self.scheduler.current_time,
            ),
        )

    def _match_aggressive_order(
        self,
        aggressor: Order,
        book: OrderBook,
    ) -> List[Fill]:
        fills: List[Fill] = []
        passive_levels = book.asks if aggressor.side == Side.BUY else book.bids
        level_index = 0
        while aggressor.leaves_qty_lots > 0 and passive_levels:
            best_level = passive_levels[0]
            if aggressor.price_ticks is not None:
                if (
                    aggressor.side == Side.BUY
                    and best_level.price_ticks > aggressor.price_ticks
                ):
                    break
                if (
                    aggressor.side == Side.SELL
                    and best_level.price_ticks < aggressor.price_ticks
                ):
                    break

            while aggressor.leaves_qty_lots > 0 and best_level.orders:
                passive = best_level.orders[0]
                match_qty = min(
                    aggressor.leaves_qty_lots,
                    passive.leaves_qty_lots,
                )
                if self._is_self_cross(aggressor, passive):
                    self._prevent_self_trade(
                        aggressor,
                        passive,
                        book,
                        match_qty,
                    )
                    return fills

                exec_price = passive.price_ticks
                if exec_price is None:
                    raise RuntimeError(f"Resting order {passive.id} has no limit price")
                aggressor.validate_fill(match_qty, exec_price)
                book.validate_fill(passive, match_qty, exec_price)
                aggressor_target = (
                    OrderStatus.FILLED
                    if aggressor.leaves_qty_lots == match_qty
                    else OrderStatus.PARTIAL
                )
                passive_target = (
                    OrderStatus.FILLED
                    if passive.leaves_qty_lots == match_qty
                    else OrderStatus.PARTIAL
                )
                aggressor.validate_transition_to(aggressor_target)
                passive.validate_transition_to(passive_target)

                fill = Fill(
                    trade_id=self.next_trade_id,
                    exchange_trade_id=self.identifier_scope.next_trade_id(),
                    run_generation=self.identifier_scope.generation,
                    symbol=aggressor.symbol,
                    price_ticks=exec_price,
                    qty_lots=match_qty,
                    aggressor_order_id=aggressor.id,
                    passive_order_id=passive.id,
                    aggressor_side=aggressor.side,
                    timestamp=self.scheduler.current_time,
                )
                commit_start_ns = self.monotonic_clock.now_ns()
                try:
                    aggressor.apply_fill(match_qty, exec_price)
                    fill_result = book.apply_fill(
                        passive,
                        match_qty,
                        exec_price,
                    )
                    passive.transition_to(passive_target)
                    aggressor.transition_to(aggressor_target)
                finally:
                    self._record_cpu_duration(
                        BOOK_COMMIT_DURATION,
                        aggressor.id,
                        aggressor.session_id,
                        commit_start_ns,
                        context=f"fill commit for order {aggressor.id}",
                    )
                self.next_trade_id += 1
                fills.append(fill)
                self._queue_displayed_event(
                    DisplayedOrderExecuted(
                        run_generation=fill.run_generation,
                        order_id=passive.id,
                        order_ref_num=self._displayed_reference(passive),
                        symbol=fill.symbol,
                        displayed_price_ticks=exec_price,
                        execution_price_ticks=fill.price_ticks,
                        executed_qty_lots=fill.qty_lots,
                        match_number=fill.trade_id,
                        timestamp=fill.timestamp,
                    )
                )
                if fill_result.fully_filled:
                    passive.displayed_order_ref = None

                trade = self._create_trade(
                    fill,
                    aggressor,
                    passive,
                    level_index,
                )
                self._notify_observer("trade", self.on_trade, trade)
                self._send_execution_report(
                    self._create_execution_report(
                        passive,
                        exec_type=ExecutionType.TRADE,
                        last_price_ticks=fill.price_ticks,
                        last_qty_lots=fill.qty_lots,
                        trade_id=fill.trade_id,
                        exchange_trade_id=fill.exchange_trade_id,
                    )
                )
                self._send_execution_report(
                    self._create_execution_report(
                        aggressor,
                        exec_type=ExecutionType.TRADE,
                        last_price_ticks=fill.price_ticks,
                        last_qty_lots=fill.qty_lots,
                        trade_id=fill.trade_id,
                        exchange_trade_id=fill.exchange_trade_id,
                    )
                )
                if fill_result.level_removed:
                    level_index += 1
                    break
        return fills

    def _finalize_unfilled_remainder(
        self,
        order: Order,
        book: OrderBook,
    ) -> None:
        if order.leaves_qty_lots == 0:
            return
        if order.status == OrderStatus.CANCELLED:
            return
        immediate_only = (
            order.order_type == OrderType.MARKET or order.tif == TimeInForce.IOC
        )
        if immediate_only:
            order.validate_transition_to(OrderStatus.CANCELLED)
            canceled = order.cancel_remaining()
            order.transition_to(OrderStatus.CANCELLED)
            kind = "Market" if order.order_type == OrderType.MARKET else "IOC"
            self._send_execution_report(
                self._create_execution_report(
                    order,
                    exec_type=ExecutionType.CANCELED,
                    text=(
                        f"{kind} unexecuted remainder canceled: "
                        f"{order.instrument.format_quantity(canceled)}"
                    ),
                )
            )
            return

        if order.filled_qty_lots == 0 and order.status == OrderStatus.PENDING_NEW:
            order.transition_to(OrderStatus.NEW)
            publish_new = True
        else:
            publish_new = False
        commit_start_ns = self.monotonic_clock.now_ns()
        try:
            if not 0 < self.next_displayed_order_ref < 2**64:
                raise RuntimeError("Displayed order reference space exhausted")
            book.add_order(order)
            order.displayed_order_ref = self.next_displayed_order_ref
            self.next_displayed_order_ref += 1
        finally:
            self._record_cpu_duration(
                BOOK_COMMIT_DURATION,
                order.id,
                order.session_id,
                commit_start_ns,
                context=f"rest commit for order {order.id}",
            )
        if order.price_ticks is None:
            raise RuntimeError(f"Resting order {order.id} has no limit price")
        self._queue_displayed_event(
            DisplayedOrderAdded(
                run_generation=order.run_generation,
                order_id=order.id,
                order_ref_num=self._displayed_reference(order),
                symbol=order.symbol,
                side=order.side,
                price_ticks=order.price_ticks,
                qty_lots=order.leaves_qty_lots,
                timestamp=self.scheduler.current_time,
            )
        )
        if publish_new:
            self._send_execution_report(
                self._create_execution_report(
                    order,
                    exec_type=ExecutionType.NEW,
                )
            )

    def _create_trade(
        self,
        fill: Fill,
        aggressor: Order,
        passive: Order,
        level: int,
    ) -> Trade:
        buyer = aggressor if aggressor.side == Side.BUY else passive
        seller = passive if passive.side == Side.SELL else aggressor
        trade = Trade(
            id=fill.trade_id,
            symbol=fill.symbol,
            price_ticks=fill.price_ticks,
            qty_lots=fill.qty_lots,
            notional_tick_lots=fill.price_ticks * fill.qty_lots,
            buyer_order_id=buyer.id,
            seller_order_id=seller.id,
            timestamp=fill.timestamp,
            level=level,
            buyer_session_alias=buyer.session_alias,
            seller_session_alias=seller.session_alias,
            buyer_participant_id=buyer.participant_id,
            seller_participant_id=seller.participant_id,
            aggressor_order_id=fill.aggressor_order_id,
            passive_order_id=fill.passive_order_id,
            aggressor_side=fill.aggressor_side,
            exchange_trade_id=fill.exchange_trade_id,
            run_generation=fill.run_generation,
        )
        return trade

    def _create_execution_report(
        self,
        order: Order,
        *,
        exec_type: ExecutionType,
        last_price_ticks: Optional[int] = None,
        last_qty_lots: Optional[int] = None,
        trade_id: Optional[int] = None,
        exchange_trade_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
        orig_cl_ord_id: Optional[str] = None,
        text: Optional[str] = None,
        cancel_ingress_sequence: Optional[int] = None,
        replace_ingress_sequence: Optional[int] = None,
    ) -> ExecutionReport:
        start_ns = self.monotonic_clock.now_ns()
        report = create_execution_report(
            order,
            exec_type=exec_type,
            timestamp=self.scheduler.current_time,
            exec_id=self.identifier_scope.next_exec_id(),
            last_price_ticks=last_price_ticks,
            last_qty_lots=last_qty_lots,
            trade_id=trade_id,
            exchange_trade_id=exchange_trade_id,
            cl_ord_id=cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id,
            text=text,
            cancel_ingress_sequence=cancel_ingress_sequence,
            replace_ingress_sequence=replace_ingress_sequence,
        )
        self._record_cpu_duration(
            REPORT_CREATION_DURATION,
            order.id,
            order.session_id,
            start_ns,
            context=f"execution report {report.exec_id} creation",
            report_exec_id=report.exec_id,
        )
        return report

    def _send_execution_report(self, report: ExecutionReport) -> None:
        self._notify_observer(
            "execution report",
            self.on_execution_report,
            report,
        )

    @staticmethod
    def _snapshot_levels(
        book: OrderBook,
        side: Side,
    ) -> tuple[PriceLevelSnapshot, ...]:
        levels = book.bids if side == Side.BUY else book.asks
        return tuple(
            PriceLevelSnapshot(
                price_ticks=level.price_ticks,
                level_qty_lots=level.level_qty_lots,
                strategy_qty_lots=level.strategy_qty_lots,
                market_qty_lots=level.market_qty_lots,
                order_count=len(level.orders),
            )
            for level in levels
            if level.level_qty_lots > 0 and level.orders
        )

    def _create_market_data_snapshot(
        self,
        symbol: str,
        *,
        sequence: int,
        fills: tuple[Fill, ...] = (),
    ) -> MarketDataSnapshot:
        instrument = self.instruments.require(symbol)
        book = self.books[symbol]
        return MarketDataSnapshot(
            symbol=symbol,
            instrument=instrument,
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
                    price_ticks=fill.price_ticks,
                    qty_lots=fill.qty_lots,
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
        if symbol not in self.books:
            return
        sequence = self.market_data_sequence + 1
        snapshot = self._create_market_data_snapshot(
            symbol,
            sequence=sequence,
            fills=tuple(fills),
        )
        self.market_data_sequence = sequence
        self._notify_observer(
            "market data",
            self.on_market_data_update,
            snapshot,
        )

    def get_market_data_snapshot(
        self,
        symbol: str,
    ) -> Optional[MarketDataSnapshot]:
        if symbol not in self.books:
            return None
        return self._create_market_data_snapshot(
            symbol,
            sequence=self.market_data_sequence,
        )

    def get_order_book_snapshot(self, symbol: str) -> Optional[OrderBook]:
        return self.books.get(symbol)

    def cancel_order(
        self,
        command: CancelOrderCommand,
    ) -> ExecutionReport | OrderCancelReject:
        """Measure and resolve one non-noop cancel command."""
        if not isinstance(command, CancelOrderCommand):
            raise TypeError("cancel_order requires CancelOrderCommand")
        self.scheduler.ensure_running()
        key = ClientOrderKey(
            session_id=command.session_id,
            cl_ord_id=command.orig_cl_ord_id,
        )
        order_id = self.client_order_index.get(key)
        order = self.orders.get(order_id) if order_id is not None else None
        start_ns = self.monotonic_clock.now_ns()
        try:
            return self._cancel_order_impl(command)
        finally:
            self._record_cpu_duration(
                ENGINE_COMMAND_DURATION,
                order.id if order is not None else None,
                command.session_id,
                start_ns,
                context=(
                    "cancel command "
                    f"{command.run_generation}:{command.ingress_sequence}"
                ),
            )

    def _cancel_order_impl(
        self,
        command: CancelOrderCommand,
    ) -> ExecutionReport | OrderCancelReject:
        """Resolve one cancel against authoritative execution-time state."""
        if not isinstance(command, CancelOrderCommand):
            raise TypeError("cancel_order requires CancelOrderCommand")
        self.scheduler.ensure_running()
        self._notify_observer("cancel request", self.on_cancel_request, command)

        if command.run_generation != self.identifier_scope.generation:
            return self._cancel_reject(
                command,
                reason=CancelRejectReason.INVALID_REQUEST,
                text="Stale cancel run generation",
                outcome=CancellationOutcome.STALE_RUN_GENERATION,
            )
        if command.duplicate_request:
            return self._cancel_reject(
                command,
                reason=CancelRejectReason.INVALID_REQUEST,
                text="Duplicate cancel request ClOrdID in session",
                outcome=CancellationOutcome.DUPLICATE_REQUEST,
            )

        key = ClientOrderKey(
            session_id=command.session_id,
            cl_ord_id=command.orig_cl_ord_id,
        )
        order_id = self.client_order_index.get(key)
        order = self.orders.get(order_id) if order_id is not None else None
        if order is None:
            foreign = next(
                (
                    candidate
                    for candidate in sorted(
                        self.orders.values(),
                        key=lambda item: item.id,
                    )
                    if (
                        candidate.session_id is not None
                        and self.client_order_index.get(ClientOrderKey(
                            candidate.session_id, command.orig_cl_ord_id,
                        )) == candidate.id
                        and candidate.session_id != command.session_id
                    )
                ),
                None,
            )
            if foreign is not None:
                return self._cancel_reject(
                    command,
                    order=foreign,
                    reason=CancelRejectReason.SESSION_MISMATCH,
                    text="OrigClOrdID belongs to a different session",
                    outcome=CancellationOutcome.WRONG_OWNER,
                )
            return self._cancel_reject(
                command,
                reason=CancelRejectReason.UNKNOWN_ORDER,
                text=f"Order not found: {command.orig_cl_ord_id}",
                outcome=CancellationOutcome.UNKNOWN_ORDER,
            )
        if order.cl_ord_id != command.orig_cl_ord_id:
            return self._cancel_reject(
                command,
                order=order,
                reason=CancelRejectReason.STALE_ORDER_REFERENCE,
                text="Stale OrigClOrdID; reference the latest accepted ClOrdID",
                outcome=CancellationOutcome.UNKNOWN_ORDER,
            )
        if order.status == OrderStatus.CANCELLED:
            return self._cancel_reject(
                command,
                order=order,
                reason=CancelRejectReason.ALREADY_CANCELED,
                text="Order is already canceled",
                outcome=CancellationOutcome.ALREADY_TERMINAL,
            )
        if order.status in {OrderStatus.FILLED, OrderStatus.REJECTED}:
            return self._cancel_reject(
                command,
                order=order,
                reason=CancelRejectReason.TOO_LATE_TO_CANCEL,
                text=f"Order already {order.status.value}",
                outcome=CancellationOutcome.ALREADY_TERMINAL,
            )

        book = self.books[order.symbol]
        pending = order.status == OrderStatus.PENDING_NEW and not order.processed
        resting = book.is_resting(order.id)
        if (not pending and not resting) or order.leaves_qty_lots <= 0:
            return self._cancel_reject(
                command,
                order=order,
                reason=CancelRejectReason.TOO_LATE_TO_CANCEL,
                text="Order has no live cancellable remainder",
                outcome=CancellationOutcome.ALREADY_TERMINAL,
            )

        order.validate_transition_to(OrderStatus.CANCELLED)
        commit_start_ns = self.monotonic_clock.now_ns()
        try:
            if resting:
                if not book.remove_order(order):
                    raise RuntimeError(f"Resting order {order.id} could not be removed")
                book.assert_invariants()
            order.version += 1
            order.cancel_remaining()
            order.transition_to(OrderStatus.CANCELLED)
        finally:
            self._record_cpu_duration(
                BOOK_COMMIT_DURATION,
                order.id,
                order.session_id,
                commit_start_ns,
                context=f"cancel commit for order {order.id}",
            )
        report = self._create_execution_report(
            order,
            exec_type=ExecutionType.CANCELED,
            cl_ord_id=command.cancel_cl_ord_id,
            orig_cl_ord_id=order.cl_ord_id,
            text="Order cancelled by request",
            cancel_ingress_sequence=command.ingress_sequence,
        )
        self._send_execution_report(report)
        self._notify_observer(
            "cancel outcome",
            self.on_cancel_outcome,
            command,
            CancellationOutcome.ACCEPTED,
        )
        if resting:
            self._notify_observer(
                "displayed order event",
                self.on_displayed_order_event,
                DisplayedOrderDeleted(
                    run_generation=order.run_generation,
                    order_id=order.id,
                    order_ref_num=self._displayed_reference(order),
                    symbol=order.symbol,
                    timestamp=self.scheduler.current_time,
                ),
            )
            order.displayed_order_ref = None
            self._send_market_data_update(order.symbol)
        return report

    def replace_order(
        self, command: ReplaceOrderCommand,
    ) -> ExecutionReport | OrderCancelReject:
        """Resolve one replacement against current authoritative order state."""
        if not isinstance(command, ReplaceOrderCommand):
            raise TypeError("replace_order requires ReplaceOrderCommand")
        self.scheduler.ensure_running()
        order_id = self.client_order_index.get(
            ClientOrderKey(command.session_id, command.orig_cl_ord_id)
        )
        start_ns = self.monotonic_clock.now_ns()
        try:
            return self._replace_order_impl(command)
        finally:
            self._record_cpu_duration(
                ENGINE_COMMAND_DURATION, order_id, command.session_id, start_ns,
                context=f"replace command {command.run_generation}:{command.ingress_sequence}",
            )

    def _replace_order_impl(
        self, command: ReplaceOrderCommand,
    ) -> ExecutionReport | OrderCancelReject:
        self._notify_observer("replace request", self.on_replace_request, command)
        # Even early request rejections must snapshot the authoritative target.
        # Only the requesting session's index is consulted before ownership
        # validation, preserving rejection precedence without exposing a foreign
        # order through a duplicate or generation error.
        key = ClientOrderKey(command.session_id, command.orig_cl_ord_id)
        order_id = self.client_order_index.get(key)
        order = self.orders.get(order_id) if order_id is not None else None
        if command.run_generation != self.identifier_scope.generation:
            return self._replace_reject(
                command, order=order, reason=CancelRejectReason.INVALID_REQUEST,
                text="Stale replace run generation",
                outcome=ReplacementOutcome.STALE_RUN_GENERATION,
            )
        request_key = ClientOrderKey(command.session_id, command.replace_cl_ord_id)
        duplicate = command.duplicate_request or request_key in self._claimed_client_order_ids
        self._claimed_client_order_ids.add(request_key)
        if duplicate:
            return self._replace_reject(
                command, order=order, reason=CancelRejectReason.DUPLICATE_CL_ORD_ID,
                text="Duplicate replace request ClOrdID in session",
                outcome=ReplacementOutcome.DUPLICATE_REQUEST,
            )
        if order is None:
            foreign_id = next((
                indexed_order_id
                for indexed_key, indexed_order_id in self.client_order_index.items()
                if indexed_key.cl_ord_id == command.orig_cl_ord_id
                and indexed_key.session_id != command.session_id
            ), None)
            foreign = self.orders.get(foreign_id) if foreign_id is not None else None
            if foreign is not None:
                return self._replace_reject(
                    command, order=foreign, reason=CancelRejectReason.SESSION_MISMATCH,
                    text="OrigClOrdID belongs to a different session",
                    outcome=ReplacementOutcome.WRONG_OWNER,
                )
            return self._replace_reject(
                command, reason=CancelRejectReason.UNKNOWN_ORDER,
                text=f"Order not found: {command.orig_cl_ord_id}",
                outcome=ReplacementOutcome.UNKNOWN_ORDER,
            )
        if order.cl_ord_id != command.orig_cl_ord_id:
            return self._replace_reject(
                command, order=order, reason=CancelRejectReason.STALE_ORDER_REFERENCE,
                text="Stale OrigClOrdID; reference the latest accepted ClOrdID",
                outcome=ReplacementOutcome.STALE_ORDER_REFERENCE,
            )
        if command.participant_id != order.participant_id:
            return self._replace_reject(
                command, order=order, reason=CancelRejectReason.PARTICIPANT_MISMATCH,
                text="Replacement participant does not own the order",
                outcome=ReplacementOutcome.WRONG_OWNER,
            )
        book = self.books[order.symbol]
        if (order.status not in {OrderStatus.NEW, OrderStatus.PARTIAL}
                or order.order_type != OrderType.LIMIT or order.tif != TimeInForce.GTC
                or not book.is_resting(order.id) or order.leaves_qty_lots <= 0):
            return self._replace_reject(
                command, order=order, reason=CancelRejectReason.TOO_LATE_TO_REPLACE,
                text="Order has no live replaceable GTC limit remainder",
                outcome=ReplacementOutcome.ALREADY_TERMINAL,
            )
        if (command.symbol != order.symbol or command.side != order.side
                or command.order_type != order.order_type or command.tif != order.tif):
            return self._replace_reject(
                command, order=order, reason=CancelRejectReason.UNSUPPORTED_CHANGE,
                text="Replacement cannot change symbol, side, order type, or TimeInForce",
                outcome=ReplacementOutcome.INVALID_REQUEST,
            )
        if (not isinstance(command.order_qty_lots, int)
                or isinstance(command.order_qty_lots, bool) or command.order_qty_lots <= 0
                or not isinstance(command.price_ticks, int)
                or isinstance(command.price_ticks, bool) or command.price_ticks <= 0):
            return self._replace_reject(
                command, order=order, reason=CancelRejectReason.INVALID_REQUEST,
                text="Replacement requires positive integer quantity lots and limit price ticks",
                outcome=ReplacementOutcome.INVALID_REQUEST,
            )

        book.assert_invariants()
        old_cl_ord_id = order.cl_ord_id
        old_quantity = order.order_qty_lots
        old_price = order.price_ticks
        old_reference = self._displayed_reference(order)
        old_leaves = order.leaves_qty_lots
        new_quantity = max(command.order_qty_lots, order.filled_qty_lots)
        retains_priority = command.price_ticks == old_price and new_quantity <= old_quantity
        changed = new_quantity != old_quantity or command.price_ticks != old_price
        displayed_events: List[DisplayedOrderEvent] = []
        self._displayed_event_buffer = displayed_events
        try:
            commit_start_ns = self.monotonic_clock.now_ns()
            try:
                if retains_priority:
                    book.resize_order(order, new_quantity)
                    reduced = old_leaves - order.leaves_qty_lots
                    if reduced:
                        self._queue_displayed_event(DisplayedOrderReduced(
                            run_generation=order.run_generation, order_id=order.id,
                            order_ref_num=old_reference, symbol=order.symbol,
                            canceled_qty_lots=reduced, timestamp=self.scheduler.current_time,
                        ))
                    if order.leaves_qty_lots == 0:
                        order.displayed_order_ref = None
                else:
                    if not book.remove_order(order):
                        raise RuntimeError(f"Resting order {order.id} could not be removed")
                    self._queue_displayed_event(DisplayedOrderDeleted(
                        run_generation=order.run_generation, order_id=order.id,
                        order_ref_num=old_reference, symbol=order.symbol,
                        timestamp=self.scheduler.current_time,
                    ))
                    order.displayed_order_ref = None
                    order.order_qty_lots = new_quantity
                order.price_ticks = command.price_ticks
                order.cl_ord_id = command.replace_cl_ord_id
                order.version += 1
                if order.leaves_qty_lots == 0:
                    order.transition_to(OrderStatus.FILLED)
                self.client_order_index[request_key] = order.id
            finally:
                self._record_cpu_duration(
                    BOOK_COMMIT_DURATION, order.id, order.session_id, commit_start_ns,
                    context=f"replace commit for order {order.id}",
                )
            report = self._create_execution_report(
                order, exec_type=ExecutionType.REPLACED,
                orig_cl_ord_id=old_cl_ord_id,
                replace_ingress_sequence=command.ingress_sequence,
                text="Order replaced by request",
            )
            self._send_execution_report(report)
            self._notify_observer(
                "replace outcome", self.on_replace_outcome, command, ReplacementOutcome.ACCEPTED,
            )
            fills: List[Fill] = []
            if not retains_priority and order.leaves_qty_lots:
                fills = self._run_matching(order, book)
                self._finalize_unfilled_remainder(order, book)
            book.assert_invariants()
            self._publish_displayed_events(displayed_events)
            if changed:
                self._send_market_data_update(order.symbol, fills)
            return report
        finally:
            self._displayed_event_buffer = None

    def reject_replace_unavailable(
        self, command: ReplaceOrderCommand, reason: str,
    ) -> OrderCancelReject:
        """Settle queued replacement during lifecycle failure without matching."""
        if not isinstance(command, ReplaceOrderCommand):
            raise TypeError("reject_replace_unavailable requires ReplaceOrderCommand")
        self._notify_observer("replace request", self.on_replace_request, command)
        self._claimed_client_order_ids.add(ClientOrderKey(command.session_id, command.replace_cl_ord_id))
        order_id = self.client_order_index.get(ClientOrderKey(command.session_id, command.orig_cl_ord_id))
        return self._replace_reject(
            command, order=self.orders.get(order_id) if order_id is not None else None,
            reason=CancelRejectReason.EXCHANGE_UNAVAILABLE, text=reason,
            outcome=ReplacementOutcome.EXCHANGE_UNAVAILABLE,
        )

    def _replace_reject(
        self, command: ReplaceOrderCommand, *, reason: CancelRejectReason,
        text: str, outcome: ReplacementOutcome, order: Optional[Order] = None,
    ) -> OrderCancelReject:
        start_ns = self.monotonic_clock.now_ns()
        reject = create_order_cancel_reject(
            command, reject_id=self.identifier_scope.next_cancel_reject_id(),
            reason=reason, text=text, timestamp=self.scheduler.current_time, order=order,
        )
        self._record_cpu_duration(
            REPORT_CREATION_DURATION, order.id if order else None, command.session_id,
            start_ns, context=f"replace reject {reject.reject_id} creation",
            report_exec_id=reject.reject_id,
        )
        self._notify_observer("replace reject", self.on_replace_reject, reject)
        self._notify_observer("replace outcome", self.on_replace_outcome, command, outcome)
        return reject

    @staticmethod
    def _displayed_reference(order: Order) -> int:
        reference = order.displayed_order_ref
        if reference is None or not 0 < reference < 2**64:
            raise RuntimeError(f"Resting order {order.id} has no displayed reference")
        return reference

    def _queue_displayed_event(self, event: DisplayedOrderEvent) -> None:
        if self._displayed_event_buffer is None:
            self._notify_observer(
                "displayed order event",
                self.on_displayed_order_event,
                event,
            )
            return
        self._displayed_event_buffer.append(event)

    def _publish_displayed_events(
        self,
        events: List[DisplayedOrderEvent],
    ) -> None:
        for event in events:
            self._notify_observer(
                "displayed order event",
                self.on_displayed_order_event,
                event,
            )

    def _cancel_reject(
        self,
        command: CancelOrderCommand,
        *,
        reason: CancelRejectReason,
        text: str,
        order: Optional[Order] = None,
        outcome: CancellationOutcome,
    ) -> OrderCancelReject:
        start_ns = self.monotonic_clock.now_ns()
        reject = create_order_cancel_reject(
            command,
            reject_id=self.identifier_scope.next_cancel_reject_id(),
            reason=reason,
            text=text,
            timestamp=self.scheduler.current_time,
            order=order,
        )
        self._record_cpu_duration(
            REPORT_CREATION_DURATION,
            order.id if order is not None else None,
            command.session_id,
            start_ns,
            context=f"cancel reject {reject.reject_id} creation",
            report_exec_id=reject.reject_id,
        )
        self._notify_observer("cancel reject", self.on_cancel_reject, reject)
        self._notify_observer(
            "cancel outcome",
            self.on_cancel_outcome,
            command,
            outcome,
        )
        return reject

    def active_order_count(self) -> int:
        return sum(
            order.status not in TERMINAL_ORDER_STATUSES
            for order in self.orders.values()
        )

    def resting_order_count(self) -> int:
        return sum(
            len(level.orders)
            for book in self.books.values()
            for level in (*book.bids, *book.asks)
        )

    def order_book_level_count(self) -> int:
        return sum(len(book.bids) + len(book.asks) for book in self.books.values())

    def _record_cpu_duration(
        self,
        metric: str,
        order_id: Optional[int],
        session_id: Optional[str],
        start_ns: int,
        *,
        context: str,
        report_exec_id: Optional[str] = None,
    ) -> None:
        end_ns = self.monotonic_clock.now_ns()
        try:
            duration_ns = monotonic_duration_ns(
                start_ns,
                end_ns,
                context=context,
            )
        except ValueError:
            logger.exception(
                "Invalid CPU duration timestamps",
                extra={
                    "metric": metric,
                    "order_id": order_id,
                    "session_id": session_id,
                },
            )
            return
        self._notify_observer(
            metric,
            self.on_cpu_duration,
            metric,
            order_id,
            session_id,
            duration_ns,
            self.scheduler.current_time,
            report_exec_id,
        )

    @staticmethod
    def _notify_observer(name: str, callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("%s observer failed", name)

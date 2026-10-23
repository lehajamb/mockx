"""External source-to-FIX bookkeeping; the exchange remains the only matcher."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
import threading
from typing import Any

from examples.fix_support import FixReport
from src.mockx.instruments import Instrument


TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED"})


class PendingCommandError(RuntimeError):
    """An order's previous amendment has not received its own acknowledgement."""


@dataclass(frozen=True, slots=True)
class FixIntent:
    kind: str
    role: str
    cl_ord_id: str
    orig_cl_ord_id: str | None
    symbol: str
    side: str
    quantity_lots: int
    price_ticks: int | None
    tif: str
    source_id: str | None
    event_index: int | None


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    intent: FixIntent
    accepted: bool
    status: str
    text: str | None
    report: FixReport


@dataclass(frozen=True, slots=True)
class LedgerOrder:
    source_id: str
    role: str
    cl_ord_id: str
    side: str
    price_ticks: int
    order_qty_lots: int
    cum_qty_lots: int
    leaves_qty_lots: int
    status: str
    source_remaining_lots: int = 0
    historical_matched_lots: int = 0
    order_id: str | None = None
    pending_cl_ord_id: str | None = None
    last_report: FixReport | None = None


def _side(value: Any) -> str:
    normalized = getattr(value, "value", value)
    if normalized not in {"BUY", "SELL"}:
        raise ValueError(f"Invalid replay side: {value!r}")
    return normalized


def _quantity(value: int, name: str, *, zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise ValueError(f"{name} must be a {'nonnegative' if zero else 'positive'} integer")
    return value


class ReplayLedger:
    """Track accepted FIX identities and observed fills without rebuilding a book.

    Source events are already validated by the file reader. Their source leaves
    plus cumulative historical matches define a desired lifetime quantity. The
    actual FIX cumulative fills are never undone by a later source amendment.
    Snapshots returned to the driver are immutable and safe across FIX callbacks.
    """

    def __init__(self, symbol: str, instrument: Instrument, id_prefix: str = "REPLAY"):
        if not symbol or instrument.symbol != symbol or not id_prefix:
            raise ValueError("A matching instrument, symbol and ID prefix are required")
        self.symbol = symbol
        self.instrument = instrument
        self.id_prefix = id_prefix
        self._lock = threading.RLock()
        self._counter = 0
        self._orders: dict[str, LedgerOrder] = {}
        self._order_aliases: dict[str, str] = {}
        self._commands: dict[str, FixIntent] = {}
        self._outcomes: dict[str, CommandOutcome] = {}
        self._seen_reports: set[tuple[str, str]] = set()
        self._seen_events: set[int] = set()

    def _new_id(self) -> str:
        self._counter += 1
        return f"{self.id_prefix}-{self._counter:012d}"

    def register_seed(self, source_order: Any, cl_ord_id: str,
                      order_id: str | None = None) -> None:
        """Register an acknowledged administrative seed owned by a replay session."""
        with self._lock:
            source_id = source_order.source_id
            if not source_id or not cl_ord_id:
                raise ValueError("Seed source ID and ClOrdID are required")
            if source_id in self._orders or cl_ord_id in self._order_aliases:
                raise ValueError("Duplicate seed source ID or ClOrdID")
            side = _side(source_order.side)
            quantity = _quantity(source_order.quantity_lots, "seed quantity")
            price = _quantity(source_order.price_ticks, "seed price")
            self._orders[source_id] = LedgerOrder(
                source_id, f"replay_{side.lower()}", cl_ord_id, side, price,
                quantity, 0, quantity, "NEW", source_remaining_lots=quantity,
                order_id=order_id,
            )
            self._order_aliases[cl_ord_id] = source_id

    def translate(self, event: Any) -> FixIntent | None:
        """Make one ordinary FIX request, or skip a no-op/terminal lineage.

        The caller must wait for each request's outcome before translating a
        later amendment to the same source order. Retrying an already translated
        source event returns None, so a MATCH can never be sent twice.
        """
        with self._lock:
            if event.index in self._seen_events:
                return None
            operation = getattr(event.operation, "value", event.operation)
            side = _side(event.side)
            source_id = event.source_id
            if operation == "MATCH":
                side = "SELL" if side == "BUY" else "BUY"
                intent = FixIntent(
                    "NEW", f"replay_{side.lower()}", self._new_id(), None,
                    self.symbol, side, _quantity(event.quantity_lots, "match quantity"),
                    _quantity(event.price_ticks, "match price"), "IOC", source_id,
                    event.index,
                )
                if source_id in self._orders:
                    self._orders[source_id] = self._source_quantities(
                        self._orders[source_id], event,
                    )
            elif operation == "ADD":
                if source_id in self._orders:
                    raise ValueError(f"Source order ID was reused: {source_id}")
                quantity = _quantity(event.quantity_lots, "add quantity")
                price = _quantity(event.price_ticks, "add price")
                clid = self._new_id()
                intent = FixIntent(
                    "NEW", f"replay_{side.lower()}", clid, None, self.symbol,
                    side, quantity, price, "GTC", source_id, event.index,
                )
                self._orders[source_id] = LedgerOrder(
                    source_id, intent.role, clid, side, price, quantity, 0, 0,
                    "PENDING_NEW", source_remaining_lots=quantity,
                    pending_cl_ord_id=clid,
                )
                self._order_aliases[clid] = source_id
            elif operation in {"SET", "SUB", "DELETE"}:
                order = self._orders.get(source_id)
                if order is None:
                    raise ValueError(f"Unknown source order: {source_id}")
                if order.pending_cl_ord_id is not None:
                    raise PendingCommandError(
                        f"Await {order.pending_cl_ord_id} before changing {source_id}"
                    )
                order = self._source_quantities(order, event)
                self._orders[source_id] = order
                if order.status in TERMINAL:
                    self._seen_events.add(event.index)
                    return None
                remaining = order.source_remaining_lots
                historical_fills = order.historical_matched_lots
                total = max(order.cum_qty_lots, remaining + historical_fills)
                price = _quantity(event.price_ticks, "amendment price")
                cancel = (operation == "DELETE" or remaining == 0
                          or total == order.cum_qty_lots)
                if (not cancel and total == order.order_qty_lots
                        and price == order.price_ticks):
                    self._seen_events.add(event.index)
                    return None
                intent = FixIntent(
                    "CANCEL" if cancel else "REPLACE", order.role, self._new_id(),
                    order.cl_ord_id, self.symbol, order.side,
                    order.order_qty_lots if cancel else total,
                    order.price_ticks if cancel else price, "GTC", source_id,
                    event.index,
                )
                self._orders[source_id] = replace(order, pending_cl_ord_id=intent.cl_ord_id)
            else:
                raise ValueError(f"Unsupported source operation: {operation}")
            self._commands[intent.cl_ord_id] = intent
            self._seen_events.add(event.index)
            return intent

    @staticmethod
    def _source_quantities(order: LedgerOrder, event: Any) -> LedgerOrder:
        return replace(
            order,
            source_remaining_lots=_quantity(
                event.source_remaining_lots, "source remaining", zero=True,
            ),
            historical_matched_lots=_quantity(
                event.historical_matched_lots, "historical matches", zero=True,
            ),
        )

    def _lots(self, value: str | None, fallback: int) -> int:
        if value is None:
            return fallback
        parsed = Decimal(value)
        if parsed == 0:
            return 0
        return self.instrument.quantity_to_lots(value)

    def record_report(self, report: FixReport) -> bool:
        """Apply one report; only the matching request can settle an amendment."""
        with self._lock:
            command = self._commands.get(report.cl_ord_id)
            source_id = self._order_aliases.get(report.cl_ord_id)
            if source_id is None and command is not None and command.tif != "IOC":
                source_id = command.source_id
            if command is None and source_id is None:
                return False
            exec_id = report.exec_id
            key = (report.sender_comp_id, exec_id) if exec_id else None
            if key is not None and key in self._seen_reports:
                return False
            if key is not None:
                self._seen_reports.add(key)

            rejected = (report.message_type == "OrderCancelReject"
                        or report.status == "REJECTED" or report.exec_type == "8")
            completes = False
            if command is not None and command.cl_ord_id not in self._outcomes:
                if rejected:
                    completes = True
                elif report.message_type == "ExecutionReport":
                    if command.kind == "REPLACE":
                        completes = report.exec_type == "5"
                    elif command.kind == "CANCEL":
                        completes = report.exec_type == "4"
                    elif command.tif == "IOC":
                        completes = report.status in TERMINAL
                    else:
                        completes = report.status in {"NEW", "PARTIAL", *TERMINAL}
            if completes:
                self._outcomes[command.cl_ord_id] = CommandOutcome(
                    command, not rejected, report.status, report.text, report,
                )

            if source_id is not None:
                order = self._orders[source_id]
                if report.message_type == "ExecutionReport":
                    accepted_replace = (command is not None and command.kind == "REPLACE"
                                        and report.exec_type == "5" and not rejected
                                        and command.cl_ord_id in {
                                            order.cl_ord_id, order.pending_cl_ord_id,
                                        })
                    current_reference = report.cl_ord_id == order.cl_ord_id
                    cum = self._lots(report.cum_qty, order.cum_qty_lots)
                    # A delayed old fill must not undo a newer accepted total or
                    # revive a canceled order. CumQty is monotonically observed.
                    if cum >= order.cum_qty_lots:
                        total = order.order_qty_lots
                        price = order.price_ticks
                        clid = order.cl_ord_id
                        if accepted_replace:
                            total = self._lots(report.quantity, command.quantity_lots)
                            price = command.price_ticks
                            clid = command.cl_ord_id
                            self._order_aliases[clid] = source_id
                        elif current_reference and report.quantity is not None:
                            total = self._lots(report.quantity, total)
                        leaves = max(0, total - cum)
                        if current_reference or accepted_replace or (
                            command is not None and command.kind == "CANCEL"
                            and report.exec_type == "4"
                        ):
                            leaves = self._lots(report.leaves_qty, leaves)
                        status = report.status
                        if order.status in TERMINAL and not accepted_replace:
                            status, leaves = order.status, 0
                        elif not current_reference and not accepted_replace and (
                            command is None or command.kind != "CANCEL"
                        ):
                            status = "FILLED" if leaves == 0 else ("PARTIAL" if cum else "NEW")
                        order = replace(
                            order, cl_ord_id=clid, price_ticks=price,
                            order_qty_lots=total, cum_qty_lots=cum,
                            leaves_qty_lots=leaves, status=status,
                            order_id=report.order_id or order.order_id,
                            last_report=report,
                        )
                if completes and order.pending_cl_ord_id == report.cl_ord_id:
                    order = replace(order, pending_cl_ord_id=None)
                self._orders[source_id] = order
            return True

    def is_complete(self, cl_ord_id: str) -> bool:
        with self._lock:
            return cl_ord_id in self._outcomes

    def outcome(self, cl_ord_id: str) -> CommandOutcome | None:
        with self._lock:
            return self._outcomes.get(cl_ord_id)

    def orders_snapshot(self) -> dict[str, LedgerOrder]:
        with self._lock:
            return dict(self._orders)

    def live_orders(self) -> tuple[LedgerOrder, ...]:
        with self._lock:
            return tuple(order for order in self._orders.values()
                         if order.leaves_qty_lots > 0 and order.status not in TERMINAL)


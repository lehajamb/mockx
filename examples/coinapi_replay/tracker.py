"""Minimal source-to-FIX ledger; actual execution remains owned by MockX."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from examples.fix_support import FixReport
from src.mockx.instruments import Instrument, parse_decimal


TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED"})


class PendingCommandError(RuntimeError):
    """An amendment requires the accepted identity of an outstanding replace."""


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


@dataclass(slots=True)
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
    source_remaining_lots: int
    historical_matched_lots: int = 0
    order_id: str | None = None
    pending_cl_ord_id: str | None = None
    pending_ids: set[str] = field(default_factory=set)
    source_index: int = 0
    deferred_indices: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    intent: FixIntent
    accepted: bool
    status: str
    text: str | None
    report: FixReport
    order: LedgerOrder | None = None
    also_completed: tuple[FixIntent, ...] = ()


def _side(value):
    side = getattr(value, "value", value)
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"Invalid side: {side}")
    return side


class ReplayLedger:
    """Track live lineages and outstanding requests, not the entire run history.

    Source ordinals are consumed once. The compact watermark retains only holes
    caused by unresolved per-order replacements; the runner bounds that backlog.
    Call methods on the runner event loop, including marshalled FIX callbacks.
    """

    def __init__(self, symbol: str, instrument: Instrument, id_prefix: str = "REPLAY"):
        if symbol != instrument.symbol or not id_prefix:
            raise ValueError("Matching instrument and nonempty ID prefix required")
        self.symbol, self.instrument, self.id_prefix = symbol, instrument, id_prefix
        self.orders: dict[str, LedgerOrder] = {}
        self.pending: dict[str, FixIntent] = {}
        self._aliases: dict[str, str] = {}
        self._ioc_cum: dict[str, int] = {}
        self._counter = 0
        self._seen_through: int | None = None
        self._seen_ahead: set[int] = set()
        self.filled_quantity_lots = 0
        self.filled_by_role: dict[str, int] = {}

    def _id(self):
        self._counter += 1
        return f"{self.id_prefix}-{self._counter:012d}"

    def register_seed(self, order, cl_ord_id, order_id=None):
        if order.source_id in self.orders or cl_ord_id in self._aliases:
            raise ValueError("Duplicate seed identity")
        side = _side(order.side)
        self.orders[order.source_id] = LedgerOrder(
            order.source_id,
            f"replay_{side.lower()}",
            cl_ord_id,
            side,
            order.price_ticks,
            order.quantity_lots,
            0,
            order.quantity_lots,
            "NEW",
            order.quantity_lots,
            order_id=order_id,
        )
        self._aliases[cl_ord_id] = order.source_id

    def pending_for(self, source_id):
        order = self.orders.get(source_id)
        return order.pending_cl_ord_id if order else None

    def _seen(self, index):
        return self._seen_through is not None and (
            index <= self._seen_through or index in self._seen_ahead
        )

    def _mark(self, index):
        if self._seen_through is None:
            self._seen_through = index - 1
        self._seen_ahead.add(index)
        while self._seen_through + 1 in self._seen_ahead:
            self._seen_through += 1
            self._seen_ahead.remove(self._seen_through)

    def translate(self, event) -> FixIntent | None:
        if self._seen(event.index):
            return None
        operation, side = event.operation, _side(event.side)
        order = self.orders.get(event.source_id)
        if operation in {"SET", "SUB", "DELETE"} and order and order.pending_cl_ord_id:
            order.deferred_indices.add(event.index)
            raise PendingCommandError(f"Await replacement {order.pending_cl_ord_id}")
        if order:
            order.deferred_indices.discard(event.index)
            if event.index > order.source_index:
                order.source_remaining_lots = event.source_remaining_lots
                order.historical_matched_lots = event.historical_matched_lots
                order.source_index = event.index
        intent = None
        if operation == "MATCH":
            opposite = "SELL" if side == "BUY" else "BUY"
            intent = FixIntent(
                "NEW",
                f"replay_{opposite.lower()}",
                self._id(),
                None,
                self.symbol,
                opposite,
                event.quantity_lots,
                event.price_ticks,
                "IOC",
                event.source_id,
                event.index,
            )
            self._ioc_cum[intent.cl_ord_id] = 0
        elif operation == "ADD":
            if order:
                raise ValueError("Duplicate source lifetime")
            clid = self._id()
            order = LedgerOrder(
                event.source_id,
                f"replay_{side.lower()}",
                clid,
                side,
                event.price_ticks,
                event.quantity_lots,
                0,
                0,
                "PENDING_NEW",
                event.source_remaining_lots,
                source_index=event.index,
            )
            self.orders[event.source_id] = order
            self._aliases[clid] = event.source_id
            intent = FixIntent(
                "NEW",
                order.role,
                clid,
                None,
                self.symbol,
                side,
                event.quantity_lots,
                event.price_ticks,
                "GTC",
                event.source_id,
                event.index,
            )
        elif operation in {"SUB", "SET", "DELETE"}:
            if order is None:
                raise ValueError(f"Unknown source lifetime: {event.source_id}")
            if order.status not in TERMINAL:
                total = max(
                    order.cum_qty_lots,
                    event.source_remaining_lots + event.historical_matched_lots,
                )
                cancel = (
                    operation == "DELETE"
                    or event.source_remaining_lots == 0
                    or total == order.cum_qty_lots
                )
                if cancel or total != order.order_qty_lots:
                    intent = FixIntent(
                        "CANCEL" if cancel else "REPLACE",
                        order.role,
                        self._id(),
                        order.cl_ord_id,
                        self.symbol,
                        side,
                        order.order_qty_lots if cancel else total,
                        order.price_ticks,
                        "GTC",
                        event.source_id,
                        event.index,
                    )
                    if not cancel:
                        order.pending_cl_ord_id = intent.cl_ord_id
        else:
            raise ValueError(f"Unsupported operation: {operation}")
        self._mark(event.index)
        if intent:
            self.pending[intent.cl_ord_id] = intent
            if intent.tif != "IOC":
                order.pending_ids.add(intent.cl_ord_id)
                self._aliases[intent.cl_ord_id] = event.source_id
        if order:
            self._retire(order)
        return intent

    def _lots(self, value, default):
        if value is None:
            return default
        decimal = parse_decimal(value, field_name="FIX quantity")
        return 0 if decimal == 0 else self.instrument.quantity_to_lots(decimal)

    def _count_fill(self, role, delta):
        if delta > 0:
            self.filled_quantity_lots += delta
            self.filled_by_role[role] = self.filled_by_role.get(role, 0) + delta

    def record_report(self, report: FixReport) -> CommandOutcome | None:
        intent = self.pending.get(report.cl_ord_id)
        source_id = self._aliases.get(report.cl_ord_id)
        order = self.orders.get(source_id) if source_id else None
        if intent is None and order is None:
            return None  # retired unique IDs cannot affect a later lifetime
        rejected = (
            report.message_type == "OrderCancelReject" or report.status == "REJECTED"
        )
        if intent and intent.tif == "IOC":
            previous = self._ioc_cum[intent.cl_ord_id]
            cum = max(previous, self._lots(getattr(report, "cum_qty", None), previous))
            self._count_fill(intent.role, cum - previous)
            self._ioc_cum[intent.cl_ord_id] = cum
            if report.status not in TERMINAL:
                return None
            del self._ioc_cum[intent.cl_ord_id]
            del self.pending[intent.cl_ord_id]
            return CommandOutcome(
                intent, not rejected, report.status, report.text, report
            )
        if order is None:
            return None
        if report.message_type != "OrderCancelReject":
            cum = self._lots(getattr(report, "cum_qty", None), order.cum_qty_lots)
            if cum >= order.cum_qty_lots:
                self._count_fill(order.role, cum - order.cum_qty_lots)
                order.cum_qty_lots = cum
                if order.status not in TERMINAL:
                    order.order_qty_lots = self._lots(
                        getattr(report, "quantity", None), order.order_qty_lots
                    )
                    order.leaves_qty_lots = self._lots(
                        report.leaves_qty, order.leaves_qty_lots
                    )
                    order.status = report.status
                order.order_id = getattr(report, "order_id", None) or order.order_id
        elif report.status in {"FILLED", "CANCELLED"}:
            order.status, order.leaves_qty_lots = report.status, 0
        completed = intent is not None and (
            rejected
            or (intent.kind == "NEW" and report.exec_type in {"0", "F"})
            or (intent.kind == "REPLACE" and report.exec_type == "5")
            or (intent.kind == "CANCEL" and report.exec_type == "4")
        )
        outcome = None
        if completed:
            del self.pending[intent.cl_ord_id]
            order.pending_ids.discard(intent.cl_ord_id)
            if intent.kind == "REPLACE":
                order.pending_cl_ord_id = None
                if not rejected:
                    old = order.cl_ord_id
                    order.cl_ord_id = intent.cl_ord_id
                    self._aliases.pop(old, None)
            if intent.kind == "CANCEL" or (intent.kind == "REPLACE" and rejected):
                self._aliases.pop(intent.cl_ord_id, None)
            also_completed = []
            if not rejected and intent.kind in {"CANCEL", "REPLACE"}:
                # A pending NEW can be canceled/replaced before its processing
                # event. The engine then has no separate New ack to deliver.
                for clid in sorted(order.pending_ids):
                    previous = self.pending[clid]
                    if previous.kind == "NEW":
                        also_completed.append(self.pending.pop(clid))
                        order.pending_ids.remove(clid)
            outcome = CommandOutcome(
                intent,
                not rejected,
                report.status,
                report.text,
                report,
                self._snapshot(order),
                tuple(also_completed),
            )
        self._retire(order)
        return outcome

    def _retire(self, order):
        if (
            order.source_remaining_lots == 0
            and order.status in TERMINAL
            and not order.pending_ids
            and not order.deferred_indices
        ):
            self.orders.pop(order.source_id, None)
            self._aliases.pop(order.cl_ord_id, None)

    def live_orders(self):
        return tuple(
            self._snapshot(order)
            for order in self.orders.values()
            if order.status not in TERMINAL and order.leaves_qty_lots > 0
        )

    def orders_snapshot(self):
        return {key: self._snapshot(order) for key, order in self.orders.items()}

    @staticmethod
    def _snapshot(order):
        return replace(
            order,
            pending_ids=set(order.pending_ids),
            deferred_indices=set(order.deferred_indices),
        )

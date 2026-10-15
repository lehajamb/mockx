"""Protocol-neutral order-book reconstruction from the typed native journal."""

from __future__ import annotations

from bisect import bisect_left, insort
from dataclasses import dataclass, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class InstrumentProjection:
    tick_size: Decimal
    lot_size: Decimal
    price_display_precision: int
    quantity_display_precision: int


@dataclass(frozen=True, slots=True)
class NativeOrder:
    order_id: int
    logical_order_id: int
    symbol: str
    side: str
    price_ticks: int
    qty_lots: int
    liquidity_category: str


def _side(value: object) -> str:
    value = str(getattr(value, "value", value)).upper()
    if value in {"BUY", "B", "1"}:
        return "BUY"
    if value in {"SELL", "S", "2"}:
        return "SELL"
    raise ValueError(f"Unknown journal side {value}")


class NativeBook:
    """Order identity/FIFO and integer aggregates shared by historical clients."""

    def __init__(self) -> None:
        self._orders: dict[int, NativeOrder] = {}
        self._levels: dict[tuple[str, str, int], int] = {}
        self._prices: dict[tuple[str, str], list[int]] = {}

    @property
    def orders(self):
        return MappingProxyType(self._orders)

    def depth(self, symbol: str, limit: int | None = None):
        def levels(side: str, reverse: bool):
            prices = self._prices.get((symbol, side), ())
            chosen = list(reversed(prices)) if reverse else prices
            if limit is not None:
                chosen = chosen[:limit]
            return tuple(
                (price, self._levels[(symbol, side, price)]) for price in chosen
            )

        return levels("BUY", True), levels("SELL", False)

    def apply_changes(self, rows: Iterable[Mapping[str, object]]) -> None:
        """Validate a whole command in a small overlay before changing the book."""
        changes: dict[int, NativeOrder | None] = {}
        deltas: dict[tuple[str, str, int], int] = {}
        touched_symbols: set[str] = set()
        for row in rows:
            kind = str(row["kind"])
            if not kind.startswith("BOOK_"):
                continue
            order_id = int(row["order_id"])
            old = (
                changes[order_id] if order_id in changes else self._orders.get(order_id)
            )
            if kind == "BOOK_ADDED":
                if old is not None:
                    raise ValueError(f"Duplicate displayed order {order_id}")
                new = NativeOrder(
                    order_id,
                    int(row.get("logical_order_id") or order_id),
                    str(row["symbol"]),
                    _side(row["side"]),
                    int(row["price_ticks"]),
                    int(row["qty_lots"]),
                    str(row.get("liquidity_category") or "market"),
                )
                if (
                    new.order_id <= 0
                    or new.logical_order_id <= 0
                    or new.price_ticks <= 0
                    or new.qty_lots <= 0
                ):
                    raise ValueError(
                        "Displayed add requires positive integer quantities"
                    )
            else:
                if old is None:
                    raise ValueError(f"Unknown displayed order {order_id}")
                if row.get("symbol") is not None and str(row["symbol"]) != old.symbol:
                    raise ValueError("Book change has the wrong symbol")
                if kind == "BOOK_DELETED":
                    new = None
                elif kind in {"BOOK_EXECUTED", "BOOK_REDUCED"}:
                    quantity = int(row["qty_lots"])
                    if not 0 < quantity <= old.qty_lots:
                        raise ValueError("Book change exceeds remaining quantity")
                    remaining = old.qty_lots - quantity
                    if (
                        row.get("remaining_qty_lots") is not None
                        and int(row["remaining_qty_lots"]) != remaining
                    ):
                        raise ValueError("Book change remaining quantity disagrees")
                    new = replace(old, qty_lots=remaining) if remaining else None
                else:
                    raise ValueError(f"Unsupported native book event {kind}")
            for order, sign in ((old, -1), (new, 1)):
                if order is not None:
                    key = (order.symbol, order.side, order.price_ticks)
                    deltas[key] = deltas.get(key, 0) + sign * order.qty_lots
                    touched_symbols.add(order.symbol)
            changes[order_id] = new
        for key, delta in deltas.items():
            if self._levels.get(key, 0) + delta < 0:
                raise ValueError("Negative reconstructed level quantity")

        def best(symbol: str, side: str):
            existing = self._prices.get((symbol, side), ())
            iterator = reversed(existing) if side == "BUY" else iter(existing)
            price = next(
                (
                    p
                    for p in iterator
                    if self._levels[(symbol, side, p)]
                    + deltas.get((symbol, side, p), 0)
                    > 0
                ),
                None,
            )
            for key, delta in deltas.items():
                if key[:2] == (symbol, side) and self._levels.get(key, 0) + delta > 0:
                    if price is None or (
                        key[2] > price if side == "BUY" else key[2] < price
                    ):
                        price = key[2]
            return price

        for symbol in touched_symbols:
            bid, ask = best(symbol, "BUY"), best(symbol, "SELL")
            if bid is not None and ask is not None and bid >= ask:
                raise ValueError("Completed journal command leaves a crossed book")
        for order_id, new in changes.items():
            if new is None:
                self._orders.pop(order_id, None)
            else:
                self._orders[order_id] = new
        for key, delta in deltas.items():
            old = self._levels.get(key, 0)
            new = old + delta
            prices = self._prices.setdefault(key[:2], [])
            if new:
                self._levels[key] = new
                if not old:
                    insort(prices, key[2])
            elif old:
                del self._levels[key]
                del prices[bisect_left(prices, key[2])]


class NativeJournalReader:
    """Apply complete command groups; ignore incomplete checkpoint attempts."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.venue_id: str | None = None
        self.label: str | None = None
        self.sequence = 0
        self.seen_sequence = 0
        self.timestamp_ns = 0
        self.instruments: dict[str, InstrumentProjection] = {}
        self.book = NativeBook()
        self.run_ended = False
        self.run_failed = False
        self.next_itch_sequence: int | None = None
        self._command: list[Mapping[str, object]] = []
        self._checkpoint: list[Mapping[str, object]] | None = None
        self._bootstrapping = False

    def _apply_command(
        self, rows: list[Mapping[str, object]], marker: Mapping[str, object]
    ) -> None:
        expected = int(marker["event_count"])
        if len(rows) != expected or int(marker["ordinal"]) != expected:
            raise ValueError("Incomplete native command group")
        command = marker["command_sequence"]
        if any(
            row.get("command_sequence") != command or int(row["ordinal"]) != i
            for i, row in enumerate(rows)
        ):
            raise ValueError("Native command ordering disagrees with completion marker")
        # Financial publication is already validated by the engine; the reader
        # nevertheless validates its reconstruction before advancing the cut.
        definitions = {}
        for row in rows:
            if row["kind"] == "INSTRUMENT":
                definitions[str(row["symbol"])] = InstrumentProjection(
                    Decimal(str(row["tick_size"])),
                    Decimal(str(row["lot_size"])),
                    int(row["price_display_precision"]),
                    int(row["quantity_display_precision"]),
                )
        self.book.apply_changes(rows)
        for row in rows:
            kind = row["kind"]
            if kind == "RUN_STARTED":
                self.label = str(row.get("run_label") or "")
            elif kind == "INSTRUMENT":
                self.instruments[str(row["symbol"])] = definitions[str(row["symbol"])]
            elif kind == "RUN_ENDED":
                self.run_ended = True
            elif kind == "RUN_FAILED":
                self.run_failed = True
        self.sequence = int(marker["event_sequence"])
        self.timestamp_ns = int(marker["event_timestamp_ns"])

    def _complete_checkpoint(self, marker: Mapping[str, object]) -> None:
        rows = self._checkpoint
        if rows is None or int(rows[0]["checkpoint_id"]) != int(
            marker["checkpoint_id"]
        ):
            raise ValueError("Checkpoint completion has no matching header")
        orders = rows[1:]
        expected = int(marker["order_count"])
        if len(orders) != expected or int(rows[0]["order_count"]) != expected:
            raise ValueError("Checkpoint order count disagrees with its marker")
        if any(int(row["fifo_position"]) != i for i, row in enumerate(orders)):
            raise ValueError("Checkpoint FIFO positions are incomplete")
        for row in rows:
            if int(row["checkpoint_id"]) != int(marker["checkpoint_id"]) or int(
                row["cut_sequence"]
            ) != int(marker["cut_sequence"]):
                raise ValueError("Checkpoint rows disagree about their cut")
        restored = NativeBook()
        restored.apply_changes(({**row, "kind": "BOOK_ADDED"} for row in orders))
        if not self._bootstrapping:

            def queues(book):
                result = {}
                for order in book.orders.values():
                    key = (order.symbol, order.side, order.price_ticks)
                    result.setdefault(key, []).append(order.order_id)
                return result

            if restored.orders != self.book.orders or queues(restored) != queues(
                self.book
            ):
                raise ValueError(
                    "Checkpoint disagrees with the reconstructed native book"
                )
        self.book = restored
        self.next_itch_sequence = int(marker["next_itch_sequence"])
        self.sequence = int(marker["event_sequence"])
        self.timestamp_ns = int(marker["event_timestamp_ns"])
        self._checkpoint = None

    def apply_rows(self, rows: Iterable[Mapping[str, object]]) -> int:
        applied = 0
        for row in rows:
            sequence = int(row["event_sequence"])
            run_id = str(row["run_id"])
            if self.run_id is None:
                self.run_id = run_id
                self.venue_id = str(row["venue_id"])
            if run_id != self.run_id:
                raise ValueError("A journal reader cannot mix run identities")
            if sequence <= self.seen_sequence:
                continue
            if sequence != self.seen_sequence + 1:
                raise ValueError(f"Native journal sequence gap before {sequence}")
            kind = row["kind"]
            if kind == "COMMAND_COMPLETE":
                self._apply_command(self._command, row)
                self._command = []
                applied += 1
            elif str(kind).startswith("CHECKPOINT_"):
                if self._command:
                    raise ValueError("Checkpoint interrupts an incomplete command")
                if kind == "CHECKPOINT_STARTED":
                    if int(row["cut_sequence"]) != self.sequence:
                        raise ValueError(
                            "Checkpoint cut is not the completed native journal prefix"
                        )
                    self._checkpoint = [row]
                elif kind == "CHECKPOINT_ORDER":
                    if self._checkpoint is None:
                        raise ValueError("Checkpoint order has no header")
                    self._checkpoint.append(row)
                elif kind == "CHECKPOINT_COMPLETE":
                    self._complete_checkpoint(row)
                    applied += 1
                else:
                    raise ValueError("Unknown checkpoint record")
            else:
                self._command.append(row)
            self.seen_sequence = sequence
        return applied

    def bootstrap_checkpoint(
        self,
        rows: Iterable[Mapping[str, object]],
        *,
        metadata_rows: Iterable[Mapping[str, object]] = (),
    ) -> None:
        """Start at a complete checkpoint, with the run's instrument metadata."""
        rows = tuple(rows)
        if (
            not rows
            or rows[0]["kind"] != "CHECKPOINT_STARTED"
            or rows[-1]["kind"] != "CHECKPOINT_COMPLETE"
        ):
            raise ValueError("Checkpoint bootstrap requires a complete immutable spin")
        if self.seen_sequence:
            raise ValueError("Checkpoint bootstrap requires a fresh reader")
        self.seen_sequence = int(rows[0]["event_sequence"]) - 1
        self.sequence = int(rows[0]["cut_sequence"])
        self._bootstrapping = True
        try:
            self.apply_rows(rows)
        finally:
            self._bootstrapping = False
        for row in metadata_rows:
            if str(row["run_id"]) != self.run_id:
                raise ValueError("Checkpoint metadata belongs to another run")
            if row["kind"] == "RUN_STARTED":
                self.label = str(row.get("run_label") or "")
            elif row["kind"] == "INSTRUMENT":
                self.instruments[str(row["symbol"])] = InstrumentProjection(
                    Decimal(str(row["tick_size"])),
                    Decimal(str(row["lot_size"])),
                    int(row["price_display_precision"]),
                    int(row["quantity_display_precision"]),
                )

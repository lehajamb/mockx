"""Streaming CoinAPI L3 reference state. This module never matches orders."""

from __future__ import annotations

from bisect import bisect_left, insort
from contextlib import contextmanager
import csv
from dataclasses import dataclass, replace
from datetime import date as Date, datetime, timezone
import gzip
from itertools import chain
from pathlib import Path
import re

from src.mockx.configuration import MAX_INSTRUMENT_UNITS
from src.mockx.instruments import Instrument, parse_decimal
from src.mockx.models import Side


class SourceFormatError(ValueError):
    """Invalid source data, annotated with its original row ordinal."""


@dataclass(frozen=True, slots=True)
class SourceOrder:
    source_id: str
    raw_source_id: str
    side: Side
    price_ticks: int
    quantity_lots: int


@dataclass(frozen=True, slots=True)
class SourceEvent:
    index: int
    time_ns: int
    coinapi_time_ns: int
    operation: str
    source_id: str
    raw_source_id: str
    side: Side
    price_ticks: int
    quantity_lots: int
    source_remaining_lots: int
    historical_matched_lots: int
    target_mid_twice_ticks: int | None


_FIELDS = {
    "time_exchange",
    "time_coinapi",
    "update_type",
    "is_buy",
    "entry_px",
    "entry_sx",
    "order_id",
}
_OPERATIONS = {"0": "ADD", "1": "SUB", "2": "SET", "3": "DELETE", "4": "MATCH"}
_TIME = re.compile(
    r"(?:(\d{4}-\d{2}-\d{2})[T ])?(\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?\Z"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _timestamp_ns(value, day):
    match = _TIME.fullmatch(value)
    if not match:
        raise SourceFormatError(f"Invalid timestamp: {value!r}")
    calendar, clock, fraction, zone = match.groups()
    if calendar is None:
        if day is None:
            raise SourceFormatError(
                "Time-only timestamps require a date or D-YYYYMMDD filename"
            )
        calendar = day.isoformat()
    instant = datetime.fromisoformat(
        f'{calendar}T{clock}{zone or "+00:00"}'.replace("Z", "+00:00")
    )
    delta = instant.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int(
        (fraction or "").ljust(9, "0")
    )


def _date_context(path, asserted):
    explicit = Date.fromisoformat(asserted) if isinstance(asserted, str) else asserted
    matches = re.findall(r"(?:^|[/\\_])D-(\d{8})(?:\d{2})?(?=[_/\\.]|$)", str(path))
    inferred = {datetime.strptime(value, "%Y%m%d").date() for value in matches}
    if len(inferred) > 1 or (
        explicit is not None and inferred and explicit not in inferred
    ):
        raise SourceFormatError("Asserted date disagrees with capture date metadata")
    return explicit or next(iter(inferred), None)


class _Levels:
    def __init__(self):
        self.quantities = {Side.BUY: {}, Side.SELL: {}}
        self.prices = {Side.BUY: [], Side.SELL: []}

    def change(self, side, price, delta):
        levels, prices = self.quantities[side], self.prices[side]
        before = levels.get(price, 0)
        after = before + delta
        if after < 0:
            raise SourceFormatError("Reference level quantity underflow")
        if not before and after:
            insort(prices, price)
        if after:
            levels[price] = after
        elif before:
            del levels[price]
            prices.pop(bisect_left(prices, price))

    def midpoint(self):
        bids, asks = self.prices[Side.BUY], self.prices[Side.SELL]
        if not bids or not asks:
            return None
        if bids[-1] >= asks[0]:
            raise SourceFormatError(
                "Source book is locked or crossed; unsupported discontinuity/batch"
            )
        return bids[-1] + asks[0]


class ReplaySource:
    """Read the initial snapshot eagerly, then stream increments exactly once."""

    def __init__(self, stream, path, instrument, *, date=None, source_symbol=None):
        self.instrument, self.path = instrument, path
        self.date = _date_context(path, date)
        found = re.search(r"(?:^|_)SC-(.+?)(?=_S-|\.itch|\.csv|$)", path.name)
        filename_symbol = found.group(1) if found else None
        if source_symbol and filename_symbol and source_symbol != filename_symbol:
            raise SourceFormatError("Asserted source symbol disagrees with filename")
        self.source_symbol = source_symbol or filename_symbol
        self.event_count = self.start_ns = self.end_ns = 0
        self._live, self._matched = {}, {}
        self._levels = _Levels()
        self._started = False
        self._last_time = None
        first = stream.readline()
        if not first:
            raise SourceFormatError("Empty input")
        delimiter = ";" if first.count(";") > first.count(",") else ","
        reader = csv.DictReader(
            chain([first.lstrip("\ufeff")], stream), delimiter=delimiter
        )
        if not reader.fieldnames or not _FIELDS.issubset(reader.fieldnames):
            raise SourceFormatError("Missing required CoinAPI L3 columns")
        self._rows = enumerate(reader, start=1)
        seed_times = None
        for index, row in self._rows:
            parsed = self._parse(index, row)
            if parsed[2] != "SNAPSHOT":
                if seed_times is None:
                    raise SourceFormatError("File must begin with an initial SNAPSHOT")
                self._first = index, parsed
                break
            if seed_times is None:
                seed_times = parsed[:2]
                self.start_ns = self.end_ns = parsed[0]
            if parsed[:2] != seed_times:
                raise SourceFormatError(
                    f"row {index}: initial snapshot timestamps must agree"
                )
            try:
                self._apply(index, parsed)
            except ValueError as exc:
                raise SourceFormatError(f"row {index}: {exc}") from exc
        else:
            raise SourceFormatError("Initial snapshot must be followed by an increment")
        self._levels.midpoint()
        self.snapshot = tuple(self._live.values())

    def _parse(self, index, row):
        try:
            if None in row or any(value is None for value in row.values()):
                raise SourceFormatError("Incorrect field count")
            row = {key: value.strip() for key, value in row.items()}
            row_symbol = row.get("symbol_id")
            if row_symbol is not None:
                if not row_symbol or (
                    self.source_symbol and self.source_symbol != row_symbol
                ):
                    raise SourceFormatError("Source symbol metadata disagrees")
                self.source_symbol = row_symbol
            if not self.source_symbol:
                raise SourceFormatError(
                    "Source symbol must be asserted without filename or symbol_id metadata"
                )
            now = _timestamp_ns(row["time_exchange"], self.date)
            received = _timestamp_ns(row["time_coinapi"], self.date)
            if self._last_time is not None and now < self._last_time:
                raise SourceFormatError("Exchange timestamp regression")
            self._last_time = now
            operation = _OPERATIONS.get(row["update_type"], row["update_type"])
            if operation not in {"SNAPSHOT", "ADD", "SUB", "SET", "DELETE", "MATCH"}:
                raise SourceFormatError(f"Unsupported action: {operation}")
            if row["is_buy"] not in {"0", "1"}:
                raise SourceFormatError("is_buy must be 0 or 1")
            side = Side.BUY if row["is_buy"] == "1" else Side.SELL
            raw_id = row["order_id"]
            if not raw_id:
                raise SourceFormatError("L3 order_id is required")
            price = self.instrument.price_to_ticks(row["entry_px"])
            quantity = parse_decimal(row["entry_sx"], field_name="entry_sx")
            lots = (
                0
                if quantity == 0 and operation in {"DELETE", "SET"}
                else self.instrument.quantity_to_lots(quantity)
            )
            return now, received, operation, raw_id, side, price, lots
        except (ValueError, OverflowError) as exc:
            raise SourceFormatError(f"row {index}: {exc}") from exc

    def _apply(self, index, parsed):
        now, received, operation, raw_id, side, price, quantity = parsed
        previous = self._live.get(raw_id)
        if operation in {"SNAPSHOT", "ADD"}:
            if previous:
                raise SourceFormatError(f"Duplicate live order_id: {raw_id}")
            order = SourceOrder(f"{raw_id}#{index}", raw_id, side, price, quantity)
            remaining, matched = quantity, 0
            self._levels.change(side, price, quantity)
        else:
            if previous is None:
                raise SourceFormatError(f"Unknown/terminal source order_id: {raw_id}")
            if (previous.side, previous.price_ticks) != (side, price):
                raise SourceFormatError("Existing source order changed side or price")
            matched = self._matched[raw_id]
            if operation in {"SUB", "MATCH"}:
                remaining = previous.quantity_lots - quantity
                if remaining < 0:
                    raise SourceFormatError("Source quantity underflow")
                matched += quantity if operation == "MATCH" else 0
            else:
                remaining = quantity if operation == "SET" else 0
            if remaining + matched > MAX_INSTRUMENT_UNITS:
                raise SourceFormatError(
                    "Source lifetime quantity exceeds integer bounds"
                )
            order = replace(previous, quantity_lots=remaining)
            self._levels.change(side, price, remaining - previous.quantity_lots)
        if remaining:
            self._live[raw_id], self._matched[raw_id] = order, matched
        else:
            del self._live[raw_id]
            del self._matched[raw_id]
        mid = None if operation == "SNAPSHOT" else self._levels.midpoint()
        return SourceEvent(
            index,
            now,
            received,
            operation,
            order.source_id,
            raw_id,
            side,
            price,
            quantity,
            remaining,
            matched,
            mid,
        )

    def events(self):
        if self._started:
            raise RuntimeError("ReplaySource.events may only be consumed once")
        self._started = True
        rows = chain(
            [self._first],
            ((index, self._parse(index, row)) for index, row in self._rows),
        )
        for index, parsed in rows:
            try:
                if parsed[2] == "SNAPSHOT":
                    raise SourceFormatError(
                        "Later snapshot requires a new replay segment"
                    )
                event = self._apply(index, parsed)
            except (ValueError, OverflowError) as exc:
                raise SourceFormatError(f"row {index}: {exc}") from exc
            self.event_count += 1
            self.end_ns = event.time_ns
            yield event

    def reference_depth(self, limit=10):
        """Historical levels as of the most recently yielded event (or seed)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Depth limit must be a positive integer")
        result = []
        for side in (Side.BUY, Side.SELL):
            prices = self._levels.prices[side]
            selected = reversed(prices[-limit:]) if side == Side.BUY else prices[:limit]
            result.append(
                tuple(
                    (price, self._levels.quantities[side][price]) for price in selected
                )
            )
        return tuple(result)


@contextmanager
def open_source(path, instrument: Instrument, *, date=None, source_symbol=None):
    """Open local CSV/CSV.gz without downloading or executing source contents."""
    source_path = Path(path)
    if not source_path.name.lower().endswith((".csv", ".csv.gz")):
        raise SourceFormatError("Input must be a local .csv or .csv.gz file")
    opener = gzip.open if source_path.name.lower().endswith(".gz") else open
    with opener(source_path, "rt", encoding="utf-8-sig", newline="") as stream:
        yield ReplaySource(
            stream, source_path, instrument, date=date, source_symbol=source_symbol
        )

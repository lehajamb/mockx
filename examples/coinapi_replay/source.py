"""Validate local CoinAPI L3 files and prepare a bounded, disk-backed replay.

Only this offline preparation step reconstructs source state. The resulting
JSONL stream contains one normalized increment, midpoint, and state digest per
source row; the runtime does not reconstruct another order book.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date as Date, datetime, timedelta, timezone
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import zlib

from src.mockx.configuration import MAX_INSTRUMENT_UNITS
from src.mockx.instruments import Instrument, parse_decimal
from src.mockx.models import Side


_COLUMNS = (
    "time_exchange", "time_coinapi", "is_buy", "update_type",
    "entry_px", "entry_sx", "order_id",
)
_OPERATIONS = {"0": "ADD", "1": "SUB", "2": "SET", "3": "DELETE", "4": "MATCH"}
_TIMESTAMP = re.compile(
    r"^(?:(?P<day>\d{4}-\d{2}-\d{2})[T ])?"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?(?P<zone>Z|[+-]\d{2}:\d{2})?$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class SourceFormatError(ValueError):
    """A local source cannot be replayed under the supported L3 contract."""


@dataclass(frozen=True, slots=True)
class SourceOrder:
    source_id: str
    side: Side
    price_ticks: int
    quantity_lots: int


@dataclass(frozen=True, slots=True)
class SourceEvent:
    # Original 1-based data-row ordinal, including the initial snapshot rows.
    index: int
    time_ns: int
    operation: str
    source_id: str
    side: Side
    price_ticks: int
    quantity_lots: int
    source_remaining_lots: int
    historical_matched_lots: int
    target_mid_twice_ticks: int | None
    expected_digest: str
    coinapi_time_ns: int | None = None


def source_digest(
    orders: Iterable[SourceOrder] | Mapping[str, SourceOrder],
) -> str:
    """Hash sorted live identities, sides, prices and leaves; exclude FIFO."""
    values = orders.values() if isinstance(orders, Mapping) else orders
    digest = hashlib.sha256()
    previous: str | None = None
    for order in sorted(values, key=lambda item: item.source_id):
        if not order.source_id or order.source_id == previous:
            raise ValueError("Source digest requires unique nonempty source IDs")
        if order.price_ticks <= 0 or order.quantity_lots <= 0:
            raise ValueError("Source digest requires positive live prices and quantities")
        record = [order.source_id, order.side.value, order.price_ticks, order.quantity_lots]
        digest.update(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
        previous = order.source_id
    return digest.hexdigest()


@dataclass(slots=True)
class PreparedReplay:
    snapshot: tuple[SourceOrder, ...]
    start_ns: int
    source_sha256: str
    snapshot_digest: str
    event_count: int
    end_ns: int
    source_symbol: str
    source_path: Path
    _directory: tempfile.TemporaryDirectory
    _event_path: Path
    _submicro_row: int | None = None
    _closed: bool = False
    _active_streams: set[io.TextIOWrapper] = field(default_factory=set, init=False, repr=False)

    def __enter__(self) -> PreparedReplay:
        if self._closed:
            raise RuntimeError("Prepared replay is closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            for stream in self._active_streams:
                stream.close()
            self._active_streams.clear()
            self._directory.cleanup()
            self._closed = True

    def events(self) -> Iterator[SourceEvent]:
        """Read the prepared stream afresh without loading all events."""
        if self._closed:
            raise RuntimeError("Prepared replay is closed")
        stream = self._event_path.open("r", encoding="utf-8")
        self._active_streams.add(stream)
        try:
            with stream:
                while not self._closed:
                    line = stream.readline()
                    if not line:
                        break
                    record = json.loads(line)
                    record["side"] = Side(record["side"])
                    yield SourceEvent(**record)
        finally:
            self._active_streams.discard(stream)

    def assert_exact_runtime_precision(self) -> None:
        """Reject elapsed nanoseconds unsupported by MockX's datetime clock."""
        if self._submicro_row is not None:
            raise SourceFormatError(
                f"row {self._submicro_row}: elapsed source time has submicrosecond "
                "precision; exact replay cannot round it to MockX's microsecond clock"
            )


class _HashingReader(io.RawIOBase):
    def __init__(self, source: io.BufferedReader, digest):
        super().__init__()
        self.source = source
        self.digest = digest

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        count = self.source.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
        return count


@contextmanager
def _open_source(path: Path, digest) -> Iterator[io.TextIOWrapper]:
    with path.open("rb") as raw:
        with io.BufferedReader(_HashingReader(raw, digest)) as hashed:
            if path.name.lower().endswith(".csv.gz"):
                with gzip.GzipFile(fileobj=hashed, mode="rb") as uncompressed:
                    with io.TextIOWrapper(uncompressed, encoding="utf-8-sig", newline="") as text:
                        yield text
            else:
                with io.TextIOWrapper(hashed, encoding="utf-8-sig", newline="") as text:
                    yield text


def _date_context(path: Path, value: Date | str | None) -> Date | None:
    if isinstance(value, datetime):
        raise SourceFormatError("date must be a calendar date, not a datetime")
    if isinstance(value, str):
        try:
            value = Date.fromisoformat(value)
        except ValueError as exc:
            raise SourceFormatError("date must be YYYY-MM-DD") from exc
    if value is not None and not isinstance(value, Date):
        raise SourceFormatError("date must be YYYY-MM-DD or a date object")
    partitions: set[Date] = set()
    for part in path.parts:
        match = re.fullmatch(r"D-(\d{8})(\d{2})?", part)
        if match is not None:
            try:
                partitions.add(datetime.strptime(match[1], "%Y%m%d").date())
                if match[2] is not None and not 0 <= int(match[2]) <= 23:
                    raise ValueError("invalid partition hour")
            except ValueError as exc:
                raise SourceFormatError(f"Invalid date partition: {part}") from exc
    if len(partitions) > 1 or (value is not None and partitions and value not in partitions):
        raise SourceFormatError("Conflicting explicit date and/or file date partitions")
    return value or next(iter(partitions), None)


def _timestamp_ns(value: str, context: Date | None) -> int:
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise SourceFormatError(f"Invalid timestamp or precision greater than nanoseconds: {value!r}")
    day = Date.fromisoformat(match["day"]) if match["day"] else context
    if day is None:
        raise SourceFormatError("Time-only timestamp requires date or a D-YYYYMMDD[HH] partition")
    zone = match["zone"]
    offset = timezone.utc
    if zone not in (None, "Z"):
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        if hours > 23 or minutes > 59:
            raise SourceFormatError(f"Invalid timestamp UTC offset: {zone}")
        direction = 1 if zone[0] == "+" else -1
        offset = timezone(direction * timedelta(hours=hours, minutes=minutes))
    instant = datetime(
        day.year, day.month, day.day, int(match["hour"]),
        int(match["minute"]), int(match["second"]), tzinfo=offset,
    )
    elapsed = instant - _EPOCH
    fraction_ns = int((match["fraction"] or "0").ljust(9, "0"))
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000_000 + fraction_ns


def _filename_symbol(path: Path) -> str | None:
    name = path.name[:-7] if path.name.lower().endswith(".csv.gz") else path.name[:-4]
    match = re.search(r"(?:^|\+)SC-([^+]+)(?:\+|$)", name)
    return match[1] if match else None


def _csv_rows(stream: io.TextIOWrapper) -> Iterator[tuple[int, dict[str, str]]]:
    header = stream.readline()
    fields: list[str] | None = None
    delimiter: str | None = None
    for candidate in (",", ";"):
        possible = next(csv.reader([header], delimiter=candidate, strict=True))
        if len(possible) == len(set(possible)) and set(possible) in (
            set(_COLUMNS), set(_COLUMNS) | {"symbol_id"},
        ):
            fields, delimiter = possible, candidate
            break
    if fields is None or delimiter is None:
        raise SourceFormatError(
            "CSV header must contain exactly " + ",".join(_COLUMNS)
            + " (optionally symbol_id), separated by commas or semicolons"
        )
    for index, row in enumerate(csv.reader(stream, delimiter=delimiter, strict=True), start=1):
        if len(row) != len(fields):
            raise SourceFormatError(f"row {index}: expected {len(fields)} CSV fields, got {len(row)}")
        yield index, dict(zip(fields, (value.strip() for value in row)))


def _midpoint(orders: Mapping[str, SourceOrder]) -> int | None:
    bid: int | None = None
    ask: int | None = None
    for order in orders.values():
        if order.side == Side.BUY:
            bid = order.price_ticks if bid is None else max(bid, order.price_ticks)
        else:
            ask = order.price_ticks if ask is None else min(ask, order.price_ticks)
    if bid is not None and ask is not None:
        if bid >= ask:
            raise SourceFormatError("Source state is locked or crossed; an unsupported atomic batch may be missing")
        return bid + ask
    return None


def prepare_csv(
    path: str | Path,
    instrument: Instrument,
    *,
    date: Date | str | None = None,
    symbol: str | None = None,
) -> PreparedReplay:
    """Preflight one local L3 CSV/gzip file before any exchange mutation.

    ``symbol`` labels the CoinAPI source; ``instrument.symbol`` is the caller's
    explicit MockX destination. A filename SC component or a symbol_id column
    can supply the source label. An initial same-timestamp SNAPSHOT group must
    be followed by an increment. Later snapshots and reused IDs are unsupported.
    """
    if "://" in str(path):
        raise SourceFormatError("Only local .csv or .csv.gz sources are supported")
    source_path = Path(path).resolve()
    if not source_path.name.lower().endswith((".csv", ".csv.gz")):
        raise SourceFormatError("Only local .csv or .csv.gz sources are supported")
    day = _date_context(source_path, date)
    filename_symbol = _filename_symbol(source_path)
    if symbol is not None:
        symbol = symbol.strip()
        if not symbol:
            raise SourceFormatError("Explicit source symbol must not be empty")
    if symbol and filename_symbol and symbol != filename_symbol:
        raise SourceFormatError("Explicit source symbol disagrees with filename SC metadata")
    source_symbol = symbol or filename_symbol
    directory = tempfile.TemporaryDirectory(prefix="mockx_coinapi_")
    event_path = Path(directory.name) / "events.jsonl"
    digest = hashlib.sha256()
    live: dict[str, SourceOrder] = {}
    matched: dict[str, int] = {}
    snapshot: tuple[SourceOrder, ...] | None = None
    snapshot_times: tuple[int, int] | None = None
    snapshot_digest: str | None = None
    last_time: int | None = None
    start_ns: int | None = None
    event_count = 0
    submicro_row: int | None = None
    current_row = 0
    try:
        # IDs are retained on disk to reject reuse without retaining every
        # historical order in memory after it leaves the live source state.
        with closing(sqlite3.connect(Path(directory.name) / "identities.sqlite3")) as identities:
            identities.execute("CREATE TABLE source_ids (id TEXT PRIMARY KEY, first_row INTEGER NOT NULL)")
            with _open_source(source_path, digest) as stream, event_path.open("w", encoding="utf-8", newline="\n") as output:
                for index, row in _csv_rows(stream):
                    current_row = index
                    try:
                        row_symbol = row.get("symbol_id")
                        if row_symbol is not None and (not row_symbol or (source_symbol and row_symbol != source_symbol)):
                            raise SourceFormatError("symbol_id is empty or disagrees with the source symbol")
                        source_symbol = source_symbol or row_symbol
                        if source_symbol is None:
                            raise SourceFormatError("Explicit source symbol is required without filename SC or symbol_id metadata")
                        time_ns = _timestamp_ns(row["time_exchange"], day)
                        coinapi_time_ns = _timestamp_ns(row["time_coinapi"], day)
                        if last_time is not None and time_ns < last_time:
                            raise SourceFormatError("Chronological regression in time_exchange")
                        last_time = time_ns
                        operation = _OPERATIONS.get(row["update_type"], row["update_type"])
                        if operation not in {"SNAPSHOT", "ADD", "SUB", "SET", "DELETE", "MATCH"}:
                            raise SourceFormatError(f"Unsupported update_type: {operation!r}")
                        source_id = row["order_id"]
                        if not source_id:
                            raise SourceFormatError("L3 order_id is required; aggregated L2 data is unsupported")
                        if row["is_buy"] not in {"0", "1"}:
                            raise SourceFormatError("is_buy must be 0 or 1")
                        side = Side.BUY if row["is_buy"] == "1" else Side.SELL
                        price_ticks = instrument.price_to_ticks(row["entry_px"])
                        raw_quantity = parse_decimal(row["entry_sx"], field_name="entry_sx")
                        if raw_quantity == 0 and operation in {"SET", "DELETE"}:
                            quantity = 0
                        else:
                            quantity = instrument.quantity_to_lots(raw_quantity)
                        if start_ns is None:
                            if operation != "SNAPSHOT":
                                raise SourceFormatError("File must start with a complete explicit SNAPSHOT group")
                            start_ns = time_ns
                            snapshot_times = (time_ns, coinapi_time_ns)
                        if operation == "SNAPSHOT":
                            if snapshot is not None:
                                raise SourceFormatError("Later SNAPSHOT is a discontinuity; start a new replay segment")
                            if snapshot_times != (time_ns, coinapi_time_ns):
                                raise SourceFormatError("Initial SNAPSHOT rows must have identical exchange and CoinAPI timestamps")
                        elif snapshot is None:
                            _midpoint(live)
                            snapshot = tuple(live.values())
                            snapshot_digest = source_digest(snapshot)
                        if operation in {"SNAPSHOT", "ADD"}:
                            try:
                                identities.execute("INSERT INTO source_ids VALUES (?, ?)", (source_id, index))
                            except sqlite3.IntegrityError as exc:
                                reason = "duplicate live order_id" if source_id in live else "reused order_id lineage is unsupported"
                                raise SourceFormatError(f"{reason}: {source_id!r}") from exc
                            live[source_id] = SourceOrder(source_id, side, price_ticks, quantity)
                            matched[source_id] = 0
                            remaining, historical_matched = quantity, 0
                        else:
                            previous = live.get(source_id)
                            if previous is None:
                                raise SourceFormatError(f"Unknown or source-terminal order_id: {source_id!r}")
                            if previous.side != side or previous.price_ticks != price_ticks:
                                raise SourceFormatError("Existing order changed side/price; this source operation is unsupported")
                            historical_matched = matched[source_id]
                            if operation in {"SUB", "MATCH"}:
                                if quantity > previous.quantity_lots:
                                    raise SourceFormatError("Source quantity underflow")
                                remaining = previous.quantity_lots - quantity
                                if operation == "MATCH":
                                    historical_matched += quantity
                            elif operation == "SET":
                                remaining = quantity
                            else:
                                remaining = 0
                            if remaining + historical_matched > MAX_INSTRUMENT_UNITS:
                                raise SourceFormatError("Effective source order total exceeds MockX integer quantity bounds")
                            if remaining:
                                live[source_id] = SourceOrder(source_id, side, price_ticks, remaining)
                                matched[source_id] = historical_matched
                            else:
                                del live[source_id]
                                del matched[source_id]
                        if operation == "SNAPSHOT":
                            continue
                        midpoint = _midpoint(live)
                        if submicro_row is None and (time_ns - start_ns) % 1000:
                            submicro_row = index
                        event = SourceEvent(
                            index=index, time_ns=time_ns, operation=operation,
                            source_id=source_id, side=side, price_ticks=price_ticks,
                            quantity_lots=quantity, source_remaining_lots=remaining,
                            historical_matched_lots=historical_matched,
                            target_mid_twice_ticks=midpoint,
                            expected_digest=source_digest(live), coinapi_time_ns=coinapi_time_ns,
                        )
                        record = asdict(event)
                        record["side"] = event.side.value
                        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                        event_count += 1
                    except (ValueError, OverflowError) as exc:
                        raise SourceFormatError(f"row {index}: {exc}") from exc
        if snapshot is None:
            raise SourceFormatError("Initial SNAPSHOT completeness is unknown: a following increment is required")
        assert start_ns is not None and last_time is not None and snapshot_digest is not None
        assert source_symbol is not None
        return PreparedReplay(
            snapshot=snapshot, start_ns=start_ns, source_sha256=digest.hexdigest(),
            snapshot_digest=snapshot_digest, event_count=event_count, end_ns=last_time,
            source_symbol=source_symbol, source_path=source_path,
            _directory=directory, _event_path=event_path, _submicro_row=submicro_row,
        )
    except BaseException as exc:
        directory.cleanup()
        if isinstance(exc, (OSError, EOFError, zlib.error, UnicodeError, csv.Error)):
            raise SourceFormatError(f"Cannot read local source near row {current_row}: {exc}") from exc
        raise

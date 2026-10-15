"""Build bounded, disposable Grafana depth projections from the native journal."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from decimal import Decimal
import logging
import os
from pathlib import Path
import sys
import time
from typing import Iterable, Mapping
from urllib.parse import urlsplit

import httpx
import numpy as np
from questdb.ingress import IngressError, Protocol, Sender, TimestampNanos

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.mockx.journal_reader import NativeJournalReader

LOG = logging.getLogger("mockx.dashboard")
DEPTH = 40
DEPTH_COLUMNS = {
    "timestamp": "TIMESTAMP_NS", "venue_id": "SYMBOL", "run_id": "SYMBOL",
    "symbol": "SYMBOL", "run_label": "VARCHAR", "state": "SYMBOL",
    "source_sequence": "LONG", "source_head_sequence": "LONG",
    "source_timestamp_ns": "LONG", "published_at_ns": "LONG",
    "bids": "DOUBLE[][]", "asks": "DOUBLE[][]",
    "best_bid": "DOUBLE", "best_ask": "DOUBLE",
}
DEPTH_KEYS = {"timestamp", "venue_id", "run_id", "symbol", "source_sequence"}
DEPTH_DDL = (
    "CREATE TABLE IF NOT EXISTS dashboard_depth ("
    + ", ".join(f"{name} {kind}" for name, kind in DEPTH_COLUMNS.items())
    + ") TIMESTAMP(timestamp) PARTITION BY DAY WAL DEDUP UPSERT KEYS("
    + ", ".join(name for name in DEPTH_COLUMNS if name in DEPTH_KEYS) + ")"
)
CURSOR_COLUMNS = {
    "timestamp": "TIMESTAMP_NS", "venue_id": "SYMBOL", "run_id": "SYMBOL",
    "run_label": "VARCHAR", "state": "SYMBOL", "source_sequence": "LONG",
    "source_head_sequence": "LONG", "book_sequence": "LONG", "error": "VARCHAR",
}
CURSOR_DDL = (
    "CREATE TABLE IF NOT EXISTS dashboard_cursor ("
    + ", ".join(f"{name} {kind}" for name, kind in CURSOR_COLUMNS.items())
    + ") TIMESTAMP(timestamp) PARTITION BY DAY WAL DEDUP UPSERT KEYS(timestamp, run_id)"
)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def decode_rows(payload: Mapping[str, object]) -> list[dict[str, object]]:
    if "error" in payload:
        raise RuntimeError(f"QuestDB query failed: {payload['error']}")
    columns = payload.get("columns", [])
    names = [column["name"] for column in columns]
    result = []
    for values in payload.get("dataset", []):
        if len(values) != len(names):
            raise ValueError("QuestDB result has inconsistent columns")
        result.append(dict(zip(names, values)))
    return result


@dataclass(frozen=True)
class DepthProjection:
    """An immutable source cut; retries retain the complete original payload."""

    venue_id: str
    run_id: str
    symbol: str
    run_label: str
    state: str
    source_sequence: int
    source_head_sequence: int
    source_timestamp_ns: int
    published_at_ns: int
    bids: tuple[tuple[float, ...], tuple[float, ...]]
    asks: tuple[tuple[float, ...], tuple[float, ...]]


@dataclass(frozen=True)
class CursorProjection:
    venue_id: str
    run_id: str
    run_label: str
    state: str
    source_sequence: int
    source_head_sequence: int
    book_sequence: int
    timestamp_ns: int
    error: str = ""


def depth_array(levels: Iterable[tuple[int, int]], tick: Decimal, lot: Decimal):
    prices = [float("nan")] * DEPTH
    quantities = [float("nan")] * DEPTH
    for index, (price, quantity) in enumerate(levels):
        if index == DEPTH:
            break
        prices[index] = float(price * tick)
        quantities[index] = float(quantity * lot)
    return tuple(prices), tuple(quantities)


def capture_depth(reader: NativeJournalReader, head: int, published_at_ns: int,
                  *, sequence=None, timestamp_ns=None):
    if not reader.run_id or not reader.venue_id or not reader.sequence:
        return ()
    status = "FAILED" if reader.run_failed else "ENDED" if reader.run_ended else "LIVE"
    rows = []
    for symbol, instrument in sorted(reader.instruments.items()):
        bids, asks = reader.book.depth(symbol, DEPTH)
        rows.append(DepthProjection(
            reader.venue_id, reader.run_id, symbol, reader.label or "", status,
            reader.sequence if sequence is None else sequence, head,
            reader.timestamp_ns if timestamp_ns is None else timestamp_ns, published_at_ns,
            depth_array(bids, instrument.tick_size, instrument.lot_size),
            depth_array(asks, instrument.tick_size, instrument.lot_size),
        ))
    return tuple(rows)


class QuestDBProjectionStore:
    def __init__(self, url: str, username: str | None, password: str | None):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("QuestDB URL must use HTTP(S)")
        self.url = url.rstrip("/")
        self.sender_args = (
            Protocol.Http if parsed.scheme == "http" else Protocol.Https,
            parsed.hostname, parsed.port or (80 if parsed.scheme == "http" else 443),
        )
        self.sender_options = dict(auto_flush=False, retry_timeout=0, request_timeout=5000)
        if username is not None:
            self.sender_options.update(username=username, password=password)
        self.client = httpx.Client(timeout=5, auth=(username, password) if username else None)

    def query(self, query: str):
        response = self.client.get(f"{self.url}/exec", params={"query": query})
        if response.status_code == 400:
            raise RuntimeError(f"QuestDB SQL: {response.json().get('error', response.text)}")
        response.raise_for_status()
        return decode_rows(response.json())

    def ensure_schema(self):
        for table, ddl, columns, expected_keys in (
            ("dashboard_depth", DEPTH_DDL, DEPTH_COLUMNS, DEPTH_KEYS),
            ("dashboard_cursor", CURSOR_DDL, CURSOR_COLUMNS, {"timestamp", "run_id"}),
        ):
            self.query(ddl)
            metadata = self.query(
                "SELECT designatedTimestamp, partitionBy, walEnabled, dedup "
                f"FROM tables() WHERE table_name={sql_literal(table)}"
            )
            if (len(metadata) != 1 or metadata[0].get("designatedTimestamp") != "timestamp"
                    or metadata[0].get("partitionBy") != "DAY"
                    or metadata[0].get("walEnabled") is not True
                    or metadata[0].get("dedup") is not True):
                raise ValueError(f"{table} metadata is incompatible; use a fresh native schema volume")
            rows = self.query(f"SELECT * FROM table_columns('{table}')")
            actual = {row.get("column", row.get("name")): str(row["type"]).upper() for row in rows}
            if actual != columns:
                raise ValueError(f"{table} schema is incompatible; use a fresh native schema volume")
            keys = {row.get("column", row.get("name")) for row in rows if row.get("upsertKey") is True}
            if keys != expected_keys:
                raise ValueError(f"{table} dedup keys are incompatible")

    def write(self, projections):
        sender = Sender(*self.sender_args, **self.sender_options)
        try:
            sender.establish()
            for row in projections:
                if isinstance(row, CursorProjection):
                    sender.row("dashboard_cursor", symbols={
                        "venue_id": row.venue_id, "run_id": row.run_id, "state": row.state,
                    }, columns={
                        "run_label": row.run_label, "source_sequence": row.source_sequence,
                        "source_head_sequence": row.source_head_sequence,
                        "book_sequence": row.book_sequence, "error": row.error,
                    }, at=TimestampNanos(row.timestamp_ns))
                    continue
                sender.row("dashboard_depth", symbols={
                    "venue_id": row.venue_id, "run_id": row.run_id,
                    "symbol": row.symbol, "state": row.state,
                }, columns={
                    "run_label": row.run_label, "source_sequence": row.source_sequence,
                    "source_head_sequence": row.source_head_sequence,
                    "source_timestamp_ns": row.source_timestamp_ns,
                    "published_at_ns": row.published_at_ns,
                    "bids": np.asarray(row.bids, dtype=np.float64),
                    "asks": np.asarray(row.asks, dtype=np.float64),
                    "best_bid": row.bids[0][0], "best_ask": row.asks[0][0],
                }, at=TimestampNanos(row.source_timestamp_ns))
            sender.flush()
        finally:
            sender.close(flush=False)

    def close(self):
        self.client.close()


@dataclass
class RunProjection:
    reader: NativeJournalReader = field(default_factory=NativeJournalReader)
    published_sequence: int = 0
    failed: bool = False
    pending: tuple[DepthProjection | CursorProjection, ...] = ()
    depth_sequence: int = 0
    depth_timestamp_ns: int = 0
    command_changes_book: bool = False
    error: str = ""


class Projector:
    """One reader per run; incomplete commands never enter a display snapshot."""

    def __init__(self, store, *, page_size=10000, from_start=False, clock=time.monotonic, wall_clock=time.time_ns):
        if not 1 <= page_size <= 100000:
            raise ValueError("page size must be between 1 and 100000")
        self.store = store
        self.page_size = page_size
        self.from_start = from_start
        self.clock = clock
        self.wall_clock = wall_clock
        self.runs: dict[str, RunProjection] = {}

    def _bootstrap(self, run_id):
        run = RunProjection()
        if self.from_start:
            return run
        literal = sql_literal(run_id)
        markers = self.store.query(
            f"SELECT checkpoint_id FROM exchange_events WHERE run_id={literal} "
            "AND kind='CHECKPOINT_COMPLETE' ORDER BY event_sequence DESC LIMIT 1"
        )
        if markers:
            rows = self.store.query(
                f"SELECT * FROM exchange_events WHERE run_id={literal} "
                f"AND checkpoint_id={int(markers[0]['checkpoint_id'])} ORDER BY event_sequence"
            )
            metadata = self.store.query(
                f"SELECT * FROM exchange_events WHERE run_id={literal} "
                "AND kind IN ('RUN_STARTED','INSTRUMENT') ORDER BY event_sequence"
            )
            run.reader.bootstrap_checkpoint(rows, metadata_rows=metadata)
            run.depth_sequence = run.reader.sequence
            run.depth_timestamp_ns = run.reader.timestamp_ns
        return run

    def tick(self):
        heads = self.store.query(
            "SELECT run_id, max(event_sequence) AS head FROM exchange_events GROUP BY run_id"
        )
        for item in heads:
            run_id, head = str(item["run_id"]), int(item["head"])
            if run_id not in self.runs:
                self.runs[run_id] = self._bootstrap(run_id)
            run = self.runs[run_id]
            if run.pending:
                self.store.write(run.pending)
                run.published_sequence = run.depth_sequence
                run.pending = ()
                continue
            if (run.reader.run_ended or run.reader.run_failed or run.failed) and run.reader.seen_sequence == head:
                continue
            deadline = self.clock() + 0.20
            while not run.failed and run.reader.seen_sequence < head and self.clock() < deadline:
                rows = self.store.query(
                    f"SELECT * FROM exchange_events WHERE run_id={sql_literal(run_id)} "
                    f"AND event_sequence>{run.reader.seen_sequence} AND event_sequence<={head} "
                    f"ORDER BY event_sequence LIMIT {self.page_size}"
                )
                if not rows:
                    break
                try:
                    # Track book cuts separately from report delivery, whose
                    # originating timestamp can precede an ended run's book.
                    for row in rows:
                        kind = str(row["kind"])
                        if kind.startswith("BOOK_") or kind in {"RUN_STARTED", "RUN_ENDED", "RUN_FAILED"}:
                            run.command_changes_book = True
                        run.reader.apply_rows((row,))
                        if kind == "CHECKPOINT_COMPLETE" or (kind == "COMMAND_COMPLETE" and run.command_changes_book):
                            run.depth_sequence = run.reader.sequence
                            run.depth_timestamp_ns = run.reader.timestamp_ns
                        if kind == "COMMAND_COMPLETE":
                            run.command_changes_book = False
                except ValueError as exc:
                    run.failed = True
                    run.error = str(exc)
                    LOG.exception("Native journal cannot be reconstructed for run %s", run_id)
                    break
            wall_time = self.wall_clock()
            depth = ()
            if run.depth_sequence > run.published_sequence:
                depth = capture_depth(run.reader, head, wall_time,
                                      sequence=run.depth_sequence, timestamp_ns=run.depth_timestamp_ns)
            if run.reader.run_id:
                state = "INVALID" if run.failed else "FAILED" if run.reader.run_failed else "ENDED" if run.reader.run_ended else "CATCHING_UP" if run.reader.sequence < head else "LIVE"
                run.pending = (*depth, CursorProjection(
                    run.reader.venue_id, run_id, run.reader.label or "", state,
                    run.reader.sequence, head, run.depth_sequence, wall_time, run.error,
                ))
                self.store.write(run.pending)
                run.published_sequence = run.depth_sequence
                run.pending = ()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questdb-url", default=os.getenv("QUESTDB_URL", "http://127.0.0.1:9000"))
    parser.add_argument("--interval-ms", type=int, default=250)
    parser.add_argument("--page-size", type=int, default=10000)
    parser.add_argument("--from-start", action="store_true", help="Reconstruct from event 1 rather than the latest complete checkpoint")
    args = parser.parse_args(argv)
    if args.interval_ms < 250:
        parser.error("projection interval must be at least 250 ms")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = QuestDBProjectionStore(args.questdb_url, os.getenv("QUESTDB_USERNAME"), os.getenv("QUESTDB_PASSWORD"))
    projector = Projector(store, page_size=args.page_size, from_start=args.from_start)
    schema_ready = False
    try:
        while True:
            started = time.monotonic()
            try:
                if not schema_ready:
                    store.ensure_schema()
                    schema_ready = True
                projector.tick()
            except (httpx.HTTPError, IngressError, RuntimeError) as exc:
                LOG.warning("Projection is waiting for QuestDB: %s", exc)
            time.sleep(max(0, args.interval_ms/1000 - (time.monotonic()-started)))
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

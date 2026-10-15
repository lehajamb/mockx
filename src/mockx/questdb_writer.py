"""QuestDB persistence adapter for immutable metric records."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import httpx

try:
    from questdb.ingress import Protocol, Sender, TimestampNanos
except ModuleNotFoundError:  # Core metrics tests require no QuestDB package.
    Protocol = Sender = TimestampNanos = None

from .metrics import MetricRecord, MetricsSinkError


IDENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("timestamp", "TIMESTAMP_NS"),
    ("run_id", "SYMBOL"),
    ("event_sequence", "LONG"),
    ("event_timestamp_ns", "LONG"),
)


@dataclass(frozen=True, slots=True)
class QuestDBTableSchema:
    name: str
    columns: tuple[tuple[str, str], ...]

    @property
    def ddl(self) -> str:
        definitions = ",\n    ".join(
            f"{name} {column_type}" for name, column_type in self.columns
        )
        return (
            f"CREATE TABLE IF NOT EXISTS {self.name} (\n"
            f"    {definitions}\n"
            ") TIMESTAMP(timestamp) PARTITION BY DAY WAL "
            "DEDUP UPSERT KEYS(timestamp, run_id, event_sequence)"
        )


def _schema(
    name: str,
    *,
    symbols: Sequence[str],
    longs: Sequence[str] = (),
    doubles: Sequence[str] = (),
    booleans: Sequence[str] = (),
    varchars: Sequence[str] = (),
) -> QuestDBTableSchema:
    return QuestDBTableSchema(
        name=name,
        columns=(
            *IDENTITY_COLUMNS,
            *((column, "SYMBOL") for column in symbols),
            *((column, "LONG") for column in longs),
            *((column, "DOUBLE") for column in doubles),
            *((column, "BOOLEAN") for column in booleans),
            *((column, "VARCHAR") for column in varchars),
        ),
    )


QUESTDB_SCHEMAS: tuple[QuestDBTableSchema, ...] = (
    _schema(
        "exchange_events",
        symbols=(
            "venue_id",
            "kind",
            "command_type",
            "symbol",
            "side",
            "order_type",
            "tif",
            "ord_status",
            "exec_type",
            "liquidity_category",
            "session_alias",
            "buyer_session_alias",
            "seller_session_alias",
            "aggressor_side",
            "metric_name",
            "reason_code",
        ),
        longs=(
            "command_sequence",
            "ordinal",
            "event_count",
            "ingress_sequence",
            "order_id",
            "logical_order_id",
            "order_version",
            "price_ticks",
            "qty_lots",
            "order_qty_lots",
            "cum_qty_lots",
            "leaves_qty_lots",
            "canceled_qty_lots",
            "filled_notional_tick_lots",
            "last_price_ticks",
            "last_qty_lots",
            "trade_id",
            "match_number",
            "buyer_order_id",
            "seller_order_id",
            "aggressor_order_id",
            "passive_order_id",
            "notional_tick_lots",
            "prevented_qty_lots",
            "remaining_qty_lots",
            "submitted_at_ns",
            "report_created_at_ns",
            "duration_ns",
            "checkpoint_id",
            "cut_sequence",
            "next_itch_sequence",
            "order_count",
            "fifo_position",
            "price_display_precision",
            "quantity_display_precision",
        ),
        varchars=(
            "run_label",
            "session_id",
            "participant_id",
            "cl_ord_id",
            "orig_cl_ord_id",
            "list_id",
            "exec_id",
            "exchange_order_id",
            "exchange_trade_id",
            "buyer_participant_id",
            "seller_participant_id",
            "text",
            "tick_size",
            "lot_size",
            "metadata_json",
        ),
    ),
)


class QuestDBMetricsSink:
    """Acknowledged ILP/HTTP ingestion with REST-managed fixed schemas."""

    def __init__(
        self,
        *,
        url: str,
        health_url: str,
        username: str | None,
        password: str | None,
        request_timeout_ms: int,
    ) -> None:
        if Sender is None or Protocol is None or TimestampNanos is None:
            raise MetricsSinkError("QuestDB support requires the questdb package")
        if request_timeout_ms <= 0:
            raise ValueError("QuestDB request_timeout_ms must be positive")

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("QuestDB url must be an absolute HTTP(S) URL")
        protocol = Protocol.Http if parsed.scheme == "http" else Protocol.Https
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
        sender_options: dict[str, Any] = {
            "auto_flush": False,
            "retry_timeout": 0,
            "request_timeout": request_timeout_ms,
        }
        if username is not None:
            sender_options.update(username=username, password=password)

        self._timeout_seconds = request_timeout_ms / 1000
        self._health_url = health_url
        self._sql_url = f"{url.rstrip('/')}/exec"
        self._closed = False
        self._schema_ready = False
        self._close_lock = asyncio.Lock()
        self._operation_lock = threading.RLock()
        sender = None
        http_client = None
        try:
            sender = Sender(
                protocol,
                parsed.hostname,
                port,
                **sender_options,
            )
            sender.establish()
            http_client = httpx.Client(
                timeout=self._timeout_seconds,
                auth=(
                    (username, password)
                    if username is not None and password is not None
                    else None
                ),
            )
        except Exception as exc:
            if http_client is not None:
                try:
                    http_client.close()
                except Exception:
                    pass
            if sender is not None:
                try:
                    sender.close(flush=False)
                except Exception:
                    pass
            raise MetricsSinkError(
                "QuestDB metrics client initialization failed"
            ) from exc
        self._sender = sender
        self._http = http_client

    async def write(self, records: Sequence[MetricRecord]) -> None:
        if self._closed:
            raise MetricsSinkError("QuestDB metrics sink is closed")
        if not records:
            return
        await self._run_with_timeout(
            self._write_sync,
            records,
            message="QuestDB metric write failed",
        )

    def _write_sync(self, records: Sequence[MetricRecord]) -> None:
        if any(record.table != "exchange_events" for record in records):
            raise ValueError("The exchange sink writes only the native journal")
        with self._operation_lock:
            self._ensure_schema_sync()
            buffer = self._sender.new_buffer()
            for record in records:
                buffer.row(
                    record.table,
                    symbols=record.symbol_map,
                    columns=record.column_map,
                    at=TimestampNanos(record.timestamp_ns),
                )
            self._sender.flush(buffer, transactional=True)

    async def flush(self) -> None:
        if self._closed:
            return
        await self._run_with_timeout(
            self._flush_sync,
            message="QuestDB metric flush failed",
        )

    def _flush_sync(self) -> None:
        with self._operation_lock:
            self._sender.flush()

    async def health_check(self) -> None:
        if self._closed:
            raise MetricsSinkError("QuestDB metrics sink is closed")
        await self._run_with_timeout(
            self._health_check_sync,
            message="QuestDB metrics health check failed",
        )

    def _health_check_sync(self) -> None:
        with self._operation_lock:
            response = self._http.get(self._health_url)
            response.raise_for_status()
            self._ensure_schema_sync()

    def _ensure_schema_sync(self) -> None:
        if self._schema_ready:
            return
        for schema in QUESTDB_SCHEMAS:
            self._execute_sql(schema.ddl)
            self._verify_schema(schema)
        self._schema_ready = True

    def _execute_sql(self, query: str) -> Mapping[str, Any]:
        response = self._http.get(self._sql_url, params={"query": query})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("QuestDB SQL returned an invalid response")
        if "error" in payload:
            raise ValueError(f"QuestDB SQL error: {payload['error']}")
        return payload

    @staticmethod
    def _rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        columns = payload.get("columns", [])
        dataset = payload.get("dataset", [])
        names = [
            column.get("name") if isinstance(column, Mapping) else str(column)
            for column in columns
        ]
        return [dict(zip(names, values)) for values in dataset]

    def _verify_schema(self, schema: QuestDBTableSchema) -> None:
        escaped_name = schema.name.replace("'", "''")
        table_rows = self._rows(
            self._execute_sql(
                "SELECT table_name, designatedTimestamp, partitionBy, "
                "walEnabled, dedup FROM tables() "
                f"WHERE table_name = '{escaped_name}'"
            )
        )
        if len(table_rows) != 1:
            raise ValueError(f"QuestDB table {schema.name!r} is unavailable")
        normalized_table = {
            str(key).lower(): value for key, value in table_rows[0].items()
        }
        if (
            str(normalized_table.get("designatedtimestamp", "")).lower() != "timestamp"
            or str(normalized_table.get("partitionby", "")).lower() != "day"
            or normalized_table.get("walenabled") is not True
            or normalized_table.get("dedup") is not True
        ):
            raise ValueError(f"QuestDB table {schema.name!r} has incompatible metadata")

        column_rows = self._rows(
            self._execute_sql(f"SELECT * FROM table_columns('{escaped_name}')")
        )
        actual: dict[str, tuple[str, bool, bool]] = {}
        for row in column_rows:
            normalized = {str(key).lower(): value for key, value in row.items()}
            name = normalized.get("column") or normalized.get("name")
            if name is None:
                continue
            actual[str(name)] = (
                str(normalized.get("type", "")).upper(),
                normalized.get("designated") is True,
                normalized.get("upsertkey") is True,
            )

        expected_types = dict(schema.columns)
        if {name: values[0] for name, values in actual.items()} != expected_types:
            raise ValueError(f"QuestDB table {schema.name!r} has incompatible columns")
        if not actual.get("timestamp", ("", False, False))[1]:
            raise ValueError(
                f"QuestDB table {schema.name!r} lacks its designated timestamp"
            )
        upsert_keys = {name for name, values in actual.items() if values[2]}
        if upsert_keys != {"timestamp", "run_id", "event_sequence"}:
            raise ValueError(
                f"QuestDB table {schema.name!r} has incompatible dedup keys"
            )

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._close_sync),
                    timeout=self._timeout_seconds,
                )
            except Exception as exc:
                raise MetricsSinkError("QuestDB metric close failed") from exc
            self._closed = True

    def _close_sync(self) -> None:
        with self._operation_lock:
            sender_error: Exception | None = None
            try:
                self._sender.close(flush=False)
            except Exception as exc:  # Preserve the first close failure.
                sender_error = exc
            try:
                self._http.close()
            except Exception:
                if sender_error is None:
                    raise
            if sender_error is not None:
                raise sender_error

    async def _run_with_timeout(
        self,
        operation,
        *args,
        message: str,
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(operation, *args),
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            self._schema_ready = False
            raise MetricsSinkError(message) from exc

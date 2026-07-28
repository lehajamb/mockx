"""Reliable process metrics, latency tracking, and buffered persistence."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
except ModuleNotFoundError:  # pragma: no cover - dependency is optional at import.
    CollectorRegistry = Counter = Gauge = Histogram = None
    generate_latest = None

from .latency import (
    MonotonicClock,
    PerfCounterClock,
    datetime_to_ns,
    monotonic_duration_ns,
)
from .models import (
    CancellationOutcome,
    CancelLatencyTrace,
    CancelOrderCommand,
    CancelReject,
    ExecutionReport,
    ExecutionType,
    FixDeliveryQueueSnapshot,
    FixDeliveryState,
    LatencyMeasurement,
    LiquidityCategory,
    MarketDataUpdate,
    MetricsSnapshot,
    NewOrderCommand,
    OrderLatencyTrace,
    OrderStatus,
    Trade,
    liquidity_category_for_session,
)


logger = logging.getLogger(__name__)


SIM_INBOUND_LATENCY = "mockx_sim_inbound_latency_seconds"
SIM_ENGINE_LATENCY = "mockx_sim_engine_latency_seconds"
SIM_OUTBOUND_LATENCY = "mockx_sim_outbound_latency_seconds"
SIM_END_TO_END_LATENCY = "mockx_sim_end_to_end_latency_seconds"
SIM_CANCEL_INBOUND_LATENCY = "mockx_sim_cancel_inbound_latency_seconds"
SIM_CANCEL_ENGINE_LATENCY = "mockx_sim_cancel_engine_latency_seconds"
SIM_CANCEL_OUTBOUND_LATENCY = "mockx_sim_cancel_outbound_latency_seconds"
SIM_CANCEL_END_TO_END_LATENCY = (
    "mockx_sim_cancel_end_to_end_latency_seconds"
)
REAL_MATCHING_DURATION = "mockx_real_matching_duration_seconds"
REAL_REPORT_SERIALIZATION_DURATION = (
    "mockx_real_report_serialization_duration_seconds"
)

LATENCY_METRICS = frozenset(
    {
        SIM_INBOUND_LATENCY,
        SIM_ENGINE_LATENCY,
        SIM_OUTBOUND_LATENCY,
        SIM_END_TO_END_LATENCY,
        SIM_CANCEL_INBOUND_LATENCY,
        SIM_CANCEL_ENGINE_LATENCY,
        SIM_CANCEL_OUTBOUND_LATENCY,
        SIM_CANCEL_END_TO_END_LATENCY,
        REAL_MATCHING_DURATION,
        REAL_REPORT_SERIALIZATION_DURATION,
    }
)

METRIC_IDENTITY_TAGS = frozenset(
    {"run_id", "event_type", "metric_event_bucket"}
)
METRIC_IDENTITY_FIELDS = frozenset(
    {"metric_event_sequence", "event_timestamp_ns"}
)
MAX_INFLUX_INTEGER = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class MetricRecord:
    """One immutable persistence record independent of an InfluxDB client."""

    measurement: str
    tags: Tuple[Tuple[str, str], ...]
    fields: Tuple[Tuple[str, Any], ...]
    event_timestamp_ns: int
    persistence_timestamp_ns: int

    @classmethod
    def create(
        cls,
        measurement: str,
        tags: Mapping[str, object],
        fields: Mapping[str, object],
        *,
        run_id: str,
        event_type: str,
        metric_event_sequence: int,
        event_timestamp_ns: int,
        persistence_timestamp_ns: int,
        metric_event_bucket: int,
    ) -> "MetricRecord":
        if not measurement:
            raise ValueError("Metric measurement is required")
        if not run_id:
            raise ValueError("Metric run_id is required")
        if not event_type:
            raise ValueError("Metric event_type is required")
        if (
            not isinstance(metric_event_sequence, int)
            or isinstance(metric_event_sequence, bool)
            or not 1 <= metric_event_sequence <= MAX_INFLUX_INTEGER
        ):
            raise ValueError(
                "metric_event_sequence must be a positive Influx integer"
            )
        if (
            not isinstance(metric_event_bucket, int)
            or isinstance(metric_event_bucket, bool)
            or metric_event_bucket < 0
        ):
            raise ValueError("metric_event_bucket cannot be negative")

        event_timestamp_ns = int(event_timestamp_ns)
        persistence_timestamp_ns = int(persistence_timestamp_ns)
        if (
            event_timestamp_ns < 0
            or persistence_timestamp_ns > MAX_INFLUX_INTEGER
        ):
            raise ValueError(
                "Metric timestamps must fit the non-negative Influx range"
            )
        if event_timestamp_ns % 1_000:
            raise ValueError(
                "Metric event timestamps must be microsecond-aligned"
            )
        timestamp_offset = persistence_timestamp_ns - event_timestamp_ns
        if not 0 <= timestamp_offset < 1_000:
            raise ValueError(
                "Metric persistence timestamp must remain within the "
                "original simulated microsecond"
            )

        reserved_tags = METRIC_IDENTITY_TAGS.intersection(tags)
        if reserved_tags:
            raise ValueError(
                "Metric domain tags use reserved identity names: "
                + ", ".join(sorted(reserved_tags))
            )
        reserved_fields = METRIC_IDENTITY_FIELDS.intersection(fields)
        if reserved_fields:
            raise ValueError(
                "Metric domain fields use reserved identity names: "
                + ", ".join(sorted(reserved_fields))
            )

        record_tags = dict(tags)
        record_tags.update(
            run_id=run_id,
            event_type=event_type,
            metric_event_bucket=metric_event_bucket,
        )
        record_fields = dict(fields)
        record_fields.update(
            metric_event_sequence=metric_event_sequence,
            event_timestamp_ns=event_timestamp_ns,
        )
        return cls(
            measurement=measurement,
            tags=tuple(
                sorted(
                    (key, str(value))
                    for key, value in record_tags.items()
                )
            ),
            fields=tuple(sorted(record_fields.items())),
            event_timestamp_ns=event_timestamp_ns,
            persistence_timestamp_ns=persistence_timestamp_ns,
        )

    @property
    def tag_map(self) -> Dict[str, str]:
        return dict(self.tags)

    @property
    def field_map(self) -> Dict[str, Any]:
        return dict(self.fields)

    @property
    def metric_event_sequence(self) -> int:
        return int(self.field_map["metric_event_sequence"])

    @property
    def run_id(self) -> str:
        return self.tag_map["run_id"]


class MetricsSinkError(RuntimeError):
    """Expected persistence-adapter failure."""


class MetricsSink(Protocol):
    """Asynchronous persistence boundary used by the buffered writer."""

    async def write(self, records: Sequence[MetricRecord]) -> None:
        ...

    async def flush(self) -> None:
        ...

    async def close(self) -> None:
        ...


class NoOpMetricsSink:
    """Sink used when external metric persistence is disabled."""

    async def write(self, records: Sequence[MetricRecord]) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None


class InMemoryMetricsSink:
    """Deterministic sink for tests and local inspection."""

    def __init__(self) -> None:
        self.writes: list[Tuple[MetricRecord, ...]] = []
        self.flush_calls = 0
        self.close_calls = 0
        self.closed = False

    async def write(self, records: Sequence[MetricRecord]) -> None:
        if self.closed:
            raise MetricsSinkError("In-memory metrics sink is closed")
        self.writes.append(tuple(records))

    async def flush(self) -> None:
        self.flush_calls += 1

    async def close(self) -> None:
        if not self.closed:
            self.close_calls += 1
            self.closed = True


class BufferedMetricsWriter:
    """Loss-aware buffer in front of an asynchronous metric sink.

    Producers only hold the short-lived buffer lock.  A separate async lock
    serializes flushes, and no lock is held while awaiting the sink.  Failed
    batches are restored ahead of records added while the write was in flight.
    """

    def __init__(
        self,
        sink: MetricsSink,
        *,
        batch_size: int = 1000,
        on_failure: Optional[Callable[[BaseException, int], None]] = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("Metrics batch_size must be positive")
        self.sink = sink
        self.batch_size = batch_size
        self.on_failure = on_failure
        self._buffer: Deque[MetricRecord] = deque()
        self._buffer_lock = threading.RLock()
        self._flush_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._closing = False
        self._sink_closed = False
        self.failure_count = 0
        self._background_tasks: set[asyncio.Task[bool]] = set()

    @property
    def buffered_count(self) -> int:
        with self._buffer_lock:
            return len(self._buffer)

    @property
    def closed(self) -> bool:
        with self._buffer_lock:
            return self._closed

    def _append(self, record: MetricRecord) -> bool:
        with self._buffer_lock:
            if self._closed or self._closing:
                logger.warning(
                    "Metric record rejected while writer is closing",
                    extra={
                        "metric_sink": type(self.sink).__name__,
                        "buffer_size": len(self._buffer),
                    },
                )
                return False
            self._buffer.append(record)
            return len(self._buffer) >= self.batch_size

    def add_nowait(self, record: MetricRecord) -> bool:
        """Buffer a record and schedule a threshold flush when a loop exists."""
        threshold_reached = self._append(record)
        if not threshold_reached:
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous engine callers remain nonblocking.  The next explicit
            # flush or shutdown will persist the threshold-sized batch.
            return True

        task = loop.create_task(self.flush())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True

    async def add(self, record: MetricRecord) -> bool:
        """Buffer one record, flushing synchronously at the batch threshold."""
        threshold_reached = self._append(record)
        if threshold_reached:
            return await self.flush()
        return True

    async def flush(self) -> bool:
        """Persist the current batch once, restoring it on write failure."""
        async with self._flush_lock:
            with self._buffer_lock:
                if not self._buffer:
                    return True
                batch = tuple(self._buffer)
                self._buffer.clear()

            try:
                await self.sink.write(batch)
            except Exception as exc:
                with self._buffer_lock:
                    newer_records = tuple(self._buffer)
                    self._buffer = deque((*batch, *newer_records))
                    buffer_size = len(self._buffer)
                self._record_failure(exc, buffer_size)
                logger.error(
                    "Metrics flush failed; batch retained for retry",
                    extra={
                        "metric_sink": type(self.sink).__name__,
                        "buffer_size": buffer_size,
                    },
                    exc_info=True,
                )
                return False
            return True

    async def close(self) -> bool:
        """Flush and close once; a failed final write leaves the writer retryable."""
        async with self._close_lock:
            if self._closed:
                return True
            with self._buffer_lock:
                self._closing = True
            if not await self.flush():
                with self._buffer_lock:
                    self._closing = False
                return False
            try:
                await self.sink.flush()
                if not self._sink_closed:
                    await self.sink.close()
                    self._sink_closed = True
            except Exception as exc:
                self._record_failure(exc, self.buffered_count)
                logger.error(
                    "Metrics sink close failed",
                    extra={
                        "metric_sink": type(self.sink).__name__,
                        "buffer_size": self.buffered_count,
                    },
                    exc_info=True,
                )
                with self._buffer_lock:
                    self._closing = False
                return False
            with self._buffer_lock:
                self._closed = True
                self._closing = False
            return True

    async def replace_sink(self, sink: MetricsSink) -> bool:
        """Flush and close the current sink before replacing it."""
        async with self._close_lock:
            if self._closed:
                raise RuntimeError("Cannot replace a sink on a closed writer")
            if not await self.flush():
                return False
            try:
                await self.sink.flush()
                await self.sink.close()
            except Exception as exc:
                self._record_failure(exc, self.buffered_count)
                logger.error(
                    "Metrics sink replacement failed",
                    extra={
                        "metric_sink": type(self.sink).__name__,
                        "buffer_size": self.buffered_count,
                    },
                    exc_info=True,
                )
                return False
            self.sink = sink
            self._sink_closed = False
            return True

    def _record_failure(self, exc: BaseException, buffer_size: int) -> None:
        self.failure_count += 1
        if self.on_failure is not None:
            try:
                self.on_failure(exc, buffer_size)
            except Exception:
                logger.exception("Metrics failure observer failed")

    async def __aenter__(self) -> "BufferedMetricsWriter":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()


class MetricsCollector:
    """Thread-safe event-owned process metrics with an isolated registry."""

    def __init__(
        self,
        exchange_name: str,
        *,
        persistence: Optional[BufferedMetricsWriter] = None,
        prometheus_enabled: bool = True,
    ) -> None:
        self.exchange_name = exchange_name
        self.persistence = persistence
        self._run_id: Optional[str] = None
        self._metric_event_sequence = 0
        self._last_event_timestamp_ns: Optional[int] = None
        self._same_timestamp_ordinal = 0
        self._lock = threading.RLock()
        self._engine = None
        self._counts: Dict[str, int] = {
            "orders_submitted": 0,
            "orders_accepted": 0,
            "orders_rejected": 0,
            "orders_canceled": 0,
            "orders_filled": 0,
            "partial_fill_events": 0,
            "cancel_requests": 0,
            "cancel_rejects": 0,
            "cancel_accepted": 0,
            "cancel_unknown_order": 0,
            "cancel_already_terminal": 0,
            "cancel_wrong_owner": 0,
            "cancel_duplicate_request": 0,
            "cancel_stale_run_generation": 0,
            "execution_reports": 0,
            "report_delivery_failures": 0,
            "connected_sessions": 0,
            "trades": 0,
            "invalid_latency_measurements": 0,
            "metric_sink_failures": 0,
        }
        self._totals: Dict[str, float] = {
            "traded_quantity": 0.0,
            "traded_notional": 0.0,
            "canceled_quantity": 0.0,
        }
        self._latency_sums = {name: 0.0 for name in LATENCY_METRICS}
        self._latency_counts = {name: 0 for name in LATENCY_METRICS}
        self._submitted_order_ids: set[int] = set()
        self._accepted_order_ids: set[int] = set()
        self._filled_order_ids: set[int] = set()
        self._seen_report_exec_ids: set[str] = set()
        self._seen_cancel_reject_ids: set[str] = set()
        self._seen_trade_ids: set[str] = set()
        self._websocket_active_clients = 0
        self._websocket_queue_depth = 0
        self._websocket_counts: Dict[str, int] = {
            "websocket_snapshots_conflated": 0,
            "websocket_snapshots_dropped": 0,
            "websocket_slow_client_disconnects": 0,
            "websocket_send_failures": 0,
            "websocket_send_successes": 0,
        }

        self.registry = None
        self._prom_counters: Dict[str, Any] = {}
        self._prom_gauges: Dict[str, Any] = {}
        self._prom_histograms: Dict[str, Any] = {}
        self._prom_delivery_state_gauge = None
        if prometheus_enabled and CollectorRegistry is not None:
            self._init_prometheus()

        if self.persistence is not None:
            self.persistence.on_failure = self.record_sink_failure

    @property
    def run_id(self) -> Optional[str]:
        with self._lock:
            return self._run_id

    def start_run(self, run_id: str) -> None:
        """Start one persistence identity scope and reset its sequence."""
        if not run_id:
            raise ValueError("Metric run_id is required")
        with self._lock:
            if self._run_id is not None:
                raise RuntimeError("A metric persistence run is already active")
            self._run_id = run_id
            self._metric_event_sequence = 0
            self._last_event_timestamp_ns = None
            self._same_timestamp_ordinal = 0

    def end_run(self) -> None:
        """Clear the active run after its immutable records have flushed."""
        with self._lock:
            self._run_id = None

    def bind_engine(self, engine: object) -> None:
        """Bind authoritative gauge derivation without making metrics stateful."""
        self._engine = engine
        self.refresh_engine_gauges()

    def reset_run_metrics(self) -> None:
        """Reset run-local statistics without resetting process collectors."""
        with self._lock:
            connected_sessions = self._counts["connected_sessions"]
            for name in self._counts:
                self._counts[name] = 0
            self._counts["connected_sessions"] = connected_sessions
            for name in self._totals:
                self._totals[name] = 0.0
            for name in self._latency_sums:
                self._latency_sums[name] = 0.0
                self._latency_counts[name] = 0
            self._submitted_order_ids.clear()
            self._accepted_order_ids.clear()
            self._filled_order_ids.clear()
            self._seen_report_exec_ids.clear()
            self._seen_cancel_reject_ids.clear()
            self._seen_trade_ids.clear()

    def attach_persistence(self, persistence: BufferedMetricsWriter) -> None:
        self.persistence = persistence
        persistence.on_failure = self.record_sink_failure

    def _init_prometheus(self) -> None:
        self.registry = CollectorRegistry()
        counter_help = {
            "orders_submitted": "Commands accepted into engine ownership.",
            "orders_accepted": "Orders accepted for matching.",
            "orders_rejected": "Order rejection reports created.",
            "orders_canceled": "Successful terminal cancellations committed.",
            "orders_filled": "Unique orders reaching terminal FILLED.",
            "partial_fill_events": "Order-specific PARTIAL trade reports created.",
            "cancel_requests": "Cancel commands received by the engine.",
            "cancel_rejects": "Cancel rejection snapshots created.",
            "cancel_accepted": "Cancel requests accepted.",
            "cancel_unknown_order": "Cancels rejected for an unknown order.",
            "cancel_already_terminal": "Cancels rejected after terminal state.",
            "cancel_wrong_owner": "Cancels rejected for wrong FIX ownership.",
            "cancel_duplicate_request": "Duplicate cancel ClOrdIDs rejected.",
            "cancel_stale_run_generation": "Stale-generation cancels rejected.",
            "execution_reports": "Order-specific execution reports created.",
            "report_delivery_failures": "Reports not delivered to their destination.",
            "trades": "Immutable fills committed by the engine.",
            "traded_quantity": "Quantity traded once per fill.",
            "traded_notional": "Price times quantity summed once per fill.",
            "canceled_quantity": "Unexecuted quantity canceled successfully.",
            "invalid_latency_measurements": "Latency samples rejected as invalid.",
            "metric_sink_failures": "Metric persistence operations that failed.",
        }
        for name, help_text in counter_help.items():
            prometheus_name = (
                "mockx_execution_report_delivery_failures_total"
                if name == "report_delivery_failures"
                else f"mockx_{name}_total"
            )
            self._prom_counters[name] = Counter(
                prometheus_name,
                help_text,
                registry=self.registry,
            )

        for name, help_text in {
            "connected_sessions": "Currently connected FIX sessions.",
            "active_orders": "Current nonterminal orders.",
            "resting_orders": "Current orders physically resting in books.",
            "order_book_levels": "Current non-empty order book levels.",
        }.items():
            self._prom_gauges[name] = Gauge(
                f"mockx_{name}",
                help_text,
                registry=self.registry,
            )

        for name, metric_name, help_text in (
            (
                "fix_delivery_enqueued",
                "mockx_fix_delivery_enqueued_total",
                "Logical application FIX deliveries enqueued.",
            ),
            (
                "fix_delivery_attempts",
                "mockx_fix_delivery_attempts_total",
                "Transport sender invocations for FIX deliveries.",
            ),
            (
                "fix_delivery_failures",
                "mockx_fix_delivery_failures_total",
                "Disconnected checks and failed FIX delivery attempts.",
            ),
            (
                "fix_delivery_successes",
                "mockx_fix_delivery_successes_total",
                "FIX deliveries successfully submitted to QuickFIX.",
            ),
            (
                "fix_delivery_permanent_failures",
                "mockx_fix_delivery_permanent_failures_total",
                "Deterministic FIX serialization failures.",
            ),
        ):
            self._prom_counters[name] = Counter(
                metric_name,
                help_text,
                registry=self.registry,
            )
        self._prom_gauges["fix_delivery_queue_depth"] = Gauge(
            "mockx_fix_delivery_queue_depth",
            "Non-delivered application FIX records retained in memory.",
            registry=self.registry,
        )
        self._prom_gauges["fix_delivery_oldest_pending_age_seconds"] = Gauge(
            "mockx_fix_delivery_oldest_pending_age_seconds",
            "Wall-clock age of the oldest retained FIX delivery.",
            registry=self.registry,
        )
        self._prom_delivery_state_gauge = Gauge(
            "mockx_fix_delivery_records",
            "Process-lifetime FIX delivery records by state.",
            ("state",),
            registry=self.registry,
        )
        for name, metric_name, help_text in (
            (
                "websocket_snapshots_conflated",
                "mockx_websocket_snapshots_conflated_total",
                "Queued WebSocket snapshots replaced by a newer full snapshot.",
            ),
            (
                "websocket_snapshots_dropped",
                "mockx_websocket_snapshots_dropped_total",
                "Incoming WebSocket snapshots dropped as obsolete or full.",
            ),
            (
                "websocket_slow_client_disconnects",
                "mockx_websocket_slow_client_disconnects_total",
                "WebSocket clients disconnected after a send timeout.",
            ),
            (
                "websocket_send_failures",
                "mockx_websocket_send_failures_total",
                "WebSocket JSON send attempts that failed or timed out.",
            ),
            (
                "websocket_send_successes",
                "mockx_websocket_send_successes_total",
                "WebSocket JSON frames sent successfully.",
            ),
        ):
            self._prom_counters[name] = Counter(
                metric_name,
                help_text,
                registry=self.registry,
            )
        self._prom_gauges["websocket_active_clients"] = Gauge(
            "mockx_websocket_active_clients",
            "Currently active valid-symbol WebSocket subscriptions.",
            registry=self.registry,
        )
        self._prom_gauges["websocket_outbound_queue_depth"] = Gauge(
            "mockx_websocket_outbound_queue_depth",
            "Aggregate queued WebSocket frames, excluding in-flight sends.",
            registry=self.registry,
        )

        duration_help = {
            SIM_INBOUND_LATENCY: (
                "Simulated engine receive time minus client submit time."
            ),
            SIM_ENGINE_LATENCY: (
                "Simulated first report creation time minus engine receive time."
            ),
            SIM_OUTBOUND_LATENCY: (
                "Simulated report delivery time minus report creation time."
            ),
            SIM_END_TO_END_LATENCY: (
                "Simulated report delivery time minus client submit time."
            ),
            SIM_CANCEL_INBOUND_LATENCY: (
                "Simulated cancel exchange arrival minus FIX receive time."
            ),
            SIM_CANCEL_ENGINE_LATENCY: (
                "Simulated cancel report creation minus exchange arrival time."
            ),
            SIM_CANCEL_OUTBOUND_LATENCY: (
                "Simulated cancel report delivery minus report creation time."
            ),
            SIM_CANCEL_END_TO_END_LATENCY: (
                "Simulated cancel report delivery minus FIX receive time."
            ),
            REAL_MATCHING_DURATION: (
                "Real monotonic duration of the matching function."
            ),
            REAL_REPORT_SERIALIZATION_DURATION: (
                "Real monotonic duration of FIX report serialization."
            ),
        }
        for metric_name, help_text in duration_help.items():
            self._prom_histograms[metric_name] = Histogram(
                metric_name,
                help_text,
                registry=self.registry,
                buckets=(
                    0.000001,
                    0.000005,
                    0.00001,
                    0.00005,
                    0.0001,
                    0.0005,
                    0.001,
                    0.005,
                    0.01,
                    0.05,
                    0.1,
                    0.5,
                    1.0,
                ),
            )

    def record_order_submitted(
        self,
        order_id: int,
        command: NewOrderCommand,
    ) -> bool:
        """Own the submission counter exactly once per engine order ID."""
        with self._lock:
            if order_id in self._submitted_order_ids:
                return False
            self._submitted_order_ids.add(order_id)
            self._increment_locked("orders_submitted")
        self.refresh_engine_gauges()
        return True

    def record_order(self, report: ExecutionReport) -> bool:
        """Record one immutable execution-report creation snapshot."""
        fresh = self.record_execution_report(report)
        if not fresh:
            return False

        tags: Dict[str, object] = {
            "exchange": self.exchange_name,
            "symbol": report.symbol,
            "side": report.side.value,
            "status": report.ord_status.value,
            "tif": report.tif.value,
        }
        if report.session_alias:
            tags["session_alias"] = report.session_alias
        fields: Dict[str, object] = {
            "order_id": report.order_id,
            "exec_id": report.exec_id,
            "qty": report.order_qty,
            "filled_qty": report.cum_qty,
            "canceled_qty": report.canceled_qty,
            "leaves_qty": report.leaves_qty,
            "avg_fill_price": report.avg_px,
            "ts_submitted": datetime_to_ns(report.submitted_at),
        }
        if report.exchange_order_id is not None:
            fields["exchange_order_id"] = report.exchange_order_id
        if report.trade_id is not None:
            fields["trade_id"] = report.trade_id
        if report.exchange_trade_id is not None:
            fields["exchange_trade_id"] = report.exchange_trade_id
        if report.order_price is not None:
            fields["price"] = report.order_price
        self._create_point(
            "orders",
            tags,
            fields,
            datetime_to_ns(report.timestamp),
        )
        return True

    def record_execution_report(self, report: ExecutionReport) -> bool:
        """Update report and order lifecycle counters at report creation."""
        with self._lock:
            if report.exec_id in self._seen_report_exec_ids:
                return False
            self._seen_report_exec_ids.add(report.exec_id)
            self._increment_locked("execution_reports")

            if report.exec_type == ExecutionType.REJECTED:
                self._increment_locked("orders_rejected")
            elif (
                report.order_id > 0
                and report.order_id not in self._accepted_order_ids
            ):
                self._accepted_order_ids.add(report.order_id)
                self._increment_locked("orders_accepted")

            if report.exec_type == ExecutionType.CANCELED:
                self._increment_locked("orders_canceled")
                self._totals["canceled_quantity"] += report.canceled_qty
                self._prom_increment("canceled_quantity", report.canceled_qty)
            elif (
                report.exec_type == ExecutionType.TRADE
                and report.ord_status == OrderStatus.PARTIAL
            ):
                self._increment_locked("partial_fill_events")

            if (
                report.ord_status == OrderStatus.FILLED
                and report.order_id not in self._filled_order_ids
            ):
                self._filled_order_ids.add(report.order_id)
                self._increment_locked("orders_filled")
        self.refresh_engine_gauges()
        return True

    def record_trade(self, trade: Trade) -> bool:
        """Count each immutable fill/trade record exactly once."""
        identity = (
            trade.exchange_trade_id
            or f"G{trade.run_generation:08d}-T{trade.id:08d}"
        )
        with self._lock:
            if identity in self._seen_trade_ids:
                return False
            self._seen_trade_ids.add(identity)
            self._increment_locked("trades")
            self._totals["traded_quantity"] += trade.qty
            self._totals["traded_notional"] += trade.price * trade.qty
            self._prom_increment("traded_quantity", trade.qty)
            self._prom_increment(
                "traded_notional",
                trade.price * trade.qty,
            )

        tags: Dict[str, object] = {
            "exchange": self.exchange_name,
            "symbol": trade.symbol,
            "buyer_session_alias": trade.buyer_session_alias or "unaliased",
            "seller_session_alias": trade.seller_session_alias or "unaliased",
            "aggressor_side": trade.aggressor_side.value,
        }
        if (
            liquidity_category_for_session(trade.buyer_session_alias)
            == LiquidityCategory.STRATEGY
        ):
            tags.update(strategy_side="buy", has_strategy="true")
        elif (
            liquidity_category_for_session(trade.seller_session_alias)
            == LiquidityCategory.STRATEGY
        ):
            tags.update(strategy_side="sell", has_strategy="true")
        else:
            tags.update(strategy_side="none", has_strategy="false")
        self._create_point(
            "trades",
            tags,
            {
                "trade_id": trade.id,
                "exchange_trade_id": identity,
                "price": trade.price,
                "qty": trade.qty,
                "volume": trade.volume,
                "buyer_order_id": trade.buyer_order_id,
                "seller_order_id": trade.seller_order_id,
                "aggressor_order_id": trade.aggressor_order_id,
                "passive_order_id": trade.passive_order_id,
                "level": trade.level,
            },
            datetime_to_ns(trade.timestamp),
        )
        self.refresh_engine_gauges()
        return True

    def record_cancel_request(self, command: CancelOrderCommand) -> None:
        with self._lock:
            self._increment_locked("cancel_requests")

    def record_cancel_reject(self, reject: CancelReject) -> bool:
        with self._lock:
            if reject.reject_id in self._seen_cancel_reject_ids:
                return False
            self._seen_cancel_reject_ids.add(reject.reject_id)
            self._increment_locked("cancel_rejects")
            return True

    def record_cancel_outcome(
        self,
        command: CancelOrderCommand,
        outcome: CancellationOutcome,
    ) -> None:
        """Increment exactly one fixed cancel outcome counter."""
        name = {
            CancellationOutcome.ACCEPTED: "cancel_accepted",
            CancellationOutcome.UNKNOWN_ORDER: "cancel_unknown_order",
            CancellationOutcome.ALREADY_TERMINAL: "cancel_already_terminal",
            CancellationOutcome.WRONG_OWNER: "cancel_wrong_owner",
            CancellationOutcome.DUPLICATE_REQUEST: "cancel_duplicate_request",
            CancellationOutcome.STALE_RUN_GENERATION: (
                "cancel_stale_run_generation"
            ),
        }[outcome]
        with self._lock:
            self._increment_locked(name)

    def record_report_delivery_failure(
        self,
        report: ExecutionReport | CancelReject,
    ) -> None:
        with self._lock:
            self._increment_locked("report_delivery_failures")

    def set_connected_sessions(self, count: int) -> None:
        if count < 0:
            raise ValueError("Connected session count cannot be negative")
        with self._lock:
            self._counts["connected_sessions"] = count
            gauge = self._prom_gauges.get("connected_sessions")
            if gauge is not None:
                gauge.set(count)

    def record_fix_delivery_enqueued(self) -> None:
        self._prom_increment("fix_delivery_enqueued", 1)

    def record_fix_delivery_attempt(self) -> None:
        self._prom_increment("fix_delivery_attempts", 1)

    def record_fix_delivery_failure(self, *, permanent: bool = False) -> None:
        self._prom_increment("fix_delivery_failures", 1)
        if permanent:
            self._prom_increment("fix_delivery_permanent_failures", 1)

    def record_fix_delivery_success(self) -> None:
        self._prom_increment("fix_delivery_successes", 1)

    def update_fix_delivery_gauges(
        self,
        snapshot: FixDeliveryQueueSnapshot,
    ) -> None:
        depth = self._prom_gauges.get("fix_delivery_queue_depth")
        if depth is not None:
            depth.set(snapshot.queue_depth)
        age = self._prom_gauges.get(
            "fix_delivery_oldest_pending_age_seconds"
        )
        if age is not None:
            age.set(snapshot.oldest_pending_age_seconds or 0.0)
        if self._prom_delivery_state_gauge is not None:
            state_counts = {
                FixDeliveryState.PENDING: snapshot.pending,
                FixDeliveryState.DUE: snapshot.due,
                FixDeliveryState.RETRY_PENDING: snapshot.retry_pending,
                FixDeliveryState.DELIVERED: snapshot.delivered,
                FixDeliveryState.PERMANENTLY_FAILED: (
                    snapshot.permanently_failed
                ),
            }
            for state, count in state_counts.items():
                self._prom_delivery_state_gauge.labels(
                    state=state.value
                ).set(count)

    def record_invalid_latency(
        self,
        *,
        metric: str,
        order_id: Optional[int],
        session_id: Optional[str],
    ) -> None:
        with self._lock:
            self._increment_locked("invalid_latency_measurements")
        logger.warning(
            "Invalid latency timestamps rejected",
            extra={
                "order_id": order_id,
                "session_id": session_id,
                "latency_metric": metric,
            },
        )

    def observe_latency(self, measurement: LatencyMeasurement) -> bool:
        """Publish one already-validated immutable latency measurement."""
        if measurement.metric not in LATENCY_METRICS:
            raise ValueError(f"Unsupported latency metric: {measurement.metric}")
        if (
            measurement.unit != "seconds"
            or not math.isfinite(measurement.value)
            or measurement.value < 0.0
        ):
            self.record_invalid_latency(
                metric=measurement.metric,
                order_id=measurement.order_id,
                session_id=measurement.session_id,
            )
            return False

        with self._lock:
            self._latency_sums[measurement.metric] += measurement.value
            self._latency_counts[measurement.metric] += 1
            histogram = self._prom_histograms.get(measurement.metric)
            if histogram is not None:
                histogram.observe(measurement.value)
        fields: Dict[str, object] = {
            "duration_seconds": measurement.value,
        }
        if measurement.order_id is not None:
            fields["order_id"] = measurement.order_id
        if measurement.session_id is not None:
            fields["session_id"] = measurement.session_id
        if measurement.report_exec_id is not None:
            fields["report_exec_id"] = measurement.report_exec_id
        if measurement.run_generation is not None:
            fields["run_generation"] = measurement.run_generation
        if measurement.ingress_sequence is not None:
            fields["ingress_sequence"] = measurement.ingress_sequence
        if measurement.cl_ord_id is not None:
            fields["cl_ord_id"] = measurement.cl_ord_id
        if measurement.orig_cl_ord_id is not None:
            fields["orig_cl_ord_id"] = measurement.orig_cl_ord_id
        self._create_point(
            "latency",
            {
                "exchange": self.exchange_name,
                "type": measurement.metric,
            },
            fields,
            datetime_to_ns(measurement.timestamp),
        )
        return True

    def record_sink_failure(
        self,
        exc: BaseException,
        buffer_size: int,
    ) -> None:
        with self._lock:
            self._increment_locked("metric_sink_failures")
        logger.error(
            "Metrics sink operation failed",
            extra={
                "metric_sink": (
                    type(self.persistence.sink).__name__
                    if self.persistence is not None
                    else "unknown"
                ),
                "buffer_size": buffer_size,
            },
        )

    def websocket_client_connected(self) -> None:
        with self._lock:
            self._websocket_active_clients += 1
            gauge = self._prom_gauges.get("websocket_active_clients")
            if gauge is not None:
                gauge.set(self._websocket_active_clients)

    def websocket_client_disconnected(self) -> None:
        with self._lock:
            self._websocket_active_clients = max(
                0,
                self._websocket_active_clients - 1,
            )
            gauge = self._prom_gauges.get("websocket_active_clients")
            if gauge is not None:
                gauge.set(self._websocket_active_clients)

    def adjust_websocket_queue_depth(self, delta: int) -> None:
        with self._lock:
            self._websocket_queue_depth = max(
                0,
                self._websocket_queue_depth + delta,
            )
            gauge = self._prom_gauges.get("websocket_outbound_queue_depth")
            if gauge is not None:
                gauge.set(self._websocket_queue_depth)

    def _record_websocket_count(self, name: str) -> None:
        with self._lock:
            self._websocket_counts[name] += 1
            self._prom_increment(name, 1)

    def record_websocket_snapshot_conflated(self) -> None:
        self._record_websocket_count("websocket_snapshots_conflated")

    def record_websocket_snapshot_dropped(self) -> None:
        self._record_websocket_count("websocket_snapshots_dropped")

    def record_websocket_slow_client_disconnect(self) -> None:
        self._record_websocket_count("websocket_slow_client_disconnects")

    def record_websocket_send_failure(self) -> None:
        self._record_websocket_count("websocket_send_failures")

    def record_websocket_send_success(self) -> None:
        self._record_websocket_count("websocket_send_successes")

    def websocket_metrics_snapshot(self) -> Dict[str, int]:
        """Return internal process-lifetime WebSocket metrics for tests."""
        with self._lock:
            return {
                "active_clients": self._websocket_active_clients,
                "queue_depth": self._websocket_queue_depth,
                "snapshots_conflated": self._websocket_counts[
                    "websocket_snapshots_conflated"
                ],
                "snapshots_dropped": self._websocket_counts[
                    "websocket_snapshots_dropped"
                ],
                "slow_client_disconnects": self._websocket_counts[
                    "websocket_slow_client_disconnects"
                ],
                "send_failures": self._websocket_counts[
                    "websocket_send_failures"
                ],
                "send_successes": self._websocket_counts[
                    "websocket_send_successes"
                ],
            }

    def record_fix_message(
        self,
        msg_type: str,
        direction: str,
        timestamp_ns: int,
    ) -> None:
        self._create_point(
            "fix_messages",
            {
                "exchange": self.exchange_name,
                "msg_type": msg_type,
                "direction": direction,
            },
            {"count": 1},
            timestamp_ns,
        )

    def record_fix_connection(self, count: int, timestamp_ns: int) -> None:
        """Compatibility method; callers should pass the absolute count."""
        self.set_connected_sessions(max(0, count))
        self._create_point(
            "fix_connections",
            {"exchange": self.exchange_name},
            {"count": max(0, count)},
            timestamp_ns,
        )

    def update_book_metrics(
        self,
        update: MarketDataUpdate,
        price_levels: int = 15,
        tick_size: float = 1.0,
    ) -> None:
        if not update.bid_levels or not update.ask_levels:
            self.refresh_engine_gauges()
            return
        timestamp_ns = datetime_to_ns(update.timestamp)
        best_bid = update.bid_levels[0].price
        best_ask = update.ask_levels[0].price
        fields: Dict[str, object] = {
            "spread": best_ask - best_bid,
            "mid_price": (best_bid + best_ask) / 2,
            "best_bid": best_bid,
            "best_ask": best_ask,
        }
        self._create_point(
            "book",
            {"exchange": self.exchange_name, "symbol": update.symbol},
            fields,
            timestamp_ns,
        )
        self.refresh_engine_gauges()

    def update_active_orders(self, update: MarketDataUpdate) -> None:
        """Compatibility hook; gauges are derived from the engine."""
        self.refresh_engine_gauges()

    def update_pending_events(self, count: int, timestamp_ns: int) -> None:
        self._create_point(
            "pending_events",
            {"exchange": self.exchange_name},
            {"count": count},
            timestamp_ns,
        )

    def refresh_engine_gauges(self) -> Tuple[int, int, int]:
        active, resting, levels = self._authoritative_engine_counts()
        for name, value in (
            ("active_orders", active),
            ("resting_orders", resting),
            ("order_book_levels", levels),
        ):
            gauge = self._prom_gauges.get(name)
            if gauge is not None:
                gauge.set(value)
        return active, resting, levels

    def _authoritative_engine_counts(self) -> Tuple[int, int, int]:
        engine = self._engine
        if engine is None:
            return 0, 0, 0
        return (
            int(engine.active_order_count()),
            int(engine.resting_order_count()),
            int(engine.order_book_level_count()),
        )

    def snapshot(self) -> MetricsSnapshot:
        """Create a non-mutating immutable snapshot."""
        active, resting, levels = self._authoritative_engine_counts()
        with self._lock:
            counts = dict(self._counts)
            totals = dict(self._totals)
            latency_sums = dict(self._latency_sums)
            latency_counts = dict(self._latency_counts)
        buffered = (
            self.persistence.buffered_count
            if self.persistence is not None
            else 0
        )

        def average(metric: str, multiplier: float) -> Optional[float]:
            count = latency_counts[metric]
            if count == 0:
                return None
            return latency_sums[metric] / count * multiplier

        return MetricsSnapshot(
            generated_at=datetime.now(timezone.utc),
            orders_submitted=counts["orders_submitted"],
            orders_accepted=counts["orders_accepted"],
            orders_rejected=counts["orders_rejected"],
            orders_canceled=counts["orders_canceled"],
            orders_filled=counts["orders_filled"],
            partial_fill_events=counts["partial_fill_events"],
            cancel_requests=counts["cancel_requests"],
            cancel_rejects=counts["cancel_rejects"],
            cancel_accepted=counts["cancel_accepted"],
            cancel_unknown_order=counts["cancel_unknown_order"],
            cancel_already_terminal=counts["cancel_already_terminal"],
            cancel_wrong_owner=counts["cancel_wrong_owner"],
            cancel_duplicate_request=counts["cancel_duplicate_request"],
            cancel_stale_run_generation=counts[
                "cancel_stale_run_generation"
            ],
            execution_reports=counts["execution_reports"],
            trades=counts["trades"],
            traded_quantity=totals["traded_quantity"],
            traded_notional=totals["traded_notional"],
            canceled_quantity=totals["canceled_quantity"],
            active_orders=active,
            resting_orders=resting,
            order_book_levels=levels,
            connected_sessions=counts["connected_sessions"],
            average_real_matching_duration_us=average(
                REAL_MATCHING_DURATION,
                1_000_000.0,
            ),
            average_real_report_serialization_duration_us=average(
                REAL_REPORT_SERIALIZATION_DURATION,
                1_000_000.0,
            ),
            average_sim_inbound_latency_ms=average(
                SIM_INBOUND_LATENCY,
                1_000.0,
            ),
            average_sim_engine_latency_ms=average(
                SIM_ENGINE_LATENCY,
                1_000.0,
            ),
            average_sim_outbound_latency_ms=average(
                SIM_OUTBOUND_LATENCY,
                1_000.0,
            ),
            average_sim_end_to_end_latency_ms=average(
                SIM_END_TO_END_LATENCY,
                1_000.0,
            ),
            average_sim_cancel_inbound_latency_ms=average(
                SIM_CANCEL_INBOUND_LATENCY,
                1_000.0,
            ),
            average_sim_cancel_engine_latency_ms=average(
                SIM_CANCEL_ENGINE_LATENCY,
                1_000.0,
            ),
            average_sim_cancel_outbound_latency_ms=average(
                SIM_CANCEL_OUTBOUND_LATENCY,
                1_000.0,
            ),
            average_sim_cancel_end_to_end_latency_ms=average(
                SIM_CANCEL_END_TO_END_LATENCY,
                1_000.0,
            ),
            report_delivery_failures=counts["report_delivery_failures"],
            invalid_latency_measurements=counts[
                "invalid_latency_measurements"
            ],
            metric_sink_failures=counts["metric_sink_failures"],
            buffered_metric_records=buffered,
        )

    def prometheus_payload(self) -> bytes:
        if self.registry is None or generate_latest is None:
            raise RuntimeError("Prometheus support is disabled")
        return generate_latest(self.registry)

    def get_orders_total(self) -> int:
        return self.snapshot().orders_submitted

    def get_trades_total(self) -> int:
        return self.snapshot().trades

    def get_volume_total(self) -> float:
        return self.snapshot().traded_quantity

    def _increment_locked(self, name: str, amount: int = 1) -> None:
        self._counts[name] += amount
        self._prom_increment(name, amount)

    def _prom_increment(self, name: str, amount: float) -> None:
        counter = self._prom_counters.get(name)
        if counter is not None:
            counter.inc(amount)

    def _create_point(
        self,
        measurement: str,
        tags: Dict[str, object],
        fields: Dict[str, object],
        timestamp_ns: int,
    ) -> None:
        if self.persistence is None:
            return
        if (
            not isinstance(timestamp_ns, int)
            or isinstance(timestamp_ns, bool)
            or timestamp_ns < 0
        ):
            raise ValueError(
                "Metric event timestamp must be a non-negative integer"
            )
        if timestamp_ns % 1_000:
            raise ValueError(
                "Metric event timestamp must be microsecond-aligned"
            )

        with self._lock:
            run_id = self._run_id
            if run_id is None:
                raise RuntimeError(
                    "Cannot persist metrics without an active simulation run"
                )

            if self._last_event_timestamp_ns is None:
                same_timestamp_ordinal = 0
            elif timestamp_ns < self._last_event_timestamp_ns:
                raise ValueError(
                    "Metric event timestamp is backdated relative to the "
                    "persistence sequence"
                )
            elif timestamp_ns == self._last_event_timestamp_ns:
                same_timestamp_ordinal = self._same_timestamp_ordinal + 1
            else:
                same_timestamp_ordinal = 0

            metric_event_sequence = self._metric_event_sequence + 1
            if metric_event_sequence > MAX_INFLUX_INTEGER:
                raise OverflowError("Metric event sequence exhausted")
            metric_event_bucket, timestamp_offset = divmod(
                same_timestamp_ordinal,
                1_000,
            )
            record = MetricRecord.create(
                measurement,
                tags,
                fields,
                run_id=run_id,
                event_type=measurement,
                metric_event_sequence=metric_event_sequence,
                event_timestamp_ns=timestamp_ns,
                persistence_timestamp_ns=timestamp_ns + timestamp_offset,
                metric_event_bucket=metric_event_bucket,
            )
            if not self.persistence.add_nowait(record):
                raise MetricsSinkError(
                    "Metric persistence writer rejected an active-run record"
                )

            self._metric_event_sequence = metric_event_sequence
            self._last_event_timestamp_ns = timestamp_ns
            self._same_timestamp_ordinal = same_timestamp_ordinal

    async def flush(self) -> bool:
        if self.persistence is not None:
            return await self.persistence.flush()
        return True

    async def close(self) -> bool:
        if self.persistence is not None:
            return await self.persistence.close()
        return True


class LatencyTracker:
    """Own typed order traces and emit immutable, clock-safe measurements."""

    def __init__(
        self,
        scheduler,
        metrics_collector: MetricsCollector,
        monotonic_clock: Optional[MonotonicClock] = None,
    ) -> None:
        self.scheduler = scheduler
        self.metrics = metrics_collector
        self.monotonic_clock = monotonic_clock or PerfCounterClock()
        self.orders: Dict[tuple[int, int], OrderLatencyTrace] = {}
        self.cancels: Dict[tuple[int, int], CancelLatencyTrace] = {}
        self.measurements: list[LatencyMeasurement] = []
        self._delivered_exec_ids: set[str] = set()
        self._completed_order_ids: set[tuple[int, int]] = set()
        self._lock = threading.RLock()

    def reset(
        self,
        *,
        preserve_order_keys: Optional[set[tuple[int, int]]] = None,
        preserve_cancel_keys: Optional[set[tuple[int, int]]] = None,
    ) -> None:
        with self._lock:
            if preserve_order_keys is None:
                self.orders.clear()
                self._completed_order_ids.clear()
            else:
                self.orders = {
                    key: trace
                    for key, trace in self.orders.items()
                    if key in preserve_order_keys
                }
                self._completed_order_ids.intersection_update(
                    preserve_order_keys
                )
            if preserve_cancel_keys is None:
                self.cancels.clear()
            else:
                self.cancels = {
                    key: trace
                    for key, trace in self.cancels.items()
                    if key in preserve_cancel_keys
                }
            self.measurements.clear()
            self._delivered_exec_ids.clear()

    def order_submitted(
        self,
        order_id: int,
        session_id: Optional[str] = None,
        sim_submitted_at: Optional[datetime] = None,
        run_generation: int = 0,
    ) -> None:
        key = (run_generation, order_id)
        with self._lock:
            if key in self.orders or key in self._completed_order_ids:
                return
            self.orders[key] = OrderLatencyTrace(
                run_generation=run_generation,
                order_id=order_id,
                session_id=session_id,
                sim_submitted_at=sim_submitted_at or self.scheduler.current_time,
            )

    def order_arrived(
        self,
        order_id: int,
        run_generation: Optional[int] = None,
    ) -> None:
        with self._lock:
            trace = self._find_order_trace_locked(order_id, run_generation)
            if trace is None:
                self._missing_trace(order_id, "order_arrived")
                return
            if trace.sim_received_at is not None:
                return
            trace.sim_received_at = self.scheduler.current_time
            measurement = self._duration_measurement(
                trace,
                SIM_INBOUND_LATENCY,
                trace.sim_submitted_at,
                trace.sim_received_at,
            )
        self._publish(measurement)

    def order_processing_start(
        self,
        order_id: int,
        run_generation: Optional[int] = None,
    ) -> None:
        # The scheduler's configured engine delay is measured from arrival to
        # first report creation.  Real matching is measured inside the engine.
        with self._lock:
            if self._find_order_trace_locked(order_id, run_generation) is None:
                self._missing_trace(order_id, "order_processing_start")

    def order_processing_end(
        self,
        order_id: int,
        run_generation: Optional[int] = None,
    ) -> None:
        with self._lock:
            trace = self._find_order_trace_locked(order_id, run_generation)
            if trace is not None:
                trace.sim_processed_at = self.scheduler.current_time

    def report_created(self, report: ExecutionReport) -> None:
        with self._lock:
            trace = self.orders.get(
                (report.run_generation, report.order_id)
            )
            if trace is None:
                self._missing_trace(report.order_id, "report_created")
                return
            if trace.sim_report_created_at is not None:
                return
            trace.sim_report_created_at = report.timestamp
            if trace.sim_received_at is None:
                self._missing_trace(report.order_id, "report_created_without_arrival")
                return
            measurement = self._duration_measurement(
                trace,
                SIM_ENGINE_LATENCY,
                trace.sim_received_at,
                report.timestamp,
                report_exec_id=report.exec_id,
            )
        self._publish(measurement)

    def report_delivered(
        self,
        report: ExecutionReport,
        sim_delivered_at: Optional[datetime] = None,
    ) -> bool:
        delivered_at = sim_delivered_at or self.scheduler.current_time
        with self._lock:
            if report.exec_id in self._delivered_exec_ids:
                return False
            self._delivered_exec_ids.add(report.exec_id)
            trace = self.orders.get(
                (report.run_generation, report.order_id)
            )
            if trace is None:
                self._missing_trace(report.order_id, "report_delivered")
                return False
            trace.sim_report_delivered_at = delivered_at
            outbound = self._duration_measurement(
                trace,
                SIM_OUTBOUND_LATENCY,
                report.timestamp,
                delivered_at,
                report_exec_id=report.exec_id,
            )
            end_to_end = self._duration_measurement(
                trace,
                SIM_END_TO_END_LATENCY,
                trace.sim_submitted_at,
                delivered_at,
                report_exec_id=report.exec_id,
            )
        self._publish(outbound)
        self._publish(end_to_end)
        return True

    @staticmethod
    def _cancel_key(command: CancelOrderCommand) -> tuple[int, int]:
        return command.run_generation, command.ingress_sequence

    def cancel_received(self, command: CancelOrderCommand) -> None:
        """Start a cancel trace keyed independently of client order IDs."""
        key = self._cancel_key(command)
        with self._lock:
            if key in self.cancels:
                return
            self.cancels[key] = CancelLatencyTrace(
                run_generation=command.run_generation,
                ingress_sequence=command.ingress_sequence,
                session_id=command.session_id,
                cancel_cl_ord_id=command.cancel_cl_ord_id,
                orig_cl_ord_id=command.orig_cl_ord_id,
                sim_received_at=command.requested_at,
            )

    def cancel_arrived(self, command: CancelOrderCommand) -> None:
        key = self._cancel_key(command)
        with self._lock:
            trace = self.cancels.get(key)
            if trace is None:
                self._missing_cancel_trace(command, "cancel_arrived")
                return
            if trace.sim_arrived_at is not None:
                return
            trace.sim_arrived_at = self.scheduler.current_time
            measurement = self._cancel_duration_measurement(
                trace,
                SIM_CANCEL_INBOUND_LATENCY,
                trace.sim_received_at,
                trace.sim_arrived_at,
            )
        self._publish(measurement)

    def cancel_report_created(
        self,
        command: CancelOrderCommand,
        report: ExecutionReport | CancelReject,
    ) -> None:
        key = self._cancel_key(command)
        with self._lock:
            trace = self.cancels.get(key)
            if trace is None:
                self._missing_cancel_trace(command, "cancel_report_created")
                return
            if trace.sim_report_created_at is not None:
                return
            trace.order_id = report.order_id
            trace.sim_report_created_at = report.timestamp
            if trace.sim_arrived_at is None:
                self._missing_cancel_trace(
                    command,
                    "cancel_report_created_without_arrival",
                )
                return
            measurement = self._cancel_duration_measurement(
                trace,
                SIM_CANCEL_ENGINE_LATENCY,
                trace.sim_arrived_at,
                report.timestamp,
                report_exec_id=(
                    report.exec_id
                    if isinstance(report, ExecutionReport)
                    else report.reject_id
                ),
            )
        self._publish(measurement)

    def cancel_report_delivered(
        self,
        report: ExecutionReport | CancelReject,
        *,
        delivered_at: Optional[datetime] = None,
    ) -> bool:
        """Finish a cancel trace only after successful report delivery."""
        ingress_sequence = (
            report.cancel_ingress_sequence
            if isinstance(report, ExecutionReport)
            else report.ingress_sequence
        )
        if ingress_sequence is None:
            return False
        key = (report.run_generation, ingress_sequence)
        with self._lock:
            trace = self.cancels.get(key)
            if trace is None:
                logger.warning(
                    "Cancel latency trace missing",
                    extra={
                        "run_generation": report.run_generation,
                        "ingress_sequence": ingress_sequence,
                        "latency_stage": "cancel_report_delivered",
                    },
                )
                return False
            if trace.sim_report_delivered_at is not None:
                return False
            resolved_delivered_at = delivered_at or self.scheduler.current_time
            trace.sim_report_delivered_at = resolved_delivered_at
            outbound = self._cancel_duration_measurement(
                trace,
                SIM_CANCEL_OUTBOUND_LATENCY,
                report.timestamp,
                resolved_delivered_at,
                report_exec_id=(
                    report.exec_id
                    if isinstance(report, ExecutionReport)
                    else report.reject_id
                ),
            )
            end_to_end = self._cancel_duration_measurement(
                trace,
                SIM_CANCEL_END_TO_END_LATENCY,
                trace.sim_received_at,
                resolved_delivered_at,
                report_exec_id=(
                    report.exec_id
                    if isinstance(report, ExecutionReport)
                    else report.reject_id
                ),
            )
            self.cancels.pop(key, None)
        self._publish(outbound)
        self._publish(end_to_end)
        return True

    def record_real_matching_duration(
        self,
        order_id: int,
        session_id: Optional[str],
        duration_ns: int,
        timestamp: datetime,
    ) -> Optional[LatencyMeasurement]:
        measurement = self._real_measurement(
            order_id=order_id,
            session_id=session_id,
            metric=REAL_MATCHING_DURATION,
            duration_ns=duration_ns,
            timestamp=timestamp,
        )
        if measurement is not None:
            with self._lock:
                trace = self._find_order_trace_locked(order_id, None)
                if trace is not None:
                    trace.real_matching_duration_ns = duration_ns
        self._publish(measurement)
        return measurement

    def record_real_report_serialization_duration(
        self,
        report: ExecutionReport,
        duration_ns: int,
    ) -> Optional[LatencyMeasurement]:
        measurement = self._real_measurement(
            order_id=report.order_id,
            session_id=report.session_id,
            metric=REAL_REPORT_SERIALIZATION_DURATION,
            duration_ns=duration_ns,
            timestamp=self.scheduler.current_time,
            report_exec_id=report.exec_id,
        )
        self._publish(measurement)
        return measurement

    def measure_real_duration(
        self,
        *,
        order_id: int,
        session_id: Optional[str],
        metric: str,
        start_ns: int,
        end_ns: int,
        timestamp: datetime,
        report_exec_id: Optional[str] = None,
    ) -> Optional[LatencyMeasurement]:
        try:
            duration_ns = monotonic_duration_ns(
                start_ns,
                end_ns,
                context=metric,
            )
        except ValueError:
            self.metrics.record_invalid_latency(
                metric=metric,
                order_id=order_id,
                session_id=session_id,
            )
            return None
        measurement = self._real_measurement(
            order_id=order_id,
            session_id=session_id,
            metric=metric,
            duration_ns=duration_ns,
            timestamp=timestamp,
            report_exec_id=report_exec_id,
        )
        self._publish(measurement)
        return measurement

    def complete_order(self, report: ExecutionReport) -> bool:
        """Mark terminal cleanup from this report's immutable status."""
        if report.ord_status not in {
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        }:
            return False
        key = (report.run_generation, report.order_id)
        with self._lock:
            if key in self._completed_order_ids:
                return False
            if key not in self.orders:
                self._missing_trace(report.order_id, "terminal_cleanup")
                return False
            self._completed_order_ids.add(key)
            return True

    def cleanup_order(
        self,
        order_id: int,
        *,
        run_generation: int = 0,
    ) -> None:
        """Remove a trace after terminal measurements have been emitted."""
        with self._lock:
            self.orders.pop((run_generation, order_id), None)

    def _find_order_trace_locked(
        self,
        order_id: int,
        run_generation: Optional[int],
    ) -> Optional[OrderLatencyTrace]:
        if run_generation is not None:
            return self.orders.get((run_generation, order_id))
        matches = [
            (generation, trace)
            for (generation, candidate_id), trace in self.orders.items()
            if candidate_id == order_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: item[0])[1]

    def _duration_measurement(
        self,
        trace: OrderLatencyTrace,
        metric: str,
        start: datetime,
        end: datetime,
        *,
        report_exec_id: Optional[str] = None,
    ) -> Optional[LatencyMeasurement]:
        seconds = (end - start).total_seconds()
        if not math.isfinite(seconds) or seconds < 0.0:
            self.metrics.record_invalid_latency(
                metric=metric,
                order_id=trace.order_id,
                session_id=trace.session_id,
            )
            return None
        return LatencyMeasurement(
            order_id=trace.order_id,
            session_id=trace.session_id,
            metric=metric,
            value=seconds,
            unit="seconds",
            timestamp=end,
            report_exec_id=report_exec_id,
            run_generation=trace.run_generation,
        )

    def _cancel_duration_measurement(
        self,
        trace: CancelLatencyTrace,
        metric: str,
        start: datetime,
        end: datetime,
        *,
        report_exec_id: Optional[str] = None,
    ) -> Optional[LatencyMeasurement]:
        seconds = (end - start).total_seconds()
        if not math.isfinite(seconds) or seconds < 0.0:
            self.metrics.record_invalid_latency(
                metric=metric,
                order_id=trace.order_id,
                session_id=trace.session_id,
            )
            return None
        return LatencyMeasurement(
            order_id=trace.order_id,
            session_id=trace.session_id,
            metric=metric,
            value=seconds,
            unit="seconds",
            timestamp=end,
            report_exec_id=report_exec_id,
            run_generation=trace.run_generation,
            ingress_sequence=trace.ingress_sequence,
            cl_ord_id=trace.cancel_cl_ord_id,
            orig_cl_ord_id=trace.orig_cl_ord_id,
        )

    def _real_measurement(
        self,
        *,
        order_id: int,
        session_id: Optional[str],
        metric: str,
        duration_ns: int,
        timestamp: datetime,
        report_exec_id: Optional[str] = None,
    ) -> Optional[LatencyMeasurement]:
        if duration_ns < 0:
            self.metrics.record_invalid_latency(
                metric=metric,
                order_id=order_id,
                session_id=session_id,
            )
            return None
        return LatencyMeasurement(
            order_id=order_id,
            session_id=session_id,
            metric=metric,
            value=duration_ns / 1_000_000_000.0,
            unit="seconds",
            timestamp=timestamp,
            report_exec_id=report_exec_id,
        )

    def _publish(
        self,
        measurement: Optional[LatencyMeasurement],
    ) -> None:
        if measurement is None:
            return
        if self.metrics.observe_latency(measurement):
            with self._lock:
                self.measurements.append(measurement)

    @staticmethod
    def _missing_trace(order_id: int, stage: str) -> None:
        logger.warning(
            "Latency trace missing",
            extra={"order_id": order_id, "latency_stage": stage},
        )

    @staticmethod
    def _missing_cancel_trace(
        command: CancelOrderCommand,
        stage: str,
    ) -> None:
        logger.warning(
            "Cancel latency trace missing",
            extra={
                "run_generation": command.run_generation,
                "ingress_sequence": command.ingress_sequence,
                "cancel_cl_ord_id": command.cancel_cl_ord_id,
                "latency_stage": stage,
            },
        )

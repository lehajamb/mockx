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
    monotonic_duration_ns,
)
from .models import (
    CancelOrderCommand,
    CancelReject,
    ExecutionReport,
    ExecutionType,
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
        REAL_MATCHING_DURATION,
        REAL_REPORT_SERIALIZATION_DURATION,
    }
)


@dataclass(frozen=True, slots=True)
class MetricRecord:
    """One immutable persistence record independent of an InfluxDB client."""

    measurement: str
    tags: Tuple[Tuple[str, str], ...]
    fields: Tuple[Tuple[str, Any], ...]
    timestamp_ns: int

    @classmethod
    def create(
        cls,
        measurement: str,
        tags: Mapping[str, object],
        fields: Mapping[str, object],
        timestamp_ns: int,
    ) -> "MetricRecord":
        if not measurement:
            raise ValueError("Metric measurement is required")
        return cls(
            measurement=measurement,
            tags=tuple(sorted((key, str(value)) for key, value in tags.items())),
            fields=tuple(sorted(fields.items())),
            timestamp_ns=int(timestamp_ns),
        )

    @property
    def tag_map(self) -> Dict[str, str]:
        return dict(self.tags)

    @property
    def field_map(self) -> Dict[str, Any]:
        return dict(self.fields)


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
        tsdb_writer: Optional[object] = None,
        *,
        persistence: Optional[BufferedMetricsWriter] = None,
        prometheus_enabled: bool = True,
    ) -> None:
        self.exchange_name = exchange_name
        self.tsdb_writer = tsdb_writer  # Compatibility for existing integrations.
        self.persistence = persistence
        self.metrics_buffer: list[MetricRecord] = []
        self._run_id: Optional[str] = None
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
        self._seen_trade_ids: set[str] = set()

        self.registry = None
        self._prom_counters: Dict[str, Any] = {}
        self._prom_gauges: Dict[str, Any] = {}
        self._prom_histograms: Dict[str, Any] = {}
        if prometheus_enabled and CollectorRegistry is not None:
            self._init_prometheus()

        if self.persistence is not None:
            self.persistence.on_failure = self.record_sink_failure

    @property
    def run_id(self) -> Optional[str]:
        with self._lock:
            return self._run_id

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
            "qty": report.order_qty,
            "filled_qty": report.cum_qty,
            "canceled_qty": report.canceled_qty,
            "leaves_qty": report.leaves_qty,
            "avg_fill_price": report.avg_px,
            "ts_submitted": int(
                report.submitted_at.timestamp() * 1_000_000_000
            ),
        }
        if report.order_price is not None:
            fields["price"] = report.order_price
        self._create_point(
            "orders",
            tags,
            fields,
            int(report.timestamp.timestamp() * 1_000_000_000),
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
                "price": trade.price,
                "qty": trade.qty,
                "volume": trade.volume,
                "buyer_order_id": trade.buyer_order_id,
                "seller_order_id": trade.seller_order_id,
                "aggressor_order_id": trade.aggressor_order_id,
                "passive_order_id": trade.passive_order_id,
                "level": trade.level,
            },
            int(trade.timestamp.timestamp() * 1_000_000_000),
        )
        self.refresh_engine_gauges()
        return True

    def record_cancel_request(self, command: CancelOrderCommand) -> None:
        with self._lock:
            self._increment_locked("cancel_requests")

    def record_cancel_reject(self, reject: CancelReject) -> None:
        with self._lock:
            self._increment_locked("cancel_rejects")

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

    def record_invalid_latency(
        self,
        *,
        metric: str,
        order_id: int,
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
        self._create_point(
            "latency",
            {
                "exchange": self.exchange_name,
                "type": measurement.metric,
            },
            {"duration_seconds": measurement.value},
            int(measurement.timestamp.timestamp() * 1_000_000_000),
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
        timestamp_ns = int(update.timestamp.timestamp() * 1_000_000_000)
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
            else len(self.metrics_buffer)
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
        if self.persistence is None and self.tsdb_writer is None:
            return
        record_tags = dict(tags)
        with self._lock:
            run_id = self._run_id
        if run_id:
            record_tags["run_id"] = run_id
        record = MetricRecord.create(
            measurement,
            record_tags,
            fields,
            timestamp_ns,
        )
        if self.persistence is not None:
            self.persistence.add_nowait(record)
        else:
            self.metrics_buffer.append(record)

    async def flush(self) -> bool:
        if self.persistence is not None:
            return await self.persistence.flush()
        if not self.metrics_buffer:
            return True
        writer = self.tsdb_writer
        if writer is None or not hasattr(writer, "write_records"):
            logger.error(
                "Legacy TSDB writer cannot persist buffered MetricRecord data",
                extra={"buffer_size": len(self.metrics_buffer)},
            )
            return False
        batch = tuple(self.metrics_buffer)
        try:
            result = writer.write_records(batch)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            self.record_sink_failure(exc, len(self.metrics_buffer))
            return False
        del self.metrics_buffer[: len(batch)]
        return True

    async def close(self) -> bool:
        if self.persistence is not None:
            return await self.persistence.close()
        return await self.flush()

    async def set_run_id_async(self, run_id: Optional[str]) -> bool:
        if not await self.flush():
            return False
        with self._lock:
            self._run_id = run_id
        return True

    def set_run_id(self, run_id: Optional[str]) -> None:
        """Set the tag for new records without discarding existing records."""
        with self._lock:
            self._run_id = run_id

    def flush_tsdb(self):
        """Compatibility shim returning a task when called in an event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.flush())
        return loop.create_task(self.flush())


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
        self.orders: Dict[int, OrderLatencyTrace] = {}
        self.measurements: list[LatencyMeasurement] = []
        self._delivered_exec_ids: set[str] = set()
        self._completed_order_ids: set[int] = set()
        self._lock = threading.RLock()

    def reset(self) -> None:
        with self._lock:
            self.orders.clear()
            self.measurements.clear()
            self._delivered_exec_ids.clear()
            self._completed_order_ids.clear()

    def order_submitted(
        self,
        order_id: int,
        session_id: Optional[str] = None,
        sim_submitted_at: Optional[datetime] = None,
    ) -> None:
        with self._lock:
            if order_id in self.orders or order_id in self._completed_order_ids:
                return
            self.orders[order_id] = OrderLatencyTrace(
                order_id=order_id,
                session_id=session_id,
                sim_submitted_at=sim_submitted_at or self.scheduler.current_time,
            )

    def order_arrived(self, order_id: int) -> None:
        with self._lock:
            trace = self.orders.get(order_id)
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

    def order_processing_start(self, order_id: int) -> None:
        # The scheduler's configured engine delay is measured from arrival to
        # first report creation.  Real matching is measured inside the engine.
        if order_id not in self.orders:
            self._missing_trace(order_id, "order_processing_start")

    def order_processing_end(self, order_id: int) -> None:
        with self._lock:
            trace = self.orders.get(order_id)
            if trace is not None:
                trace.sim_processed_at = self.scheduler.current_time

    def report_created(self, report: ExecutionReport) -> None:
        with self._lock:
            trace = self.orders.get(report.order_id)
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
            trace = self.orders.get(report.order_id)
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
                trace = self.orders.get(order_id)
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
            timestamp=report.timestamp,
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
        with self._lock:
            if report.order_id in self._completed_order_ids:
                return False
            if report.order_id not in self.orders:
                self._missing_trace(report.order_id, "terminal_cleanup")
                return False
            self._completed_order_ids.add(report.order_id)
            return True

    def cleanup_order(self, order_id: int) -> None:
        """Remove a trace after terminal measurements have been emitted."""
        with self._lock:
            self.orders.pop(order_id, None)

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

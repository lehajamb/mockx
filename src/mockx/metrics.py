"""Reliable process metrics, latency tracking, and buffered persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
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
from .instruments import InstrumentCatalog
from .numeric import decimal_sum
from .models import (
    CancellationOutcome,
    CancelLatencyTrace,
    CancelOrderCommand,
    ReplaceOrderCommand,
    CancelReject,
    ExecutionReport,
    ExecutionType,
    FixDeliveryQueueSnapshot,
    FixDeliveryState,
    LatencyMeasurement,
    LiquidityCategory,
    MarketDataSnapshot,
    MetricsSnapshot,
    NewOrderCommand,
    OrderLatencyTrace,
    OrderStatus,
    SchedulerFailure,
    SchedulerFailureStatus,
    SchedulerState,
    SelfTradePrevention,
    Trade,
    liquidity_category_for_session,
)


logger = logging.getLogger(__name__)


SIM_INBOUND_LATENCY = "mockx_sim_inbound_latency_ms"
SIM_ENGINE_LATENCY = "mockx_sim_engine_latency_ms"
SIM_OUTBOUND_LATENCY = "mockx_sim_outbound_latency_ms"
SIM_END_TO_END_LATENCY = "mockx_sim_end_to_end_latency_ms"
SIM_CANCEL_INBOUND_LATENCY = "mockx_sim_cancel_inbound_latency_ms"
SIM_CANCEL_ENGINE_LATENCY = "mockx_sim_cancel_engine_latency_ms"
SIM_CANCEL_OUTBOUND_LATENCY = "mockx_sim_cancel_outbound_latency_ms"
SIM_CANCEL_END_TO_END_LATENCY = "mockx_sim_cancel_end_to_end_latency_ms"
SIM_REPLACE_INBOUND_LATENCY = "mockx_sim_replace_inbound_latency_ms"
SIM_REPLACE_ENGINE_LATENCY = "mockx_sim_replace_engine_latency_ms"
SIM_REPLACE_OUTBOUND_LATENCY = "mockx_sim_replace_outbound_latency_ms"
SIM_REPLACE_END_TO_END_LATENCY = "mockx_sim_replace_end_to_end_latency_ms"
MATCH_LOOP_DURATION = "mockx_match_loop_duration_us"
BOOK_COMMIT_DURATION = "mockx_book_commit_duration_us"
ENGINE_COMMAND_DURATION = "mockx_engine_command_duration_us"
REPORT_CREATION_DURATION = "mockx_report_creation_duration_us"
SERIALIZATION_DURATION = "mockx_serialization_duration_us"

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
        SIM_REPLACE_INBOUND_LATENCY,
        SIM_REPLACE_ENGINE_LATENCY,
        SIM_REPLACE_OUTBOUND_LATENCY,
        SIM_REPLACE_END_TO_END_LATENCY,
        MATCH_LOOP_DURATION,
        BOOK_COMMIT_DURATION,
        ENGINE_COMMAND_DURATION,
        REPORT_CREATION_DURATION,
        SERIALIZATION_DURATION,
    }
)

SIMULATED_LATENCY_METRICS = frozenset(
    metric for metric in LATENCY_METRICS if metric.endswith("_ms")
)
CPU_DURATION_METRICS = LATENCY_METRICS - SIMULATED_LATENCY_METRICS

METRIC_IDENTITY_SYMBOLS = frozenset({"run_id"})
METRIC_IDENTITY_COLUMNS = frozenset({"event_sequence", "event_timestamp_ns"})
MAX_QUESTDB_LONG = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class PersistenceIntegrityFailure:
    """First permanent loss or uncertain admission in one simulation run."""

    run_id: str
    metric_event_sequence: Optional[int]
    event_timestamp_ns: Optional[int]
    reason: str


@dataclass(frozen=True, slots=True)
class MetricRecord:
    """One immutable record shaped for a QuestDB ILP row."""

    table: str
    symbols: Tuple[Tuple[str, str], ...]
    columns: Tuple[Tuple[str, Any], ...]
    timestamp_ns: int
    _encoded_size_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        payload = {
            "table": self.table,
            "symbols": self.symbols,
            "columns": self.columns,
            "timestamp_ns": self.timestamp_ns,
        }
        size = len(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8")
        )
        object.__setattr__(self, "_encoded_size_bytes", size)

    @classmethod
    def create(
        cls,
        table: str,
        symbols: Mapping[str, object],
        columns: Mapping[str, object],
        *,
        run_id: str,
        event_sequence: int,
        event_timestamp_ns: int,
    ) -> "MetricRecord":
        if not table:
            raise ValueError("Metric table is required")
        if not run_id:
            raise ValueError("Metric run_id is required")
        if (
            not isinstance(event_sequence, int)
            or isinstance(event_sequence, bool)
            or not 1 <= event_sequence <= MAX_QUESTDB_LONG
        ):
            raise ValueError("event_sequence must be a positive QuestDB LONG")

        event_timestamp_ns = int(event_timestamp_ns)
        if not 0 <= event_timestamp_ns <= MAX_QUESTDB_LONG:
            raise ValueError("Metric timestamps must fit a non-negative QuestDB LONG")

        reserved_symbols = METRIC_IDENTITY_SYMBOLS.intersection(symbols)
        if reserved_symbols:
            raise ValueError(
                "Metric domain symbols use reserved identity names: "
                + ", ".join(sorted(reserved_symbols))
            )
        reserved_columns = METRIC_IDENTITY_COLUMNS.intersection(columns)
        if reserved_columns:
            raise ValueError(
                "Metric domain columns use reserved identity names: "
                + ", ".join(sorted(reserved_columns))
            )

        record_symbols = dict(symbols)
        record_symbols["run_id"] = run_id
        record_columns = dict(columns)
        record_columns.update(
            event_sequence=event_sequence,
            event_timestamp_ns=event_timestamp_ns,
        )
        return cls(
            table=table,
            symbols=tuple(
                sorted((key, str(value)) for key, value in record_symbols.items())
            ),
            columns=tuple(sorted(record_columns.items())),
            timestamp_ns=event_timestamp_ns,
        )

    @property
    def symbol_map(self) -> Dict[str, str]:
        return dict(self.symbols)

    @property
    def column_map(self) -> Dict[str, Any]:
        return dict(self.columns)

    @property
    def event_sequence(self) -> int:
        return int(self.column_map["event_sequence"])

    @property
    def run_id(self) -> str:
        return self.symbol_map["run_id"]

    @property
    def encoded_size_bytes(self) -> int:
        """Cached immutable logical payload size used by buffer accounting."""
        return self._encoded_size_bytes


class MetricsSinkError(RuntimeError):
    """Expected persistence-adapter failure."""


class PersistenceBackpressureTimeout(MetricsSinkError):
    """The admitted backlog did not fall below the low watermark in time."""


class PersistenceAddOutcome(str, Enum):
    """Explicit admission result for one logical persistence record."""

    ACCEPTED = "ACCEPTED"
    ACCEPTED_FLUSH_REQUIRED = "ACCEPTED_FLUSH_REQUIRED"
    REJECTED_CLOSED = "REJECTED_CLOSED"
    REJECTED_FULL = "REJECTED_FULL"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    TOO_LARGE = "TOO_LARGE"


class PersistenceFlushOutcome(str, Enum):
    """Explicit result for one requested buffer drain."""

    FLUSHED = "FLUSHED"
    NOTHING_TO_FLUSH = "NOTHING_TO_FLUSH"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CLOSED = "CLOSED"


class PersistenceCloseOutcome(str, Enum):
    """Explicit terminal result for the writer close boundary."""

    CLOSED = "CLOSED"
    ALREADY_CLOSED = "ALREADY_CLOSED"
    CLOSED_WITH_UNFLUSHED = "CLOSED_WITH_UNFLUSHED"
    TIMED_OUT = "TIMED_OUT"
    SINK_CLOSE_FAILED = "SINK_CLOSE_FAILED"


class PersistenceHealthState(str, Enum):
    DISABLED = "DISABLED"
    HEALTHY = "HEALTHY"
    RETRYING = "RETRYING"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class PersistenceFlushResult:
    outcome: PersistenceFlushOutcome
    records_written: int
    remaining_records: int
    error: Optional[str] = None

    @property
    def successful(self) -> bool:
        return self.outcome in {
            PersistenceFlushOutcome.FLUSHED,
            PersistenceFlushOutcome.NOTHING_TO_FLUSH,
        }


@dataclass(frozen=True, slots=True)
class PersistenceCloseResult:
    outcome: PersistenceCloseOutcome
    records_written: int
    unflushed_records: int
    error: Optional[str] = None

    @property
    def successful(self) -> bool:
        return self.outcome in {
            PersistenceCloseOutcome.CLOSED,
            PersistenceCloseOutcome.ALREADY_CLOSED,
        }


@dataclass(frozen=True, slots=True)
class PersistenceHealthSnapshot:
    state: PersistenceHealthState
    last_successful_write_time: Optional[datetime]
    last_failure_time: Optional[datetime]
    consecutive_failures: int
    lifetime_failures: int
    buffered_records: int
    buffered_bytes: int
    oldest_buffered_record_age_seconds: Optional[float]
    accepted_records: int
    flush_required_records: int
    dropped_records: int
    rejected_closed_records: int
    rejected_full_records: int
    failed_records: int
    worker_running: bool
    close_outcome: Optional[PersistenceCloseOutcome]
    unflushed_on_close: int


class MetricsSink(Protocol):
    """Asynchronous persistence boundary used by the buffered writer."""

    async def write(self, records: Sequence[MetricRecord]) -> None: ...

    async def flush(self) -> None: ...

    async def close(self) -> None: ...

    async def health_check(self) -> None: ...


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

    async def health_check(self) -> None:
        if self.closed:
            raise MetricsSinkError("In-memory metrics sink is closed")


@dataclass(frozen=True, slots=True)
class _BufferedMetricRecord:
    record: MetricRecord
    encoded_size_bytes: int
    enqueued_monotonic_ns: int
    group_id: int = 0


class BufferedMetricsWriter:
    """Strictly bounded, loss-aware buffer with one coalesced worker."""

    def __init__(
        self,
        sink: MetricsSink,
        *,
        flush_batch_size: int = 1_000,
        flush_batch_bytes: int | None = None,
        max_buffer_records: int = 10_000,
        max_buffer_bytes: int = 67_108_864,
        flush_interval_ms: int = 100,
        retry_backoff_ms: int = 1_000,
        max_retry_delay_ms: int = 30_000,
        shutdown_timeout_ms: int = 10_000,
        backpressure_timeout_ms: int = 10_000,
        on_failure: Optional[Callable[[BaseException, int], None]] = None,
        on_outcome: Optional[Callable[[PersistenceAddOutcome, int], None]] = None,
    ) -> None:
        for name, value in (
            ("flush_batch_size", flush_batch_size),
            ("max_buffer_records", max_buffer_records),
            ("max_buffer_bytes", max_buffer_bytes),
            ("flush_interval_ms", flush_interval_ms),
            ("retry_backoff_ms", retry_backoff_ms),
            ("max_retry_delay_ms", max_retry_delay_ms),
            ("shutdown_timeout_ms", shutdown_timeout_ms),
            ("backpressure_timeout_ms", backpressure_timeout_ms),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"Persistence {name} must be positive")
        if max_buffer_records < flush_batch_size:
            raise ValueError("max_buffer_records must be at least flush_batch_size")
        if max_retry_delay_ms < retry_backoff_ms:
            raise ValueError("max_retry_delay_ms must be at least retry_backoff_ms")
        if flush_batch_bytes is None:
            flush_batch_bytes = min(4_194_304, max_buffer_bytes)
        if (
            not isinstance(flush_batch_bytes, int)
            or isinstance(flush_batch_bytes, bool)
            or not 1 <= flush_batch_bytes <= max_buffer_bytes
        ):
            raise ValueError("flush_batch_bytes must fit the buffer byte capacity")
        self.sink = sink
        self.flush_batch_size = flush_batch_size
        self.flush_batch_bytes = flush_batch_bytes
        self.max_buffer_records = max_buffer_records
        self.max_buffer_bytes = max_buffer_bytes
        self.flush_interval_ms = flush_interval_ms
        self.retry_backoff_ms = retry_backoff_ms
        self.max_retry_delay_ms = max_retry_delay_ms
        self.shutdown_timeout_ms = shutdown_timeout_ms
        self.backpressure_timeout_ms = backpressure_timeout_ms
        self.on_failure = on_failure
        self.on_outcome = on_outcome
        self._admission_lock = asyncio.Lock()
        self._progress_event: Optional[asyncio.Event] = None
        self._next_group_id = 0
        self._drain_requested = False
        self.before_wait: Optional[Callable[[], None]] = None
        self._buffer: Deque[_BufferedMetricRecord] = deque()
        self._inflight: Tuple[_BufferedMetricRecord, ...] = ()
        self._buffered_bytes = 0
        self._queued_bytes = 0
        self._buffer_lock = threading.RLock()
        self._flush_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._closing = False
        self._sink_closed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake_event: Optional[asyncio.Event] = None
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._stop_worker = False

        self._health_state = PersistenceHealthState.HEALTHY
        self._last_successful_write_time: Optional[datetime] = None
        self._last_failure_time: Optional[datetime] = None
        self._consecutive_failures = 0
        self._lifetime_failures = 0
        self._current_retry_delay_ms = retry_backoff_ms
        self._retry_deadline_ns: Optional[int] = None
        self._outcomes = {outcome: 0 for outcome in PersistenceAddOutcome}
        self._dropped_records = 0
        self._last_close_result: Optional[PersistenceCloseResult] = None

    @property
    def buffered_count(self) -> int:
        with self._buffer_lock:
            return len(self._buffer) + len(self._inflight)

    @property
    def buffered_bytes(self) -> int:
        with self._buffer_lock:
            return self._buffered_bytes

    @property
    def failure_count(self) -> int:
        with self._buffer_lock:
            return self._lifetime_failures

    @property
    def worker_task(self) -> Optional[asyncio.Task[None]]:
        return self._worker_task

    @property
    def closed(self) -> bool:
        with self._buffer_lock:
            return self._closed

    async def start(self) -> None:
        """Bind one event loop and start the sole automatic flush worker."""
        loop = asyncio.get_running_loop()
        with self._buffer_lock:
            if self._closed:
                return
            if self._worker_task is not None and not self._worker_task.done():
                if self._loop is not loop:
                    raise RuntimeError(
                        "Metrics writer is already bound to another loop"
                    )
                return
            self._loop = loop
            self._wake_event = asyncio.Event()
            self._progress_event = asyncio.Event()
            self._stop_worker = False
            self._worker_task = loop.create_task(
                self._worker_loop(),
                name="mockx-metrics-flush-worker",
            )
        if not await self._check_sink_health():
            self._signal_worker()

    def _capacity_available(self, encoded_size_bytes: int, records: int = 1) -> bool:
        return (
            self.buffered_count + records <= self.max_buffer_records
            and self._buffered_bytes + encoded_size_bytes <= self.max_buffer_bytes
        )

    def _append(self, record: MetricRecord) -> PersistenceAddOutcome:
        return self._append_group((record,))

    def _append_group(self, records: Sequence[MetricRecord]) -> PersistenceAddOutcome:
        records = tuple(records)
        if not records:
            raise ValueError("A persistence group cannot be empty")
        size = sum(record.encoded_size_bytes for record in records)
        with self._buffer_lock:
            if self._closed or self._closing:
                return PersistenceAddOutcome.REJECTED_CLOSED
            if len(records) > self.max_buffer_records or size > self.max_buffer_bytes:
                return PersistenceAddOutcome.TOO_LARGE
            if not self._capacity_available(size, len(records)):
                return PersistenceAddOutcome.REJECTED_FULL
            self._next_group_id += 1
            now = time.monotonic_ns()
            entries = tuple(
                _BufferedMetricRecord(
                    record, record.encoded_size_bytes, now, self._next_group_id
                )
                for record in records
            )
            self._buffer.extend(entries)
            self._buffered_bytes += size
            self._queued_bytes += size
            queued_bytes = self._queued_bytes
            if (
                len(self._buffer) >= self.flush_batch_size
                or queued_bytes >= self.flush_batch_bytes
            ):
                return PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED
            return PersistenceAddOutcome.ACCEPTED

    def _record_outcome(self, outcome: PersistenceAddOutcome, amount: int = 1) -> None:
        with self._buffer_lock:
            self._outcomes[outcome] += amount
        if self.on_outcome is not None:
            try:
                self.on_outcome(outcome, amount)
            except Exception:
                logger.exception("Persistence outcome observer failed")

    def _signal_worker(self) -> None:
        loop = self._loop
        wake_event = self._wake_event
        if loop is None or wake_event is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            wake_event.set()
        else:
            loop.call_soon_threadsafe(wake_event.set)

    def add_nowait(self, record: MetricRecord) -> PersistenceAddOutcome:
        outcome = self._append(record)
        self._record_outcome(outcome)
        if outcome in {
            PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED,
            PersistenceAddOutcome.REJECTED_FULL,
        }:
            self._signal_worker()
        return outcome

    async def add(self, record: MetricRecord) -> PersistenceAddOutcome:
        return await self.add_group((record,))

    async def add_group(
        self, records: Sequence[MetricRecord], *, timeout_ms: int | None = None
    ) -> PersistenceAddOutcome:
        """Stage one immutable group; admit every row together or none."""
        records = tuple(records)
        if not records:
            raise ValueError("A persistence group cannot be empty")
        if self._worker_task is None:
            await self.start()
        deadline = (
            asyncio.get_running_loop().time()
            + (timeout_ms or self.backpressure_timeout_ms) / 1000
        )
        async with self._admission_lock:
            while True:
                progress = self._progress_event
                if progress is not None:
                    progress.clear()
                outcome = self._append_group(records)
                if outcome != PersistenceAddOutcome.REJECTED_FULL:
                    self._record_outcome(outcome, len(records))
                    if outcome == PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED:
                        self._signal_worker()
                    return outcome
                self._drain_requested = True
                if self.before_wait is not None:
                    self.before_wait()
                self._signal_worker()
                if not await self._wait_progress(deadline):
                    self._record_outcome(PersistenceAddOutcome.TIMED_OUT, len(records))
                    return PersistenceAddOutcome.TIMED_OUT

    async def _wait_progress(self, deadline: float) -> bool:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0 or self._progress_event is None:
            return False
        try:
            await asyncio.wait_for(self._progress_event.wait(), remaining)
            return True
        except asyncio.TimeoutError:
            return False

    def _notify_progress(self) -> None:
        loop, progress = self._loop, self._progress_event
        if loop is None or progress is None or loop.is_closed():
            return
        try:
            if asyncio.get_running_loop() is loop:
                progress.set()
            else:
                loop.call_soon_threadsafe(progress.set)
        except RuntimeError:
            loop.call_soon_threadsafe(progress.set)

    async def wait_for_pressure_relief(self, *, timeout_ms: int | None = None) -> bool:
        """At 80% pressure, drain to 50%, respecting the sink retry deadline."""
        if self._worker_task is None:
            await self.start()
        with self._buffer_lock:
            pressured = (
                self.buffered_count * 5 >= self.max_buffer_records * 4
                or self._buffered_bytes * 5 >= self.max_buffer_bytes * 4
            )
        if not pressured:
            return True
        deadline = (
            asyncio.get_running_loop().time()
            + (timeout_ms or self.backpressure_timeout_ms) / 1000
        )
        while True:
            if self._progress_event is not None:
                self._progress_event.clear()
            with self._buffer_lock:
                if self._closed or self._closing:
                    raise MetricsSinkError(
                        "Persistence writer closed during backpressure"
                    )
                if (
                    self.buffered_count * 2 <= self.max_buffer_records
                    and self._buffered_bytes * 2 <= self.max_buffer_bytes
                ):
                    return True
            self._drain_requested = True
            if self.before_wait is not None:
                self.before_wait()
            self._signal_worker()
            if not await self._wait_progress(deadline):
                raise PersistenceBackpressureTimeout(
                    "PERSISTENCE_BACKPRESSURE_TIMEOUT: admitted backlog did not drain"
                )

    def _take_batch(self) -> Tuple[_BufferedMetricRecord, ...]:
        with self._buffer_lock:
            if self._inflight:
                raise RuntimeError("Persistence writer already has an in-flight batch")
            batch: list[_BufferedMetricRecord] = []
            size = 0
            while self._buffer:
                group_id = self._buffer[0].group_id
                group = []
                for entry in self._buffer:
                    if entry.group_id != group_id:
                        break
                    group.append(entry)
                group_size = sum(entry.encoded_size_bytes for entry in group)
                if batch and (
                    len(batch) + len(group) > self.flush_batch_size
                    or size + group_size > self.flush_batch_bytes
                ):
                    break
                batch.extend(self._buffer.popleft() for _ in group)
                size += group_size
                if (
                    len(batch) >= self.flush_batch_size
                    or size >= self.flush_batch_bytes
                ):
                    break
            self._inflight = tuple(batch)
            self._queued_bytes -= size
            return self._inflight

    def _restore_inflight(self) -> int:
        with self._buffer_lock:
            self._queued_bytes += sum(
                entry.encoded_size_bytes for entry in self._inflight
            )
            for entry in reversed(self._inflight):
                self._buffer.appendleft(entry)
            self._inflight = ()
            return len(self._buffer)

    def _complete_inflight(self) -> int:
        with self._buffer_lock:
            written = len(self._inflight)
            self._buffered_bytes -= sum(
                entry.encoded_size_bytes for entry in self._inflight
            )
            self._inflight = ()
        self._notify_progress()
        return written

    async def _write_one_batch(self) -> PersistenceFlushResult:
        batch = self._take_batch()
        if not batch:
            return PersistenceFlushResult(
                PersistenceFlushOutcome.NOTHING_TO_FLUSH,
                records_written=0,
                remaining_records=0,
            )
        try:
            await self.sink.write(tuple(entry.record for entry in batch))
        except asyncio.CancelledError:
            # A canceled write has no confirmed outcome. Retain its identities
            # ahead of any newer admissions so a later flush can retry them.
            self._restore_inflight()
            raise
        except Exception as exc:
            buffer_size = self._restore_inflight()
            self._record_failure(exc, buffer_size)
            logger.error(
                "Metrics flush failed; batch retained for retry",
                extra={
                    "metric_sink": type(self.sink).__name__,
                    "buffer_size": buffer_size,
                },
                exc_info=True,
            )
            return PersistenceFlushResult(
                PersistenceFlushOutcome.FAILED,
                records_written=0,
                remaining_records=buffer_size,
                error=f"{type(exc).__name__}: {exc}",
            )

        written = self._complete_inflight()
        self._record_successful_write()
        return PersistenceFlushResult(
            PersistenceFlushOutcome.FLUSHED,
            records_written=written,
            remaining_records=self.buffered_count,
        )

    async def flush(self) -> PersistenceFlushResult:
        """Drain all admitted records, stopping on the first sink failure."""
        if self.closed:
            return PersistenceFlushResult(
                PersistenceFlushOutcome.CLOSED,
                records_written=0,
                remaining_records=self.buffered_count,
            )
        total_written = 0
        async with self._flush_lock:
            if self.buffered_count == 0:
                return PersistenceFlushResult(
                    PersistenceFlushOutcome.NOTHING_TO_FLUSH,
                    records_written=0,
                    remaining_records=0,
                )
            while self.buffered_count:
                result = await self._write_one_batch()
                total_written += result.records_written
                if not result.successful:
                    return PersistenceFlushResult(
                        result.outcome,
                        records_written=total_written,
                        remaining_records=result.remaining_records,
                        error=result.error,
                    )
        return PersistenceFlushResult(
            PersistenceFlushOutcome.FLUSHED,
            records_written=total_written,
            remaining_records=0,
        )

    def _automatic_retry_ready(self) -> bool:
        with self._buffer_lock:
            return not self._stop_worker and (
                self._retry_deadline_ns is None
                or time.monotonic_ns() >= self._retry_deadline_ns
            )

    async def _flush_automatic(self, *, include_partial: bool) -> None:
        async with self._flush_lock:
            # An explicit flush may have failed while this worker waited for
            # the lock. Its new deadline also governs this automatic attempt.
            if not self._automatic_retry_ready():
                return
            with self._buffer_lock:
                include_partial = include_partial or self._retry_deadline_ns is not None
            while self.buffered_count:
                if not self._automatic_retry_ready():
                    return
                with self._buffer_lock:
                    queued = len(self._buffer)
                    queued_bytes = self._queued_bytes
                if (
                    not include_partial
                    and queued < self.flush_batch_size
                    and queued_bytes < self.flush_batch_bytes
                ):
                    return
                result = await self._write_one_batch()
                if not result.successful:
                    return

    async def _check_sink_health(self, *, automatic: bool = False) -> bool:
        # Startup probes share the write lock too, so a slow probe cannot
        # race a write and overwrite its recovery state.
        async with self._flush_lock:
            if automatic and (not self._automatic_retry_ready() or self.buffered_count):
                return True
            checker = getattr(self.sink, "health_check", None)
            try:
                if checker is not None:
                    await checker()
            except Exception as exc:
                self._record_failure(exc, self.buffered_count)
                return False
            with self._buffer_lock:
                wake_worker = False
                if not self._buffer and not self._inflight:
                    wake_worker = self._reset_retry_locked()
            if wake_worker:
                self._signal_worker()
            return True

    def _reset_retry_locked(self) -> bool:
        """Clear recovery while holding the buffer lock; report a changed wait."""
        had_deadline = self._retry_deadline_ns is not None
        self._consecutive_failures = 0
        self._current_retry_delay_ms = self.retry_backoff_ms
        self._retry_deadline_ns = None
        self._health_state = PersistenceHealthState.HEALTHY
        return had_deadline

    def _automatic_wait_seconds(self) -> float:
        with self._buffer_lock:
            if self._retry_deadline_ns is None:
                return self.flush_interval_ms / 1_000
            return max(0, self._retry_deadline_ns - time.monotonic_ns()) / 1_000_000_000

    async def _worker_loop(self) -> None:
        wake_event = self._wake_event
        if wake_event is None:
            raise RuntimeError("Metrics worker started without a wake event")
        try:
            while not self._stop_worker:
                timeout = self._automatic_wait_seconds()
                timed_out = timeout == 0
                if timeout > 0:
                    try:
                        await asyncio.wait_for(wake_event.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        timed_out = True
                wake_event.clear()
                if self._stop_worker:
                    break
                if not self._automatic_retry_ready():
                    continue

                with self._buffer_lock:
                    retrying = self._retry_deadline_ns is not None
                    health_state = self._health_state

                if self.buffered_count:
                    self._drain_requested, requested = False, self._drain_requested
                    await self._flush_automatic(
                        include_partial=timed_out or retrying or requested,
                    )
                elif health_state in {
                    PersistenceHealthState.RETRYING,
                    PersistenceHealthState.DEGRADED,
                    PersistenceHealthState.UNAVAILABLE,
                }:
                    await self._check_sink_health(automatic=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Metrics flush worker failed")
            with self._buffer_lock:
                self._health_state = PersistenceHealthState.UNAVAILABLE

    async def close(self) -> PersistenceCloseResult:
        """Seal admission, make one final drain attempt, and close the sink."""
        async with self._close_lock:
            if self._closed:
                previous = self._last_close_result
                return PersistenceCloseResult(
                    PersistenceCloseOutcome.ALREADY_CLOSED,
                    records_written=0,
                    unflushed_records=(
                        previous.unflushed_records
                        if previous is not None
                        else self.buffered_count
                    ),
                    error=(previous.error if previous is not None else None),
                )
            with self._buffer_lock:
                self._closing = True
                self._stop_worker = True
                initial_unflushed_records = len(self._buffer) + len(self._inflight)
            self._signal_worker()
            self._notify_progress()

            async def _finalize() -> PersistenceCloseResult:
                worker = self._worker_task
                if worker is not None and not worker.done():
                    await worker
                flush_result = await self.flush()
                if not flush_result.successful:
                    return PersistenceCloseResult(
                        PersistenceCloseOutcome.CLOSED_WITH_UNFLUSHED,
                        records_written=flush_result.records_written,
                        unflushed_records=flush_result.remaining_records,
                        error=flush_result.error,
                    )
                try:
                    await self.sink.flush()
                    if not self._sink_closed:
                        await self.sink.close()
                        self._sink_closed = True
                except Exception as exc:
                    self._record_failure(exc, self.buffered_count)
                    return PersistenceCloseResult(
                        PersistenceCloseOutcome.SINK_CLOSE_FAILED,
                        records_written=flush_result.records_written,
                        unflushed_records=self.buffered_count,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                return PersistenceCloseResult(
                    PersistenceCloseOutcome.CLOSED,
                    records_written=flush_result.records_written,
                    unflushed_records=0,
                )

            try:
                result = await asyncio.wait_for(
                    _finalize(),
                    timeout=self.shutdown_timeout_ms / 1_000,
                )
            except asyncio.TimeoutError:
                worker = self._worker_task
                if worker is not None and not worker.done():
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass
                result = PersistenceCloseResult(
                    PersistenceCloseOutcome.TIMED_OUT,
                    records_written=max(
                        0,
                        initial_unflushed_records - self.buffered_count,
                    ),
                    unflushed_records=self.buffered_count,
                    error="Persistence shutdown timed out",
                )

            with self._buffer_lock:
                self._closed = True
                self._closing = False
                self._health_state = PersistenceHealthState.CLOSED
                self._last_close_result = result
            return result

    async def replace_sink(
        self,
        sink: MetricsSink,
    ) -> PersistenceFlushResult:
        """Flush and close the current sink before replacing it."""
        async with self._close_lock:
            if self._closed:
                raise RuntimeError("Cannot replace a sink on a closed writer")
            result = await self.flush()
            if not result.successful:
                return result
            try:
                await self.sink.flush()
                await self.sink.close()
            except Exception as exc:
                self._record_failure(exc, self.buffered_count)
                return PersistenceFlushResult(
                    PersistenceFlushOutcome.FAILED,
                    records_written=result.records_written,
                    remaining_records=self.buffered_count,
                    error=f"{type(exc).__name__}: {exc}",
                )
            self.sink = sink
            self._sink_closed = False
            await self._check_sink_health()
            return result

    def _record_successful_write(self) -> None:
        with self._buffer_lock:
            self._last_successful_write_time = datetime.now(timezone.utc)
            wake_worker = self._reset_retry_locked()
        if wake_worker:
            self._signal_worker()

    def _record_failure(self, exc: BaseException, buffer_size: int) -> None:
        with self._buffer_lock:
            self._lifetime_failures += 1
            self._consecutive_failures += 1
            self._last_failure_time = datetime.now(timezone.utc)
            self._current_retry_delay_ms = (
                self.retry_backoff_ms
                if self._consecutive_failures == 1
                else min(self._current_retry_delay_ms * 2, self.max_retry_delay_ms)
            )
            self._retry_deadline_ns = (
                time.monotonic_ns() + self._current_retry_delay_ms * 1_000_000
            )
            self._health_state = (
                PersistenceHealthState.UNAVAILABLE
                if self._current_retry_delay_ms >= self.max_retry_delay_ms
                else PersistenceHealthState.RETRYING
            )
        self._signal_worker()
        if self.on_failure is not None:
            try:
                self.on_failure(exc, buffer_size)
            except Exception:
                logger.exception("Metrics failure observer failed")

    def health_snapshot(self) -> PersistenceHealthSnapshot:
        now_ns = time.monotonic_ns()
        with self._buffer_lock:
            oldest_entries = tuple(self._inflight[:1]) + tuple(self._buffer)[:1]
            oldest_age = (
                max(
                    0.0,
                    (
                        now_ns
                        - min(entry.enqueued_monotonic_ns for entry in oldest_entries)
                    )
                    / 1_000_000_000,
                )
                if oldest_entries
                else None
            )
            close_outcome = (
                self._last_close_result.outcome
                if self._last_close_result is not None
                else None
            )
            return PersistenceHealthSnapshot(
                state=self._health_state,
                last_successful_write_time=self._last_successful_write_time,
                last_failure_time=self._last_failure_time,
                consecutive_failures=self._consecutive_failures,
                lifetime_failures=self._lifetime_failures,
                buffered_records=len(self._buffer) + len(self._inflight),
                buffered_bytes=self._buffered_bytes,
                oldest_buffered_record_age_seconds=oldest_age,
                accepted_records=(
                    self._outcomes[PersistenceAddOutcome.ACCEPTED]
                    + self._outcomes[PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED]
                ),
                flush_required_records=self._outcomes[
                    PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED
                ],
                dropped_records=self._dropped_records,
                rejected_closed_records=self._outcomes[
                    PersistenceAddOutcome.REJECTED_CLOSED
                ],
                rejected_full_records=self._outcomes[
                    PersistenceAddOutcome.REJECTED_FULL
                ],
                failed_records=sum(
                    self._outcomes[outcome]
                    for outcome in (
                        PersistenceAddOutcome.FAILED,
                        PersistenceAddOutcome.TIMED_OUT,
                        PersistenceAddOutcome.TOO_LARGE,
                    )
                ),
                worker_running=(
                    self._worker_task is not None and not self._worker_task.done()
                ),
                close_outcome=close_outcome,
                unflushed_on_close=(
                    self._last_close_result.unflushed_records
                    if self._last_close_result is not None
                    else 0
                ),
            )

    async def __aenter__(self) -> "BufferedMetricsWriter":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()


class MetricsCollector:
    """Thread-safe event-owned process metrics with an isolated registry."""

    def __init__(
        self,
        exchange_name: str,
        *,
        instruments: InstrumentCatalog,
        persistence: Optional[BufferedMetricsWriter] = None,
        prometheus_enabled: bool = True,
        on_integrity_failure: Optional[Callable[[], None]] = None,
    ) -> None:
        self.exchange_name = exchange_name
        self.instruments = instruments
        self.persistence = persistence
        self._fix_message_counts: dict[tuple[str, str], int] = {}
        self._run_id: Optional[str] = None
        self._run_ended = False
        self._metric_event_sequence = 0
        self._last_event_timestamp_ns: Optional[int] = None
        self._persistence_run_baseline: Dict[str, int] = {}
        self._persistence_integrity_failure: Optional[PersistenceIntegrityFailure] = (
            None
        )
        self.on_integrity_failure = on_integrity_failure
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
            "cancel_exchange_unavailable": 0,
            "execution_reports": 0,
            "report_delivery_failures": 0,
            "connected_sessions": 0,
            "trades": 0,
            "invalid_latency_measurements": 0,
            "metric_sink_failures": 0,
            "self_trade_preventions": 0,
            "scheduler_dead_letters": 0,
            "scheduler_event_processing_limit_failures": 0,
        }
        self._lifetime_counts = dict(self._counts)
        self._symbol_totals: Dict[str, Dict[str, int]] = {
            symbol: {
                "traded_qty_lots": 0,
                "traded_notional_tick_lots": 0,
                "canceled_qty_lots": 0,
            }
            for symbol in instruments.symbols
        }
        self._lifetime_symbol_totals: Dict[str, Dict[str, int]] = {
            symbol: dict(totals) for symbol, totals in self._symbol_totals.items()
        }
        self._latency_sums = {name: 0.0 for name in LATENCY_METRICS}
        self._latency_counts = {name: 0 for name in LATENCY_METRICS}
        self._lifetime_latency_sums = {name: 0.0 for name in LATENCY_METRICS}
        self._lifetime_latency_counts = {name: 0 for name in LATENCY_METRICS}
        self._submitted_order_ids: set[int] = set()
        self._accepted_order_ids: set[int] = set()
        self._filled_order_ids: set[int] = set()
        self._seen_report_exec_ids: set[str] = set()
        self._seen_cancel_reject_ids: set[str] = set()
        self._seen_trade_ids: set[str] = set()
        self._itch_counts: Dict[str, int] = {
            "itch_sessions_started": 0,
            "itch_sessions_ended": 0,
            "itch_messages_sent": 0,
            "moldudp64_packets_sent": 0,
            "moldudp64_heartbeats_sent": 0,
            "itch_failures": 0,
        }
        self._itch_state = "disabled"
        self._itch_next_sequence_number = 1
        self._glimpse_counts: Dict[str, int] = {
            "glimpse_connections_accepted": 0,
            "glimpse_fresh_sessions": 0,
            "glimpse_resumed_sessions": 0,
            "glimpse_completed_sessions": 0,
            "glimpse_rejected_sessions": 0,
            "glimpse_sequenced_messages": 0,
            "glimpse_sequenced_bytes": 0,
            "glimpse_heartbeats_sent": 0,
            "glimpse_timeouts": 0,
            "glimpse_failures": 0,
        }
        self._glimpse_state = "disabled"
        self._glimpse_active_connections = 0
        self._glimpse_cached_sessions = 0
        self._glimpse_cached_bytes = 0

        self.registry = None
        self._prom_counters: Dict[str, Any] = {}
        self._prom_gauges: Dict[str, Any] = {}
        self._prom_histograms: Dict[str, Any] = {}
        self._prom_delivery_state_gauge = None
        self._prom_persistence_state_gauge = None
        self._prom_persistence_outcomes = None
        self._prom_self_trade_preventions = None
        self._prom_self_trade_prevented_quantity = None
        if prometheus_enabled and CollectorRegistry is not None:
            self._init_prometheus()

        if self.persistence is not None:
            self.persistence.on_failure = self.record_sink_failure
            self.persistence.on_outcome = self.record_persistence_outcome

    @property
    def run_id(self) -> Optional[str]:
        with self._lock:
            return self._run_id

    @property
    def integrity_complete(self) -> bool:
        with self._lock:
            return self._persistence_integrity_failure is None

    @property
    def persistence_integrity_failure(self) -> Optional[PersistenceIntegrityFailure]:
        with self._lock:
            return self._persistence_integrity_failure

    def start_run(self, run_id: str) -> None:
        """Start one persistence identity scope and reset its sequence."""
        if not run_id:
            raise ValueError("Metric run_id is required")
        with self._lock:
            if self._run_id is not None:
                raise RuntimeError("A metric persistence run is already active")
            self._run_id = run_id
            self._run_ended = False
            self._metric_event_sequence = 0
            self._last_event_timestamp_ns = None
            self._persistence_run_baseline = self._persistence_totals_locked()
            self._persistence_integrity_failure = None

    def end_run(self) -> None:
        """Clear the active run after its immutable records have flushed."""
        with self._lock:
            self._run_id = None
            self._run_ended = True
        self.reset_run_metrics()
        for name in ("active_orders", "resting_orders", "order_book_levels"):
            gauge = self._prom_gauges.get(name)
            if gauge is not None:
                gauge.set(0)

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
            for totals in self._symbol_totals.values():
                for name in totals:
                    totals[name] = 0
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
        persistence.on_outcome = self.record_persistence_outcome

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
            "cancel_exchange_unavailable": "Cancels rejected for a failed run.",
            "execution_reports": "Order-specific execution reports created.",
            "report_delivery_failures": "Reports not delivered to their destination.",
            "trades": "Immutable fills committed by the engine.",
            "traded_quantity": "Quantity traded once per fill.",
            "traded_notional": "Price times quantity summed once per fill.",
            "canceled_quantity": "Unexecuted quantity canceled successfully.",
            "invalid_latency_measurements": "Latency samples rejected as invalid.",
            "metric_sink_failures": "Metric persistence operations that failed.",
            "scheduler_dead_letters": "Scheduler events retained as dead letters.",
            "scheduler_event_processing_limit_failures": (
                "Scheduler advances stopped by the event-processing limit."
            ),
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
            "scheduler_pending_events": "Current scheduled event queue depth.",
            "scheduler_failed": "Whether the scheduler is in FAILED state.",
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
                "fix_delivery_retries",
                "mockx_fix_delivery_retries_total",
                "FIX delivery attempts after the first attempt.",
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
                "itch_sessions_started",
                "mockx_itch_sessions_started_total",
                "Run-scoped ITCH sessions successfully started.",
            ),
            (
                "itch_sessions_ended",
                "mockx_itch_sessions_ended_total",
                "Run-scoped ITCH sessions ended normally.",
            ),
            (
                "itch_messages_sent",
                "mockx_itch_messages_sent_total",
                "ITCH messages submitted to downstream MoldUDP64 packets.",
            ),
            (
                "moldudp64_packets_sent",
                "mockx_moldudp64_packets_sent_total",
                "MoldUDP64 data, heartbeat, and end packets submitted.",
            ),
            (
                "moldudp64_heartbeats_sent",
                "mockx_moldudp64_heartbeats_sent_total",
                "Idle MoldUDP64 heartbeat packets submitted.",
            ),
            (
                "itch_failures",
                "mockx_itch_failures_total",
                "Runtime failures that degraded an ITCH session.",
            ),
        ):
            self._prom_counters[name] = Counter(
                metric_name,
                help_text,
                registry=self.registry,
            )
        self._prom_gauges["itch_session_active"] = Gauge(
            "mockx_itch_session_active",
            "Whether an ITCH session is active and coherent.",
            registry=self.registry,
        )
        self._prom_gauges["itch_degraded"] = Gauge(
            "mockx_itch_degraded",
            "Whether ITCH delivery is degraded for the current run.",
            registry=self.registry,
        )
        self._prom_gauges["moldudp64_next_sequence_number"] = Gauge(
            "mockx_moldudp64_next_sequence_number",
            "Next ITCH message sequence expected in the Mold session.",
            registry=self.registry,
        )
        for name, metric_name, help_text in (
            (
                "glimpse_connections_accepted",
                "mockx_glimpse_connections_accepted_total",
                "SoupBinTCP client connections admitted by GLIMPSE.",
            ),
            (
                "glimpse_fresh_sessions",
                "mockx_glimpse_fresh_sessions_total",
                "Fresh point-in-time GLIMPSE spins created.",
            ),
            (
                "glimpse_resumed_sessions",
                "mockx_glimpse_resumed_sessions_total",
                "Interrupted GLIMPSE spins resumed from cache.",
            ),
            (
                "glimpse_completed_sessions",
                "mockx_glimpse_completed_sessions_total",
                "GLIMPSE spins completed with Soup End of Session.",
            ),
            (
                "glimpse_rejected_sessions",
                "mockx_glimpse_rejected_sessions_total",
                "GLIMPSE login requests rejected or refused by capacity.",
            ),
            (
                "glimpse_sequenced_messages",
                "mockx_glimpse_sequenced_messages_total",
                "Higher-level GLIMPSE messages sent in Soup sequenced packets.",
            ),
            (
                "glimpse_sequenced_bytes",
                "mockx_glimpse_sequenced_bytes_total",
                "Wire bytes successfully drained in Soup sequenced packets.",
            ),
            (
                "glimpse_heartbeats_sent",
                "mockx_glimpse_heartbeats_sent_total",
                "Idle SoupBinTCP server heartbeats sent.",
            ),
            (
                "glimpse_timeouts",
                "mockx_glimpse_timeouts_total",
                "GLIMPSE login, client-idle, write, or shutdown timeouts.",
            ),
            (
                "glimpse_failures",
                "mockx_glimpse_failures_total",
                "Unexpected GLIMPSE listener, snapshot, or connection failures.",
            ),
        ):
            self._prom_counters[name] = Counter(
                metric_name,
                help_text,
                registry=self.registry,
            )
        for name, metric_name, help_text in (
            (
                "glimpse_active_connections",
                "mockx_glimpse_active_connections",
                "Currently admitted SoupBinTCP connections.",
            ),
            (
                "glimpse_cached_sessions",
                "mockx_glimpse_cached_sessions",
                "Immutable GLIMPSE spins retained for active delivery or resume.",
            ),
            (
                "glimpse_cached_bytes",
                "mockx_glimpse_cached_bytes",
                "Soup sequenced wire bytes retained in the GLIMPSE replay cache.",
            ),
            (
                "glimpse_ready",
                "mockx_glimpse_ready",
                "Whether the process-lifetime GLIMPSE listener is ready.",
            ),
            (
                "glimpse_degraded",
                "mockx_glimpse_degraded",
                "Whether the GLIMPSE listener failed to bind or run.",
            ),
        ):
            self._prom_gauges[name] = Gauge(
                metric_name,
                help_text,
                registry=self.registry,
            )
        for name, help_text in {
            "persistence_buffer_records": "Accepted buffered and in-flight records.",
            "persistence_buffer_bytes": (
                "Accepted buffered and in-flight logical bytes."
            ),
            "persistence_oldest_buffered_record_age_seconds": (
                "Wall-clock age of the oldest accepted persistence record."
            ),
            "persistence_consecutive_failures": (
                "Consecutive sink failures since the last successful probe/write."
            ),
            "persistence_lifetime_failures": "Process-lifetime sink failures.",
            "persistence_dropped_records": "Process-lifetime overflow evictions.",
            "persistence_rejected_records": "Process-lifetime rejected additions.",
        }.items():
            self._prom_gauges[name] = Gauge(
                f"mockx_{name}",
                help_text,
                registry=self.registry,
            )
        self._prom_persistence_state_gauge = Gauge(
            "mockx_persistence_state",
            "Current persistence health state (exactly one state is 1).",
            ("state",),
            registry=self.registry,
        )
        self._prom_persistence_outcomes = Counter(
            "mockx_persistence_add_outcomes_total",
            "Persistence additions by fixed outcome enum.",
            ("outcome",),
            registry=self.registry,
        )
        self._prom_self_trade_preventions = Counter(
            "mockx_self_trade_preventions_total",
            "Same-participant crossings canceled before trade creation.",
            ("symbol",),
            registry=self.registry,
        )
        self._prom_self_trade_prevented_quantity = Counter(
            "mockx_self_trade_prevented_quantity_total",
            "Prevented self-trade quantity in display units.",
            ("symbol",),
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
            SIM_REPLACE_INBOUND_LATENCY: "Simulated replace exchange arrival minus FIX receive.",
            SIM_REPLACE_ENGINE_LATENCY: "Simulated replace report creation minus arrival.",
            SIM_REPLACE_OUTBOUND_LATENCY: "Simulated replace delivery minus creation.",
            SIM_REPLACE_END_TO_END_LATENCY: "Simulated replace delivery minus FIX receive.",
            MATCH_LOOP_DURATION: "Monotonic duration of the match loop.",
            BOOK_COMMIT_DURATION: (
                "Monotonic duration of authoritative book mutations."
            ),
            ENGINE_COMMAND_DURATION: (
                "Monotonic duration of one non-noop engine command."
            ),
            REPORT_CREATION_DURATION: (
                "Monotonic duration of immutable report construction."
            ),
            SERIALIZATION_DURATION: (
                "Monotonic duration of outbound FIX construction."
            ),
        }
        for metric_name, help_text in duration_help.items():
            buckets = (
                (
                    0.001,
                    0.005,
                    0.01,
                    0.05,
                    0.1,
                    0.5,
                    1.0,
                    5.0,
                    10.0,
                    50.0,
                    100.0,
                    500.0,
                    1_000.0,
                )
                if metric_name in SIMULATED_LATENCY_METRICS
                else (
                    1.0,
                    5.0,
                    10.0,
                    50.0,
                    100.0,
                    500.0,
                    1_000.0,
                    5_000.0,
                    10_000.0,
                    50_000.0,
                    100_000.0,
                    500_000.0,
                    1_000_000.0,
                )
            )
            self._prom_histograms[metric_name] = Histogram(
                metric_name,
                help_text,
                registry=self.registry,
                buckets=buckets,
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
        return True

    def record_order(self, report: ExecutionReport) -> bool:
        """Count one immutable report; the native journal owns persistence."""
        return self.record_execution_report(report)

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
                report.order_id > 0 and report.order_id not in self._accepted_order_ids
            ):
                self._accepted_order_ids.add(report.order_id)
                self._increment_locked("orders_accepted")

            if report.exec_type == ExecutionType.CANCELED:
                self._increment_locked("orders_canceled")
                self._symbol_totals[report.instrument.symbol][
                    "canceled_qty_lots"
                ] += report.canceled_qty_lots
                self._lifetime_symbol_totals[report.instrument.symbol][
                    "canceled_qty_lots"
                ] += report.canceled_qty_lots
                canceled_display = report.instrument.quantity_decimal(
                    report.canceled_qty_lots
                )
                self._prom_increment(
                    "canceled_quantity",
                    float(canceled_display),
                )
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
        return True

    def record_trade(self, trade: Trade) -> bool:
        """Count each immutable fill/trade record exactly once."""
        identity = (
            trade.exchange_trade_id or f"G{trade.run_generation:08d}-T{trade.id:08d}"
        )
        with self._lock:
            if identity in self._seen_trade_ids:
                return False
            self._seen_trade_ids.add(identity)
            self._increment_locked("trades")
            totals = self._symbol_totals[trade.symbol]
            lifetime_totals = self._lifetime_symbol_totals[trade.symbol]
            totals["traded_qty_lots"] += trade.qty_lots
            totals["traded_notional_tick_lots"] += trade.notional_tick_lots
            lifetime_totals["traded_qty_lots"] += trade.qty_lots
            lifetime_totals["traded_notional_tick_lots"] += trade.notional_tick_lots
            instrument = self.instruments.require(trade.symbol)
            self._prom_increment(
                "traded_quantity",
                float(instrument.quantity_decimal(trade.qty_lots)),
            )
            self._prom_increment(
                "traded_notional",
                float(instrument.format_notional(trade.notional_tick_lots)),
            )

        return True

    def record_self_trade_prevention(
        self,
        event: SelfTradePrevention,
    ) -> None:
        """Record one unconditional self-cross prevention."""
        instrument = self.instruments.require(event.symbol)
        with self._lock:
            self._counts["self_trade_preventions"] += 1
            if self._prom_self_trade_preventions is not None:
                self._prom_self_trade_preventions.labels(symbol=event.symbol).inc()
            if self._prom_self_trade_prevented_quantity is not None:
                self._prom_self_trade_prevented_quantity.labels(
                    symbol=event.symbol
                ).inc(float(instrument.quantity_decimal(event.prevented_qty_lots)))

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
            CancellationOutcome.STALE_RUN_GENERATION: ("cancel_stale_run_generation"),
            CancellationOutcome.EXCHANGE_UNAVAILABLE: "cancel_exchange_unavailable",
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

    def record_fix_delivery_retry(self) -> None:
        self._prom_increment("fix_delivery_retries", 1)

    def record_fix_delivery_failure(self, *, permanent: bool = False) -> None:
        self._prom_increment("fix_delivery_failures", 1)
        if permanent:
            self._prom_increment("fix_delivery_permanent_failures", 1)

    def record_fix_delivery_success(self) -> None:
        self._prom_increment("fix_delivery_successes", 1)

    def update_scheduler_pending(self, count: int) -> None:
        gauge = self._prom_gauges.get("scheduler_pending_events")
        if gauge is not None:
            gauge.set(max(0, count))

    def update_scheduler_state(self, state: SchedulerState) -> None:
        gauge = self._prom_gauges.get("scheduler_failed")
        if gauge is not None:
            gauge.set(1 if state == SchedulerState.FAILED else 0)

    def record_scheduler_failure(self, failure: SchedulerFailure) -> None:
        with self._lock:
            self._increment_locked("scheduler_dead_letters")
            if failure.status == SchedulerFailureStatus.EVENT_PROCESSING_LIMIT_EXCEEDED:
                self._increment_locked("scheduler_event_processing_limit_failures")
        self.update_scheduler_state(SchedulerState.FAILED)

    def update_fix_delivery_gauges(
        self,
        snapshot: FixDeliveryQueueSnapshot,
    ) -> None:
        depth = self._prom_gauges.get("fix_delivery_queue_depth")
        if depth is not None:
            depth.set(snapshot.queue_depth)
        age = self._prom_gauges.get("fix_delivery_oldest_pending_age_seconds")
        if age is not None:
            age.set(snapshot.oldest_pending_age_seconds or 0.0)
        if self._prom_delivery_state_gauge is not None:
            state_counts = {
                FixDeliveryState.PENDING: snapshot.pending,
                FixDeliveryState.DUE: snapshot.due,
                FixDeliveryState.RETRY_PENDING: snapshot.retry_pending,
                FixDeliveryState.DELIVERED: snapshot.delivered,
                FixDeliveryState.PERMANENTLY_FAILED: (snapshot.permanently_failed),
            }
            for state, count in state_counts.items():
                self._prom_delivery_state_gauge.labels(state=state.value).set(count)

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
        expected_unit = (
            "ms" if measurement.metric in SIMULATED_LATENCY_METRICS else "us"
        )
        if (
            measurement.unit != expected_unit
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
            self._lifetime_latency_sums[measurement.metric] += measurement.value
            self._lifetime_latency_counts[measurement.metric] += 1
            histogram = self._prom_histograms.get(measurement.metric)
            if histogram is not None:
                histogram.observe(measurement.value)
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

    def record_persistence_outcome(
        self,
        outcome: PersistenceAddOutcome,
        amount: int = 1,
    ) -> None:
        if self._prom_persistence_outcomes is not None:
            self._prom_persistence_outcomes.labels(outcome=outcome.value).inc(amount)
        self.refresh_persistence_gauges()

    def refresh_persistence_gauges(
        self,
    ) -> PersistenceHealthSnapshot:
        health = self.persistence_health_snapshot()
        values = {
            "persistence_buffer_records": health.buffered_records,
            "persistence_buffer_bytes": health.buffered_bytes,
            "persistence_oldest_buffered_record_age_seconds": (
                health.oldest_buffered_record_age_seconds or 0.0
            ),
            "persistence_consecutive_failures": health.consecutive_failures,
            "persistence_lifetime_failures": health.lifetime_failures,
            "persistence_dropped_records": health.dropped_records,
            "persistence_rejected_records": (
                health.rejected_closed_records
                + health.rejected_full_records
                + health.failed_records
            ),
        }
        for name, value in values.items():
            gauge = self._prom_gauges.get(name)
            if gauge is not None:
                gauge.set(value)
        if self._prom_persistence_state_gauge is not None:
            for state in PersistenceHealthState:
                self._prom_persistence_state_gauge.labels(state=state.value).set(
                    1 if health.state == state else 0
                )
        return health

    def _record_itch_count(self, name: str, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("ITCH metric increments cannot be negative")
        with self._lock:
            self._itch_counts[name] += amount
            self._prom_increment(name, amount)

    def update_itch_state(self, state: str, next_sequence_number: int) -> None:
        with self._lock:
            self._itch_state = state
            self._itch_next_sequence_number = next_sequence_number
            active = self._prom_gauges.get("itch_session_active")
            degraded = self._prom_gauges.get("itch_degraded")
            sequence = self._prom_gauges.get("moldudp64_next_sequence_number")
            if active is not None:
                active.set(1 if state == "ready" else 0)
            if degraded is not None:
                degraded.set(1 if state == "degraded" else 0)
            if sequence is not None:
                sequence.set(next_sequence_number)

    def record_itch_session_started(self, next_sequence_number: int) -> None:
        self._record_itch_count("itch_sessions_started")
        self.update_itch_state("ready", next_sequence_number)

    def record_itch_session_ended(self) -> None:
        self._record_itch_count("itch_sessions_ended")

    def record_itch_messages(
        self,
        messages: int,
        packets: int,
        next_sequence_number: int,
    ) -> None:
        self._record_itch_count("itch_messages_sent", messages)
        self._record_itch_count("moldudp64_packets_sent", packets)
        self.update_itch_state(self._itch_state, next_sequence_number)

    def record_itch_heartbeat(self, next_sequence_number: int) -> None:
        self._record_itch_count("moldudp64_packets_sent")
        self._record_itch_count("moldudp64_heartbeats_sent")
        self.update_itch_state(self._itch_state, next_sequence_number)

    def record_itch_end_packet(self, next_sequence_number: int) -> None:
        self._record_itch_count("moldudp64_packets_sent")
        self.update_itch_state(self._itch_state, next_sequence_number)

    def record_itch_failure(self, _error: str) -> None:
        self._record_itch_count("itch_failures")

    def itch_metrics_snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                **self._itch_counts,
                "state": self._itch_state,
                "next_sequence_number": self._itch_next_sequence_number,
            }

    def record_glimpse_count(self, name: str, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("GLIMPSE metric increments cannot be negative")
        key = f"glimpse_{name}"
        with self._lock:
            if key not in self._glimpse_counts:
                raise KeyError(f"Unknown GLIMPSE metric: {name}")
            self._glimpse_counts[key] += amount
            self._prom_increment(key, amount)

    def update_glimpse_state(self, state: str) -> None:
        with self._lock:
            self._glimpse_state = state
            ready = self._prom_gauges.get("glimpse_ready")
            degraded = self._prom_gauges.get("glimpse_degraded")
            if ready is not None:
                ready.set(1 if state == "ready" else 0)
            if degraded is not None:
                degraded.set(1 if state == "degraded" else 0)

    def update_glimpse_active_connections(self, active: int) -> None:
        if active < 0:
            raise ValueError("GLIMPSE active connections cannot be negative")
        with self._lock:
            self._glimpse_active_connections = active
            gauge = self._prom_gauges.get("glimpse_active_connections")
            if gauge is not None:
                gauge.set(active)

    def update_glimpse_cache(self, sessions: int, wire_bytes: int) -> None:
        if sessions < 0 or wire_bytes < 0:
            raise ValueError("GLIMPSE cache gauges cannot be negative")
        with self._lock:
            self._glimpse_cached_sessions = sessions
            self._glimpse_cached_bytes = wire_bytes
            session_gauge = self._prom_gauges.get("glimpse_cached_sessions")
            byte_gauge = self._prom_gauges.get("glimpse_cached_bytes")
            if session_gauge is not None:
                session_gauge.set(sessions)
            if byte_gauge is not None:
                byte_gauge.set(wire_bytes)

    def glimpse_metrics_snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                **self._glimpse_counts,
                "state": self._glimpse_state,
                "active_connections": self._glimpse_active_connections,
                "cached_sessions": self._glimpse_cached_sessions,
                "cached_bytes": self._glimpse_cached_bytes,
            }

    def record_fix_message(
        self, msg_type: str, direction: str, timestamp_ns: int
    ) -> None:
        """FIX message receipt accounting remains an operational concern."""
        with self._lock:
            key = (msg_type, direction)
            self._fix_message_counts[key] = self._fix_message_counts.get(key, 0) + 1

    def record_fix_connection(self, count: int, timestamp_ns: int) -> None:
        self.set_connected_sessions(max(0, count))

    def update_active_orders(self, update: MarketDataSnapshot) -> None:
        """Refresh gauges derived from authoritative engine state."""
        self.refresh_engine_gauges()

    def update_pending_events(self, count: int, timestamp_ns: int) -> None:
        self.update_scheduler_pending(count)

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

    def persistence_health_snapshot(self) -> PersistenceHealthSnapshot:
        if self.persistence is not None:
            return self.persistence.health_snapshot()
        return PersistenceHealthSnapshot(
            state=PersistenceHealthState.DISABLED,
            last_successful_write_time=None,
            last_failure_time=None,
            consecutive_failures=0,
            lifetime_failures=0,
            buffered_records=0,
            buffered_bytes=0,
            oldest_buffered_record_age_seconds=None,
            accepted_records=0,
            flush_required_records=0,
            dropped_records=0,
            rejected_closed_records=0,
            rejected_full_records=0,
            failed_records=0,
            worker_running=False,
            close_outcome=None,
            unflushed_on_close=0,
        )

    def _persistence_totals_locked(self) -> Dict[str, int]:
        health = self.persistence_health_snapshot()
        return {
            "accepted_records": health.accepted_records,
            "flush_required_records": health.flush_required_records,
            "dropped_records": health.dropped_records,
            "rejected_closed_records": health.rejected_closed_records,
            "rejected_full_records": health.rejected_full_records,
            "failed_records": health.failed_records,
            "lifetime_failures": health.lifetime_failures,
        }

    def run_persistence_snapshot(self) -> Dict[str, int | bool]:
        current = self._persistence_totals_locked()
        with self._lock:
            baseline = dict(self._persistence_run_baseline)
            integrity_complete = self._persistence_integrity_failure is None
        outcomes: Dict[str, int | bool] = {
            name: max(0, value - baseline.get(name, 0))
            for name, value in current.items()
        }
        outcomes["failures"] = outcomes.pop("lifetime_failures")
        outcomes["integrity_complete"] = integrity_complete
        return outcomes

    def current_run_scheduler_snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "dead_letter_count": self._counts["scheduler_dead_letters"],
                "event_processing_limit_failures": self._counts[
                    "scheduler_event_processing_limit_failures"
                ],
            }

    def _physical_totals(
        self,
        symbol_totals: Mapping[str, Mapping[str, int]],
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Convert integer aggregate totals exactly, without per-order bounds."""
        quantities = []
        notionals = []
        canceled_quantities = []
        for symbol, totals in symbol_totals.items():
            instrument = self.instruments.require(symbol)
            quantities.append(instrument.quantity_decimal(totals["traded_qty_lots"]))
            notionals.append(
                instrument.notional_from_tick_lots(totals["traded_notional_tick_lots"])
            )
            canceled_quantities.append(
                instrument.quantity_decimal(totals["canceled_qty_lots"])
            )
        return (
            decimal_sum(quantities),
            decimal_sum(notionals),
            decimal_sum(canceled_quantities),
        )

    def process_lifetime_snapshot(self) -> Dict[str, object]:
        """Return cumulative process counters without current-state gauges."""
        with self._lock:
            counts = dict(self._lifetime_counts)
            symbol_totals = {
                symbol: dict(values)
                for symbol, values in self._lifetime_symbol_totals.items()
            }
            latency_sums = dict(self._lifetime_latency_sums)
            latency_counts = dict(self._lifetime_latency_counts)

        def average(metric: str, multiplier: float) -> Optional[float]:
            count = latency_counts[metric]
            if count == 0:
                return None
            return latency_sums[metric] / count * multiplier

        traded_quantity, traded_notional, canceled_quantity = self._physical_totals(
            symbol_totals
        )

        return {
            "scope": "process_lifetime",
            "orders": {
                "submitted": counts["orders_submitted"],
                "accepted": counts["orders_accepted"],
                "rejected": counts["orders_rejected"],
                "canceled": counts["orders_canceled"],
                "filled": counts["orders_filled"],
                "partial_fill_events": counts["partial_fill_events"],
                "cancel_requests": counts["cancel_requests"],
                "cancel_rejects": counts["cancel_rejects"],
            },
            "trades": {
                "count": counts["trades"],
                "quantity": format(traded_quantity, "f"),
                "notional": format(traded_notional, "f"),
                "canceled_quantity": format(canceled_quantity, "f"),
            },
            "cancellations": {
                "accepted": counts["cancel_accepted"],
                "unknown_order": counts["cancel_unknown_order"],
                "already_terminal": counts["cancel_already_terminal"],
                "wrong_owner": counts["cancel_wrong_owner"],
                "duplicate_request": counts["cancel_duplicate_request"],
                "stale_run_generation": counts["cancel_stale_run_generation"],
                "exchange_unavailable": counts["cancel_exchange_unavailable"],
            },
            "reports": {
                "created": counts["execution_reports"],
                "delivery_failures": counts["report_delivery_failures"],
            },
            "latency": {
                "average_match_loop_duration_us": average(MATCH_LOOP_DURATION, 1.0),
                "average_book_commit_duration_us": average(BOOK_COMMIT_DURATION, 1.0),
                "average_engine_command_duration_us": average(
                    ENGINE_COMMAND_DURATION, 1.0
                ),
                "average_report_creation_duration_us": average(
                    REPORT_CREATION_DURATION, 1.0
                ),
                "average_serialization_duration_us": average(
                    SERIALIZATION_DURATION, 1.0
                ),
                "average_sim_inbound_latency_ms": average(
                    SIM_INBOUND_LATENCY,
                    1.0,
                ),
                "average_sim_engine_latency_ms": average(
                    SIM_ENGINE_LATENCY,
                    1.0,
                ),
                "average_sim_outbound_latency_ms": average(
                    SIM_OUTBOUND_LATENCY,
                    1.0,
                ),
                "average_sim_end_to_end_latency_ms": average(
                    SIM_END_TO_END_LATENCY,
                    1.0,
                ),
                "average_sim_cancel_inbound_latency_ms": average(
                    SIM_CANCEL_INBOUND_LATENCY,
                    1.0,
                ),
                "average_sim_cancel_engine_latency_ms": average(
                    SIM_CANCEL_ENGINE_LATENCY,
                    1.0,
                ),
                "average_sim_cancel_outbound_latency_ms": average(
                    SIM_CANCEL_OUTBOUND_LATENCY,
                    1.0,
                ),
                "average_sim_cancel_end_to_end_latency_ms": average(
                    SIM_CANCEL_END_TO_END_LATENCY,
                    1.0,
                ),
                "invalid_measurements": counts["invalid_latency_measurements"],
            },
            "persistence_failures": counts["metric_sink_failures"],
            "scheduler": {
                "dead_letters": counts["scheduler_dead_letters"],
                "event_processing_limit_failures": counts[
                    "scheduler_event_processing_limit_failures"
                ],
            },
        }

    def snapshot(self) -> MetricsSnapshot:
        """Create a non-mutating immutable snapshot."""
        active, resting, levels = self._authoritative_engine_counts()
        with self._lock:
            if self._run_id is None and self._run_ended:
                active = resting = levels = 0
            counts = dict(self._counts)
            symbol_totals = {
                symbol: dict(values) for symbol, values in self._symbol_totals.items()
            }
            latency_sums = dict(self._latency_sums)
            latency_counts = dict(self._latency_counts)
        buffered = (
            self.persistence.buffered_count if self.persistence is not None else 0
        )

        def average(metric: str, multiplier: float) -> Optional[float]:
            count = latency_counts[metric]
            if count == 0:
                return None
            return latency_sums[metric] / count * multiplier

        traded_quantity, traded_notional, canceled_quantity = self._physical_totals(
            symbol_totals
        )

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
            cancel_stale_run_generation=counts["cancel_stale_run_generation"],
            cancel_exchange_unavailable=counts["cancel_exchange_unavailable"],
            execution_reports=counts["execution_reports"],
            trades=counts["trades"],
            traded_quantity=format(traded_quantity, "f"),
            traded_notional=format(traded_notional, "f"),
            canceled_quantity=format(canceled_quantity, "f"),
            active_orders=active,
            resting_orders=resting,
            order_book_levels=levels,
            connected_sessions=counts["connected_sessions"],
            average_match_loop_duration_us=average(MATCH_LOOP_DURATION, 1.0),
            average_book_commit_duration_us=average(BOOK_COMMIT_DURATION, 1.0),
            average_engine_command_duration_us=average(ENGINE_COMMAND_DURATION, 1.0),
            average_report_creation_duration_us=average(REPORT_CREATION_DURATION, 1.0),
            average_serialization_duration_us=average(SERIALIZATION_DURATION, 1.0),
            average_sim_inbound_latency_ms=average(
                SIM_INBOUND_LATENCY,
                1.0,
            ),
            average_sim_engine_latency_ms=average(
                SIM_ENGINE_LATENCY,
                1.0,
            ),
            average_sim_outbound_latency_ms=average(
                SIM_OUTBOUND_LATENCY,
                1.0,
            ),
            average_sim_end_to_end_latency_ms=average(
                SIM_END_TO_END_LATENCY,
                1.0,
            ),
            average_sim_cancel_inbound_latency_ms=average(
                SIM_CANCEL_INBOUND_LATENCY,
                1.0,
            ),
            average_sim_cancel_engine_latency_ms=average(
                SIM_CANCEL_ENGINE_LATENCY,
                1.0,
            ),
            average_sim_cancel_outbound_latency_ms=average(
                SIM_CANCEL_OUTBOUND_LATENCY,
                1.0,
            ),
            average_sim_cancel_end_to_end_latency_ms=average(
                SIM_CANCEL_END_TO_END_LATENCY,
                1.0,
            ),
            report_delivery_failures=counts["report_delivery_failures"],
            invalid_latency_measurements=counts["invalid_latency_measurements"],
            metric_sink_failures=counts["metric_sink_failures"],
            buffered_metric_records=buffered,
        )

    def prometheus_payload(self) -> bytes:
        if self.registry is None or generate_latest is None:
            raise RuntimeError("Prometheus support is disabled")
        self.refresh_persistence_gauges()
        return generate_latest(self.registry)

    def get_orders_total(self) -> int:
        return self.snapshot().orders_submitted

    def get_trades_total(self) -> int:
        return self.snapshot().trades

    def get_volume_total(self) -> str:
        return self.snapshot().traded_quantity

    def _increment_locked(self, name: str, amount: int = 1) -> None:
        self._counts[name] += amount
        self._lifetime_counts[name] += amount
        self._prom_increment(name, amount)

    def _prom_increment(self, name: str, amount: float) -> None:
        counter = self._prom_counters.get(name)
        if counter is not None:
            counter.inc(amount)

    def _mark_integrity_failure_locked(
        self,
        reason: str,
        *,
        metric_event_sequence: Optional[int] = None,
        event_timestamp_ns: Optional[int] = None,
    ) -> bool:
        """Retain the first fault; the caller must hold the collector lock."""
        if self._run_id is None or self._persistence_integrity_failure is not None:
            return False
        self._persistence_integrity_failure = PersistenceIntegrityFailure(
            run_id=self._run_id,
            metric_event_sequence=metric_event_sequence,
            event_timestamp_ns=event_timestamp_ns,
            reason=reason,
        )
        return True

    def mark_integrity_failure(
        self,
        reason: str,
        *,
        event_sequence: int | None = None,
        event_timestamp_ns: int | None = None,
    ) -> bool:
        with self._lock:
            changed = self._mark_integrity_failure_locked(
                reason,
                metric_event_sequence=event_sequence,
                event_timestamp_ns=event_timestamp_ns,
            )
        if changed:
            self._notify_integrity_failure()
        return changed

    def note_journal_sequence(self, sequence: int, timestamp_ns: int) -> None:
        """Expose the native journal cut through operational run diagnostics."""
        with self._lock:
            self._metric_event_sequence = sequence
            self._last_event_timestamp_ns = timestamp_ns

    def _notify_integrity_failure(self) -> None:
        """Notify after releasing the collector lock, without interrupting work."""
        logger.error(
            "Simulation metrics are incomplete",
            extra={"persistence_integrity_failure": self.persistence_integrity_failure},
        )
        if self.on_integrity_failure is not None:
            try:
                self.on_integrity_failure()
            except Exception:
                logger.exception("Persistence integrity observer failed")

    async def flush(self) -> PersistenceFlushResult:
        if self.persistence is not None:
            return await self.persistence.flush()
        return PersistenceFlushResult(
            PersistenceFlushOutcome.NOTHING_TO_FLUSH,
            records_written=0,
            remaining_records=0,
        )

    async def close(self) -> PersistenceCloseResult:
        if self.persistence is not None:
            return await self.persistence.close()
        return PersistenceCloseResult(
            PersistenceCloseOutcome.CLOSED,
            records_written=0,
            unflushed_records=0,
        )


@dataclass(slots=True)
class ReplaceLatencyTrace:
    run_generation: int
    ingress_sequence: int
    session_id: str
    replace_cl_ord_id: str
    orig_cl_ord_id: str
    sim_received_at: datetime
    order_id: int | None = None
    sim_arrived_at: datetime | None = None
    sim_report_created_at: datetime | None = None
    sim_report_delivered_at: datetime | None = None


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
        self.replaces: Dict[tuple[int, int], ReplaceLatencyTrace] = {}
        self.measurements: list[LatencyMeasurement] = []
        self._delivered_exec_ids: set[str] = set()
        self._completed_order_ids: set[tuple[int, int]] = set()
        self._lock = threading.RLock()

    def reset(
        self,
        *,
        preserve_order_keys: Optional[set[tuple[int, int]]] = None,
        preserve_cancel_keys: Optional[set[tuple[int, int]]] = None,
        preserve_replace_keys: Optional[set[tuple[int, int]]] = None,
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
                self._completed_order_ids.intersection_update(preserve_order_keys)
            if preserve_cancel_keys is None:
                self.cancels.clear()
            else:
                self.cancels = {
                    key: trace
                    for key, trace in self.cancels.items()
                    if key in preserve_cancel_keys
                }
            if preserve_replace_keys is None:
                self.replaces.clear()
            else:
                self.replaces = {
                    key: trace
                    for key, trace in self.replaces.items()
                    if key in preserve_replace_keys
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
            trace = self.orders.get((report.run_generation, report.order_id))
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
            trace = self.orders.get((report.run_generation, report.order_id))
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

    def record_match_loop_duration(
        self,
        order_id: int,
        session_id: Optional[str],
        duration_ns: int,
        timestamp: datetime,
    ) -> Optional[LatencyMeasurement]:
        measurement = self._real_measurement(
            order_id=order_id,
            session_id=session_id,
            metric=MATCH_LOOP_DURATION,
            duration_ns=duration_ns,
            timestamp=timestamp,
        )
        if measurement is not None:
            with self._lock:
                trace = self._find_order_trace_locked(order_id, None)
                if trace is not None:
                    trace.match_loop_duration_ns = duration_ns
        self._publish(measurement)
        return measurement

    def record_serialization_duration(
        self,
        report: ExecutionReport,
        duration_ns: int,
    ) -> Optional[LatencyMeasurement]:
        measurement = self._real_measurement(
            order_id=report.order_id,
            session_id=report.session_id,
            metric=SERIALIZATION_DURATION,
            duration_ns=duration_ns,
            timestamp=self.scheduler.current_time,
            report_exec_id=report.exec_id,
        )
        self._publish(measurement)
        return measurement

    def record_cpu_duration(
        self,
        metric: str,
        order_id: Optional[int],
        session_id: Optional[str],
        duration_ns: int,
        timestamp: datetime,
        report_exec_id: Optional[str] = None,
    ) -> Optional[LatencyMeasurement]:
        """Record one explicitly named monotonic CPU-duration boundary."""
        if metric not in CPU_DURATION_METRICS:
            raise ValueError(f"Unsupported CPU duration metric: {metric}")
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
        milliseconds = (end - start).total_seconds() * 1_000.0
        if not math.isfinite(milliseconds) or milliseconds < 0.0:
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
            value=milliseconds,
            unit="ms",
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
        milliseconds = (end - start).total_seconds() * 1_000.0
        if not math.isfinite(milliseconds) or milliseconds < 0.0:
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
            value=milliseconds,
            unit="ms",
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
        order_id: Optional[int],
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
            value=duration_ns / 1_000.0,
            unit="us",
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

    @staticmethod
    def _replace_key(command: ReplaceOrderCommand) -> tuple[int, int]:
        return command.run_generation, command.ingress_sequence

    def replace_received(self, command: ReplaceOrderCommand) -> None:
        """Start a replace trace keyed independently of client order IDs."""
        key = self._replace_key(command)
        with self._lock:
            if key in self.replaces:
                return
            self.replaces[key] = ReplaceLatencyTrace(
                run_generation=command.run_generation,
                ingress_sequence=command.ingress_sequence,
                session_id=command.session_id,
                replace_cl_ord_id=command.replace_cl_ord_id,
                orig_cl_ord_id=command.orig_cl_ord_id,
                sim_received_at=command.requested_at,
            )

    def replace_arrived(self, command: ReplaceOrderCommand) -> None:
        key = self._replace_key(command)
        with self._lock:
            trace = self.replaces.get(key)
            if trace is None:
                self._missing_replace_trace(command, "replace_arrived")
                return
            if trace.sim_arrived_at is not None:
                return
            trace.sim_arrived_at = self.scheduler.current_time
            measurement = self._replace_duration_measurement(
                trace,
                SIM_REPLACE_INBOUND_LATENCY,
                trace.sim_received_at,
                trace.sim_arrived_at,
            )
        self._publish(measurement)

    def replace_report_created(
        self,
        command: ReplaceOrderCommand,
        report: ExecutionReport | CancelReject,
    ) -> None:
        key = self._replace_key(command)
        with self._lock:
            trace = self.replaces.get(key)
            if trace is None:
                self._missing_replace_trace(command, "replace_report_created")
                return
            if trace.sim_report_created_at is not None:
                return
            trace.order_id = report.order_id
            trace.sim_report_created_at = report.timestamp
            if trace.sim_arrived_at is None:
                self._missing_replace_trace(
                    command,
                    "replace_report_created_without_arrival",
                )
                return
            measurement = self._replace_duration_measurement(
                trace,
                SIM_REPLACE_ENGINE_LATENCY,
                trace.sim_arrived_at,
                report.timestamp,
                report_exec_id=(
                    report.exec_id
                    if isinstance(report, ExecutionReport)
                    else report.reject_id
                ),
            )
        self._publish(measurement)

    def replace_report_delivered(
        self,
        report: ExecutionReport | CancelReject,
        *,
        delivered_at: Optional[datetime] = None,
    ) -> bool:
        """Finish a replace trace only after successful report delivery."""
        ingress_sequence = (
            report.replace_ingress_sequence
            if isinstance(report, ExecutionReport)
            else report.ingress_sequence
        )
        if ingress_sequence is None:
            return False
        key = (report.run_generation, ingress_sequence)
        with self._lock:
            trace = self.replaces.get(key)
            if trace is None:
                logger.warning(
                    "Replace latency trace missing",
                    extra={
                        "run_generation": report.run_generation,
                        "ingress_sequence": ingress_sequence,
                        "latency_stage": "replace_report_delivered",
                    },
                )
                return False
            if trace.sim_report_delivered_at is not None:
                return False
            resolved_delivered_at = delivered_at or self.scheduler.current_time
            trace.sim_report_delivered_at = resolved_delivered_at
            outbound = self._replace_duration_measurement(
                trace,
                SIM_REPLACE_OUTBOUND_LATENCY,
                report.timestamp,
                resolved_delivered_at,
                report_exec_id=(
                    report.exec_id
                    if isinstance(report, ExecutionReport)
                    else report.reject_id
                ),
            )
            end_to_end = self._replace_duration_measurement(
                trace,
                SIM_REPLACE_END_TO_END_LATENCY,
                trace.sim_received_at,
                resolved_delivered_at,
                report_exec_id=(
                    report.exec_id
                    if isinstance(report, ExecutionReport)
                    else report.reject_id
                ),
            )
            self.replaces.pop(key, None)
        self._publish(outbound)
        self._publish(end_to_end)
        return True

    def _replace_duration_measurement(
        self,
        trace: ReplaceLatencyTrace,
        metric: str,
        start: datetime,
        end: datetime,
        *,
        report_exec_id: Optional[str] = None,
    ) -> Optional[LatencyMeasurement]:
        milliseconds = (end - start).total_seconds() * 1_000.0
        if not math.isfinite(milliseconds) or milliseconds < 0.0:
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
            value=milliseconds,
            unit="ms",
            timestamp=end,
            report_exec_id=report_exec_id,
            run_generation=trace.run_generation,
            ingress_sequence=trace.ingress_sequence,
            cl_ord_id=trace.replace_cl_ord_id,
            orig_cl_ord_id=trace.orig_cl_ord_id,
        )

    @staticmethod
    def _missing_replace_trace(
        command: ReplaceOrderCommand,
        stage: str,
    ) -> None:
        logger.warning(
            "Replace latency trace missing",
            extra={
                "run_generation": command.run_generation,
                "ingress_sequence": command.ingress_sequence,
                "replace_cl_ord_id": command.replace_cl_ord_id,
                "latency_stage": stage,
            },
        )

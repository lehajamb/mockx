"""Deterministic latency scheduler for simulated trading."""

import heapq
import asyncio
import inspect
import math
import time
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    NoReturn,
    Optional,
    Protocol,
)
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import logging

from .models import (
    CancelOrderCommand,
    ReplaceOrderCommand,
    ExecutionReport,
    ProcessOrderCommand,
    SchedulerEventMetadata,
    SchedulerFailure,
    SchedulerFailureStatus,
    SchedulerMetadataScalar,
    SchedulerState,
    ScheduledEvent,
)


logger = logging.getLogger(__name__)

DEFAULT_MAX_EVENTS_PER_ADVANCE = 100_000
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def datetime_to_ns(value: datetime) -> int:
    """Convert a timezone-aware datetime to exact integer Unix nanoseconds."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")
    delta = value.astimezone(timezone.utc) - UNIX_EPOCH
    total_microseconds = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    return total_microseconds * 1_000


class SchedulerError(RuntimeError):
    """Base class for explicit scheduler lifecycle errors."""


class SchedulerFailedError(SchedulerError):
    """Raised whenever work is attempted on a failed scheduler."""

    def __init__(self, failure: SchedulerFailure):
        self.failure = failure
        super().__init__(
            "Scheduler failed while processing "
            f"{failure.event_type} event {failure.event_id}: "
            f"{failure.exception_type}: {failure.exception_message}"
        )


class SchedulerStoppedError(SchedulerError):
    """Raised whenever work is attempted on a stopped scheduler."""


class EventHandlerNotRegistered(RuntimeError):
    """Synthetic deterministic failure for an unhandled event type."""


class EventProcessingLimitExceeded(RuntimeError):
    """Synthetic deterministic failure for the per-advancement safety guard."""


def _type_name(value: object) -> str:
    return type(value).__name__


def _metadata_scalar(value: object) -> SchedulerMetadataScalar:
    """Copy one value into an immutable, deterministic diagnostic scalar."""
    if isinstance(value, Enum):
        return _metadata_scalar(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return _type_name(value)


def _snapshot_event_metadata(data: object) -> SchedulerEventMetadata:
    """Capture event metadata without retaining the mutable payload object."""
    metadata: Dict[str, SchedulerMetadataScalar] = {
        "data_type": _type_name(data),
    }
    if is_dataclass(data) and not isinstance(data, type):
        for field in fields(data):
            metadata[field.name] = _metadata_scalar(getattr(data, field.name))
    elif isinstance(data, Mapping):
        for key, value in sorted(
            data.items(),
            key=lambda item: str(item[0]),
        ):
            metadata[str(key)] = _metadata_scalar(value)
    elif isinstance(data, (str, bool, int, float, Enum, datetime)):
        metadata["value"] = _metadata_scalar(data)
    return tuple(sorted(metadata.items()))


class MonotonicClock(Protocol):
    """Clock used only for real elapsed-duration measurements."""

    def now_ns(self) -> int:
        ...


class PerfCounterClock:
    """Production monotonic clock backed by ``perf_counter_ns``."""

    def now_ns(self) -> int:
        return time.perf_counter_ns()


def monotonic_duration_ns(
    start_ns: int,
    end_ns: int,
    *,
    context: str,
) -> int:
    """Return a validated monotonic duration without masking clock errors."""
    if end_ns < start_ns:
        raise ValueError(
            f"Invalid monotonic timestamp order for {context}: "
            f"start_ns={start_ns}, end_ns={end_ns}"
        )
    return end_ns - start_ns


class LatencyScheduler:
    """
    Manages deterministic event scheduling with simulated latencies.

    This scheduler maintains a priority queue of events and executes
    them in order based on their simulated timestamps. It does not
    use real time delays, allowing backtests to run at full speed.
    """

    def __init__(
        self,
        session_delays: Optional[Dict[str, int]] = None,
        *,
        max_events_per_advance: int = DEFAULT_MAX_EVENTS_PER_ADVANCE,
        processing_batch_size: int = 128,
    ):
        if (
            not isinstance(max_events_per_advance, int)
            or isinstance(max_events_per_advance, bool)
            or max_events_per_advance <= 0
        ):
            raise ValueError(
                "max_events_per_advance must be a positive integer"
            )
        configured_delays = dict(session_delays or {})
        if not isinstance(processing_batch_size, int) or isinstance(processing_batch_size, bool) or processing_batch_size <= 0:
            raise ValueError("processing_batch_size must be a positive integer")
        if any(
            not isinstance(delay, int)
            or isinstance(delay, bool)
            or delay < 0
            for delay in configured_delays.values()
        ):
            raise ValueError(
                "Session delays must be non-negative integers"
            )

        self.event_queue: List[ScheduledEvent] = []
        self.current_time: datetime = datetime(1970, 1, 1, tzinfo=timezone.utc)
        self.event_handlers: Dict[str, Callable] = {}
        self.session_delays = configured_delays
        self.max_events_per_advance = max_events_per_advance
        self.processing_batch_size = processing_batch_size
        self.on_processing_boundary: Optional[Callable[[], object]] = None
        self.state = SchedulerState.RUNNING
        self.dead_letter: Optional[SchedulerFailure] = None
        self.failure_count = 0
        self._failure_cause: Optional[BaseException] = None
        self._generation = 0
        self._next_sequence = 0
        self.on_queue_depth_change: Optional[Callable[[int], None]] = None
        self.on_failure: Optional[Callable[[SchedulerFailure], None]] = None
        self.on_state_change: Optional[Callable[[SchedulerState], None]] = None

    @staticmethod
    def _notify_observer(callback: Optional[Callable], *args: object) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("Scheduler observability callback failed")

    def _notify_depth(self) -> None:
        self._notify_observer(
            self.on_queue_depth_change,
            len(self.event_queue),
        )

    @property
    def failure_cause(self) -> Optional[BaseException]:
        """Return the original process-local exception for diagnostics."""
        return self._failure_cause

    def current_time_ns(self) -> int:
        """Get current simulated time in nanoseconds."""
        return datetime_to_ns(self.current_time)

    def register_handler(self, event_type: str, handler: Callable) -> None:
        """Register a handler for a specific event type."""
        self.event_handlers[event_type] = handler

    def ensure_running(self) -> None:
        """Reject work unless this scheduler generation is active."""
        if self.state == SchedulerState.FAILED:
            self._raise_failed()
        if self.state == SchedulerState.STOPPED:
            raise SchedulerStoppedError("Scheduler is stopped")

    def _raise_failed(self) -> NoReturn:
        if self.dead_letter is None:
            raise RuntimeError(
                "Scheduler is FAILED without a dead-letter diagnostic"
            )
        error = SchedulerFailedError(self.dead_letter)
        if self._failure_cause is not None:
            raise error from self._failure_cause
        raise error

    @staticmethod
    def _validate_delay(delay_ms: int) -> None:
        if (
            not isinstance(delay_ms, int)
            or isinstance(delay_ms, bool)
            or delay_ms < 0
        ):
            raise ValueError("Event delay must be a non-negative integer")

    def _fail_event(
        self,
        event: ScheduledEvent,
        cause: BaseException,
        status: SchedulerFailureStatus,
    ) -> NoReturn:
        """Enter the terminal failed state exactly once and raise."""
        if self.state == SchedulerState.FAILED:
            self._raise_failed()

        failure = SchedulerFailure(
            event_id=event.event_id,
            event_type=event.event_type,
            scheduled_timestamp=event.timestamp,
            scheduler_sequence=event.sequence,
            failure_timestamp=self.current_time,
            exception_type=type(cause).__name__,
            exception_message=str(cause),
            retry_attempted=False,
            status=status,
            event_metadata=event.metadata,
        )
        self.dead_letter = failure
        self.failure_count += 1
        self._failure_cause = cause
        self.state = SchedulerState.FAILED
        self._notify_observer(self.on_failure, failure)
        self._notify_observer(self.on_state_change, self.state)
        logger.error(
            "Scheduler entered FAILED state at event %s (%s): %s",
            event.event_id,
            event.event_type,
            cause,
            exc_info=(type(cause), cause, cause.__traceback__),
        )
        raise SchedulerFailedError(failure) from cause

    def schedule_event(
        self,
        event_type: str,
        data: object,
        delay_ms: int,
    ) -> str:
        """Schedule an event to occur after a specified delay."""
        self.ensure_running()
        self._validate_delay(delay_ms)
        event_time = self.current_time + timedelta(milliseconds=delay_ms)
        sequence = self._next_sequence
        event = ScheduledEvent(
            event_id=f"{self._generation}:{sequence}",
            timestamp=event_time,
            sequence=sequence,
            event_type=event_type,
            data=data,
            metadata=_snapshot_event_metadata(data),
        )
        self._next_sequence += 1
        heapq.heappush(self.event_queue, event)
        self._notify_depth()
        logger.debug(f"Scheduled {event_type} at {event_time} (in {delay_ms}ms)")
        return event.event_id

    def extract_events(self, event_types: set[str]) -> List[ScheduledEvent]:
        """Remove selected pending events without disturbing retained FIFO order."""
        extracted = sorted(
            event for event in self.event_queue if event.event_type in event_types
        )
        if extracted:
            self.event_queue[:] = [
                event for event in self.event_queue
                if event.event_type not in event_types
            ]
            heapq.heapify(self.event_queue)
            self._notify_depth()
        return extracted

    def schedule_order_processing(
        self,
        command: ProcessOrderCommand,
        network_ms: int,
        engine_ms: int,
        *,
        session_alias: Optional[str] = None,
        inbound_ms: Optional[int] = None,
    ) -> None:
        """Schedule order processing with network, engine, and session-specific latencies."""
        self.ensure_running()
        session_delay_ms = self.session_delays.get(session_alias or "", 0)

        # First, simulate network latency + session delay to reach the exchange
        arrival_delay = (
            network_ms // 2 if inbound_ms is None else inbound_ms
        ) + session_delay_ms
        total_delay = arrival_delay + engine_ms
        self._validate_delay(arrival_delay)
        self._validate_delay(total_delay)
        self.schedule_event(
            "order_arrived",
            command,
            arrival_delay
        )

        # Then, simulate engine processing latency
        self.schedule_event(
            "order_process",
            command,
            total_delay
        )

        if session_delay_ms > 0:
            logger.debug(f"Applied {session_delay_ms}ms session delay for {session_alias}")

    def schedule_cancel_processing(
        self,
        command: CancelOrderCommand,
        network_ms: int,
        engine_ms: int,
        *,
        session_alias: Optional[str] = None,
        inbound_ms: Optional[int] = None,
    ) -> None:
        """Schedule cancel arrival and engine processing with order-equivalent latency."""
        self.ensure_running()
        session_delay_ms = self.session_delays.get(session_alias or "", 0)
        arrival_delay = (
            network_ms // 2 if inbound_ms is None else inbound_ms
        ) + session_delay_ms
        total_delay = arrival_delay + engine_ms
        self._validate_delay(arrival_delay)
        self._validate_delay(total_delay)
        self.schedule_event(
            "cancel_arrived_at_exchange",
            command,
            arrival_delay,
        )
        self.schedule_event(
            "cancel_engine_process",
            command,
            total_delay,
        )

    def schedule_replace_processing(
        self, command: ReplaceOrderCommand, network_ms: int, engine_ms: int,
        *, session_alias: Optional[str] = None, inbound_ms: Optional[int] = None,
    ) -> None:
        """Native replacement traverses the ordinary inbound and engine delays."""
        self.ensure_running()
        arrival_delay = (network_ms // 2 if inbound_ms is None else inbound_ms) + self.session_delays.get(session_alias or "", 0)
        total_delay = arrival_delay + engine_ms
        self._validate_delay(arrival_delay)
        self._validate_delay(total_delay)
        self.schedule_event("replace_arrived_at_exchange", command, arrival_delay)
        self.schedule_event("replace_engine_process", command, total_delay)

    def schedule_execution_report(
        self,
        report: ExecutionReport,
        network_ms: int,
        *,
        outbound_ms: Optional[int] = None,
    ) -> None:
        """Schedule delivery; equal-time reports retain engine insertion order."""
        self.schedule_event(
            "exec_report",
            report,
            network_ms // 2 if outbound_ms is None else outbound_ms,
        )

    async def _process_due_events(self, budget: List[int]) -> int:
        """Process current due work against one shared advancement budget."""
        processed = 0

        while self.event_queue:
            next_event = self.event_queue[0]
            if next_event.timestamp > self.current_time:
                break

            if budget[0] >= self.max_events_per_advance:
                await self._processing_boundary()
                event = heapq.heappop(self.event_queue)
                self._notify_depth()
                cause = EventProcessingLimitExceeded(
                    "Event processing limit of "
                    f"{self.max_events_per_advance} exceeded during one "
                    "advancement"
                )
                self._fail_event(
                    event,
                    cause,
                    SchedulerFailureStatus.EVENT_PROCESSING_LIMIT_EXCEEDED,
                )

            event = heapq.heappop(self.event_queue)
            self._notify_depth()
            handler = self.event_handlers.get(event.event_type)
            if handler is None:
                await self._processing_boundary()
                cause = EventHandlerNotRegistered(
                    f"No handler registered for event type: {event.event_type}"
                )
                self._fail_event(
                    event,
                    cause,
                    SchedulerFailureStatus.HANDLER_NOT_REGISTERED,
                )

            try:
                result = handler(event.data)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                try:
                    await self._processing_boundary()
                except Exception:
                    logger.exception("Processing boundary also failed while settling a handler failure")
                self._fail_event(
                    event,
                    exc,
                    SchedulerFailureStatus.HANDLER_EXCEPTION,
                )

            processed += 1
            budget[0] += 1
            if processed % self.processing_batch_size == 0:
                await self._processing_boundary()
                await asyncio.sleep(0)

        return processed

    async def _processing_boundary(self) -> None:
        """Flush consumers outside command exception classification."""
        if self.on_processing_boundary is not None:
            result = self.on_processing_boundary()
            if inspect.isawaitable(result):
                await result

    async def process_events(self) -> int:
        """Process all due events with one bounded advancement budget."""
        self.ensure_running()
        processed = await self._process_due_events([0])
        await self._processing_boundary()
        if processed > 0:
            logger.debug(f"Processed {processed} events at {self.current_time}")
        return processed

    def advance_time(self, delta_ms: int) -> None:
        """Advance simulated time by specified milliseconds."""
        self.ensure_running()
        self._validate_delay(delta_ms)
        self.current_time += timedelta(milliseconds=delta_ms)

    async def run_until(self, target_time: datetime) -> int:
        """Run events inclusively through target and return the handled count."""
        self.ensure_running()
        if target_time < self.current_time:
            return 0

        processed = 0
        budget = [0]
        while self.event_queue and self.event_queue[0].timestamp <= target_time:
            next_time = self.event_queue[0].timestamp
            if next_time > self.current_time:
                await self._processing_boundary()
                self.current_time = next_time

            processed += await self._process_due_events(budget)

        await self._processing_boundary()
        self.current_time = target_time
        return processed

    async def run_until_idle(self) -> int:
        """Drain scheduled work without rewinding to overdue event timestamps."""
        self.ensure_running()
        processed = 0
        budget = [0]
        while self.event_queue:
            next_time = self.event_queue[0].timestamp
            if next_time > self.current_time:
                await self._processing_boundary()
                self.current_time = next_time
            processed += await self._process_due_events(budget)
        await self._processing_boundary()
        return processed

    def clear(self) -> None:
        """Clear all scheduled events."""
        self.ensure_running()
        self.event_queue.clear()
        self._notify_depth()

    def get_pending_count(self) -> int:
        """Get number of pending events."""
        return len(self.event_queue)

    def get_next_event_time(self) -> Optional[datetime]:
        """Get the timestamp of the next event without removing it."""
        if not self.event_queue:
            return None
        return self.event_queue[0].timestamp

    def set_time(self, new_time: datetime) -> None:
        """Set the current simulated time."""
        self.ensure_running()
        self.current_time = new_time

    def reset(self) -> None:
        """Start a clean scheduler generation for a new simulation session."""
        self.event_queue.clear()
        self.current_time = datetime(1970, 1, 1, tzinfo=timezone.utc)
        self._generation += 1
        self._next_sequence = 0
        self.state = SchedulerState.RUNNING
        self.dead_letter = None
        self.failure_count = 0
        self._failure_cause = None
        self._notify_depth()
        self._notify_observer(self.on_state_change, self.state)
        logger.info(f"Latency scheduler reset. Time set to {self.current_time}.")

    def stop(self) -> None:
        """Stop scheduler work without discarding failure diagnostics."""
        self.state = SchedulerState.STOPPED
        self._notify_observer(self.on_state_change, self.state)

    def diagnostics(self) -> Dict[str, object]:
        """Return one immutable-data scheduler observability snapshot."""
        return {
            "state": self.state.value,
            "pending_events": self.get_pending_count(),
            "failure_count": self.failure_count,
            "max_events_per_advance": self.max_events_per_advance,
            "dead_letter": (
                self.dead_letter.to_dict()
                if self.dead_letter is not None
                else None
            ),
        }


@dataclass
class LatencyProfile:
    """Profile for different latency scenarios."""
    name: str
    network_ms: int
    engine_ms: int
    jitter_pct: float = 0.0  # Percentage variation in latency

    def get_network_latency(self, rng: Any = None) -> int:
        """Get network latency with optional jitter."""
        if self.jitter_pct > 0 and rng:
            variation = int(self.network_ms * self.jitter_pct)
            return self.network_ms + rng.randint(-variation, variation)
        return self.network_ms

    def get_engine_latency(self, rng: Any = None) -> int:
        """Get engine latency with optional jitter."""
        if self.jitter_pct > 0 and rng:
            variation = int(self.engine_ms * self.jitter_pct)
            return self.engine_ms + rng.randint(-variation, variation)
        return self.engine_ms


# Common latency profiles
LATENCY_PROFILES = {
    "low": LatencyProfile("low", network_ms=10, engine_ms=1),
    "medium": LatencyProfile("medium", network_ms=50, engine_ms=5),
    "high": LatencyProfile("high", network_ms=200, engine_ms=20),
    "variable": LatencyProfile("variable", network_ms=100, engine_ms=10, jitter_pct=0.3),
}

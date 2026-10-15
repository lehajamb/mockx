"""Automatic persistence retries own deadlines independent of producer signals."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import inspect
from typing import Callable

import pytest

from src.mockx import metrics as metrics_module
from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricRecord,
    MetricsSinkError,
    PersistenceAddOutcome,
    PersistenceHealthState,
)


def record(number: int) -> MetricRecord:
    return MetricRecord.create(
        "retry_deadline",
        {},
        {"value": number},
        run_id="retry-run",
        event_sequence=number,
        event_timestamp_ns=number,
    )


class VirtualClock:
    def __init__(self) -> None:
        self.nanoseconds = 0

    @property
    def milliseconds(self) -> int:
        return self.nanoseconds // 1_000_000

    def advance_to(self, milliseconds: int) -> None:
        assert milliseconds >= self.milliseconds
        self.nanoseconds = milliseconds * 1_000_000

    def monotonic_ns(self) -> int:
        return self.nanoseconds


class RecordingSink(InMemoryMetricsSink):
    def __init__(
        self,
        clock: VirtualClock,
        *,
        fail_writes: set[int] | None = None,
        fail_health: set[int] | None = None,
    ) -> None:
        super().__init__()
        self.clock = clock
        self.fail_writes = fail_writes
        self.fail_health = fail_health or set()
        self.attempts: list[tuple[int, tuple[MetricRecord, ...]]] = []
        self.health_attempts: list[int] = []

    async def write(self, records) -> None:
        batch = tuple(records)
        self.attempts.append((self.clock.milliseconds, batch))
        if self.fail_writes is None or len(self.attempts) in self.fail_writes:
            raise MetricsSinkError("planned write outage")
        await super().write(batch)

    async def health_check(self) -> None:
        self.health_attempts.append(self.clock.milliseconds)
        if len(self.health_attempts) in self.fail_health:
            raise MetricsSinkError("planned health outage")


@dataclass(frozen=True)
class WaitStep:
    kind: str
    at_ms: int | None = None
    action: Callable | None = None


class ControlledWait:
    """Only replace worker Event.wait timeouts; other wait_for calls stay real."""

    def __init__(self, writer, clock, steps) -> None:
        self.writer = writer
        self.clock = clock
        self.steps = deque(steps)
        self.requested_timeouts: list[float] = []
        self.first_wake_completed = asyncio.Event()
        self.holding = asyncio.Event()
        self.original_wait_for = asyncio.wait_for

    async def __call__(self, awaitable, *, timeout):
        code = getattr(awaitable, "cr_code", None)
        if code is None or code.co_name != "wait":
            return await self.original_wait_for(awaitable, timeout=timeout)
        if not self.steps or timeout < 0:
            awaitable.close()
            raise AssertionError("Worker requested an unexpected extra wait")
        self.requested_timeouts.append(timeout)
        step = self.steps.popleft()
        try:
            if step.at_ms is not None:
                self.clock.advance_to(step.at_ms)
            if step.action is not None:
                result = step.action()
                if inspect.isawaitable(result):
                    await result
        except BaseException:
            awaitable.close()
            raise
        if step.kind == "timeout":
            self.clock.nanoseconds += round(timeout * 1_000_000_000)
            awaitable.close()
            if len(self.requested_timeouts) == 1:
                self.first_wake_completed.set()
            raise asyncio.TimeoutError
        if step.kind == "stop":
            self.writer._stop_worker = True
        if step.kind == "hold":
            self.writer._wake_event.clear()
            self.holding.set()
        elif step.kind != "signal":
            self.writer._wake_event.set()
        result = await awaitable
        if len(self.requested_timeouts) == 1:
            self.first_wake_completed.set()
        return result


def bind_worker(writer: BufferedMetricsWriter) -> None:
    writer._loop = asyncio.get_running_loop()
    writer._wake_event = asyncio.Event()


def install_wait(monkeypatch, writer, clock, steps) -> ControlledWait:
    waiter = ControlledWait(writer, clock, steps)
    monkeypatch.setattr(metrics_module.time, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(metrics_module.asyncio, "wait_for", waiter)
    return waiter


def attempt_times(sink: RecordingSink) -> list[int]:
    return [timestamp for timestamp, _ in sink.attempts]


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["threshold", "full", "thread"])
async def test_producer_wakes_cannot_bypass_or_postpone_retry_deadline(
    monkeypatch, signal
):
    clock = VirtualClock()
    sink = RecordingSink(clock)
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        max_buffer_records=1 if signal == "full" else 4,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    first = record(1)
    writer.add_nowait(first)
    outcomes = []

    async def produce(number):
        if signal == "thread":
            outcome = await asyncio.to_thread(writer.add_nowait, record(number))
        else:
            outcome = writer.add_nowait(record(number))
        outcomes.append(outcome)

    waiter = install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 0),
            WaitStep("signal", 200, lambda: produce(2)),
            WaitStep("signal", 800, lambda: produce(3)),
            WaitStep("timeout"),
            WaitStep("stop"),
        ],
    )
    await writer._worker_loop()

    assert attempt_times(sink) == [0, 1_000]
    assert waiter.requested_timeouts[3] == pytest.approx(0.2)
    assert all(batch == (first,) and batch[0] is first for _, batch in sink.attempts)
    assert writer.failure_count == 2
    expected = (
        PersistenceAddOutcome.REJECTED_FULL
        if signal == "full"
        else PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED
    )
    assert outcomes == [expected, expected]
    queued = [first] if signal == "full" else [first, record(2), record(3)]
    assert writer.buffered_count == len(queued)
    assert writer.buffered_bytes == sum(item.encoded_size_bytes for item in queued)


@pytest.mark.asyncio
async def test_first_retry_uses_base_delay_then_doubles_and_caps(monkeypatch):
    clock = VirtualClock()
    sink = RecordingSink(clock)
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    logical = record(1)
    writer.add_nowait(logical)
    install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 0),
            *(WaitStep("timeout") for _ in range(5)),
            WaitStep("stop"),
        ],
    )
    await writer._worker_loop()

    assert attempt_times(sink) == [0, 1_000, 3_000, 7_000, 11_000, 15_000]
    assert all(
        batch == (logical,) and batch[0] is logical for _, batch in sink.attempts
    )
    assert writer.failure_count == 6
    assert writer.health_snapshot().state == PersistenceHealthState.UNAVAILABLE
    assert writer.buffered_count == 1
    assert writer.buffered_bytes == logical.encoded_size_bytes


@pytest.mark.asyncio
async def test_retry_due_flushes_partial_batch_without_threshold_signal(monkeypatch):
    clock = VirtualClock()
    sink = RecordingSink(clock, fail_writes={1})
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=5,
        flush_interval_ms=10_000,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    records = [record(1), record(2)]
    writer.add_nowait(records[0])
    install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("timeout"),
            WaitStep("wake", 10_200, lambda: writer.add_nowait(records[1])),
            WaitStep("timeout"),
            WaitStep("stop"),
        ],
    )
    await writer._worker_loop()

    assert attempt_times(sink) == [10_000, 11_000]
    assert sink.writes == [tuple(records)]
    assert sink.attempts[0][1][0] is sink.attempts[1][1][0] is records[0]
    assert writer.buffered_count == writer.buffered_bytes == 0
    assert writer.health_snapshot().state == PersistenceHealthState.HEALTHY


@pytest.mark.asyncio
async def test_initial_health_failure_obeys_backoff_before_reprobe(monkeypatch):
    clock = VirtualClock()
    sink = RecordingSink(clock, fail_writes=set(), fail_health={1, 2, 3})
    writer = BufferedMetricsWriter(
        sink,
        flush_interval_ms=10_000,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 0),
            *(WaitStep("timeout") for _ in range(3)),
            WaitStep("stop"),
        ],
    )
    await writer.start()
    await writer.worker_task

    assert sink.health_attempts == [0, 1_000, 3_000, 7_000]
    assert sink.attempts == []
    health = writer.health_snapshot()
    assert health.state == PersistenceHealthState.HEALTHY
    assert health.consecutive_failures == 0
    assert health.lifetime_failures == 3


@pytest.mark.asyncio
async def test_successful_probe_cannot_reset_backoff_for_retained_failed_batch(
    monkeypatch,
):
    clock = VirtualClock()
    sink = RecordingSink(clock)
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        flush_interval_ms=10_000,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 500),
            WaitStep("timeout"),
            WaitStep("stop"),
        ],
    )
    writer.add_nowait(record(1))
    assert not (await writer.flush()).successful
    clock.advance_to(500)
    assert await writer._check_sink_health()
    assert writer.health_snapshot().consecutive_failures == 1
    await writer._worker_loop()

    assert sink.health_attempts == [500]
    assert attempt_times(sink) == [0, 1_000]
    assert writer.failure_count == 2
    assert writer.buffered_count == 1


@pytest.mark.asyncio
async def test_successful_write_resets_backoff_for_a_new_outage(monkeypatch):
    clock = VirtualClock()
    sink = RecordingSink(clock, fail_writes={1, 2, 4})
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    records = [record(1), record(2)]
    writer.add_nowait(records[0])
    install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 0),
            WaitStep("timeout"),
            WaitStep("timeout"),
            WaitStep("wake", 3_100, lambda: writer.add_nowait(records[1])),
            WaitStep("timeout"),
            WaitStep("stop"),
        ],
    )
    await writer._worker_loop()

    assert attempt_times(sink) == [0, 1_000, 3_000, 3_100, 4_100]
    assert sink.writes == [(records[0],), (records[1],)]
    assert writer.failure_count == 3
    assert writer.health_snapshot().consecutive_failures == 0
    assert writer.buffered_count == writer.buffered_bytes == 0


@pytest.mark.asyncio
async def test_explicit_flush_remains_immediate_and_clears_obsolete_deadline(
    monkeypatch,
):
    clock = VirtualClock()
    sink = RecordingSink(clock, fail_writes={1, 3})
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    records = [record(1), record(2)]
    install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 300, lambda: writer.add_nowait(records[1])),
            WaitStep("timeout"),
            WaitStep("stop"),
        ],
    )
    writer.add_nowait(records[0])
    assert not (await writer.flush()).successful
    clock.advance_to(200)
    assert (await writer.flush()).successful
    assert attempt_times(sink) == [0, 200]
    await writer._worker_loop()

    assert attempt_times(sink) == [0, 200, 300, 1_300]
    assert sink.writes == [(records[0],), (records[1],)]
    assert writer.buffered_count == writer.buffered_bytes == 0


class PausedFirstFailureSink(RecordingSink):
    def __init__(self, clock) -> None:
        super().__init__(clock, fail_writes=set())
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def write(self, records) -> None:
        if not self.attempts:
            self.attempts.append((self.clock.milliseconds, tuple(records)))
            self.entered.set()
            await self.release.wait()
            raise MetricsSinkError("paused write outage")
        await super().write(records)


@pytest.mark.asyncio
async def test_automatic_retry_rechecks_deadline_after_waiting_for_flush_lock(
    monkeypatch,
):
    clock = VirtualClock()
    sink = PausedFirstFailureSink(clock)
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    logical = record(1)
    writer.add_nowait(logical)
    waiter = install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 100),
            WaitStep("timeout"),
            WaitStep("stop"),
        ],
    )
    explicit = asyncio.create_task(writer.flush())
    await sink.entered.wait()
    worker = asyncio.create_task(writer._worker_loop())
    await waiter.first_wake_completed.wait()
    assert writer._flush_lock.locked()
    assert attempt_times(sink) == [0]
    clock.advance_to(200)
    sink.release.set()
    try:
        assert not (await explicit).successful
        await worker
    finally:
        if not worker.done():
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    assert attempt_times(sink) == [0, 1_200]
    assert sink.writes == [(logical,)]
    assert sink.attempts[0][1][0] is sink.attempts[1][1][0] is logical
    assert writer.failure_count == 1
    assert writer.buffered_count == writer.buffered_bytes == 0
    assert not writer._flush_lock.locked()


@pytest.mark.asyncio
async def test_close_interrupts_backoff_and_performs_final_drain_immediately(
    monkeypatch,
):
    clock = VirtualClock()
    sink = RecordingSink(clock, fail_writes={1})
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        retry_backoff_ms=10_000,
        max_retry_delay_ms=40_000,
    )
    bind_worker(writer)
    logical = record(1)
    writer.add_nowait(logical)
    waiter = install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 0),
            WaitStep("hold"),
        ],
    )
    worker = asyncio.create_task(writer._worker_loop())
    writer._worker_task = worker
    await waiter.holding.wait()
    result = await writer.close()

    assert result.successful
    assert worker.done()
    assert attempt_times(sink) == [0, 0]
    assert sink.writes == [(logical,)]
    assert sink.close_calls == 1
    assert writer.buffered_count == writer.buffered_bytes == 0


@pytest.mark.asyncio
async def test_cancellation_retains_fifo_without_establishing_retry_failure(
    monkeypatch,
):
    clock = VirtualClock()
    sink = PausedFirstFailureSink(clock)
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=2,
        max_buffer_records=3,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    monkeypatch.setattr(metrics_module.time, "monotonic_ns", clock.monotonic_ns)
    records = [record(index) for index in range(1, 4)]
    for logical in records[:2]:
        writer.add_nowait(logical)
    flushing = asyncio.create_task(writer.flush())
    await sink.entered.wait()
    writer.add_nowait(records[2])
    bytes_before = sum(item.encoded_size_bytes for item in records)
    flushing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flushing

    assert writer.failure_count == 0
    assert writer.health_snapshot().consecutive_failures == 0
    assert writer.buffered_count == 3
    assert writer.buffered_bytes == bytes_before
    assert writer._inflight == ()
    assert not writer._flush_lock.locked()
    assert (await writer.flush()).successful
    persisted = [item for batch in sink.writes for item in batch]
    assert persisted == records
    assert all(actual is expected for actual, expected in zip(persisted, records))
    assert writer.buffered_count == writer.buffered_bytes == 0


class SinkWithoutHealthCheck:
    def __init__(self) -> None:
        self.flush_calls = 0
        self.close_calls = 0

    async def write(self, records) -> None:
        raise AssertionError("There are no buffered records to write")

    async def flush(self) -> None:
        self.flush_calls += 1
        if self.flush_calls == 1:
            raise MetricsSinkError("sink replacement flush outage")

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_empty_writer_without_optional_health_probe_clears_expired_recovery(
    monkeypatch,
):
    clock = VirtualClock()
    original_sink = SinkWithoutHealthCheck()
    writer = BufferedMetricsWriter(
        original_sink,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    waiter = install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 0),
            WaitStep("timeout"),
            WaitStep("stop"),
        ],
    )
    replacement = InMemoryMetricsSink()
    result = await writer.replace_sink(replacement)
    assert not result.successful
    assert writer.sink is original_sink
    assert writer.failure_count == 1
    await writer._worker_loop()

    assert writer.health_snapshot().state == PersistenceHealthState.HEALTHY
    assert writer.health_snapshot().consecutive_failures == 0
    assert writer.failure_count == 1
    assert original_sink.flush_calls == 1
    assert original_sink.close_calls == 0
    assert len(waiter.requested_timeouts) == 3
    assert writer.buffered_count == writer.buffered_bytes == 0


class PausedInitialHealthSink(RecordingSink):
    def __init__(self, clock) -> None:
        super().__init__(clock, fail_writes=set())
        self.health_entered = asyncio.Event()
        self.health_release = asyncio.Event()

    async def health_check(self) -> None:
        self.health_entered.set()
        await self.health_release.wait()
        await super().health_check()


@pytest.mark.asyncio
async def test_initial_health_probe_serializes_with_background_writes(monkeypatch):
    clock = VirtualClock()
    sink = PausedInitialHealthSink(clock)
    writer = BufferedMetricsWriter(sink, flush_batch_size=1)
    logical = record(1)
    writer.add_nowait(logical)
    waiter = install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 0),
            WaitStep("stop"),
        ],
    )
    starting = asyncio.create_task(writer.start())
    await sink.health_entered.wait()
    await waiter.first_wake_completed.wait()
    try:
        assert sink.attempts == []
        assert writer._flush_lock.locked()
    finally:
        sink.health_release.set()
        await starting
        await writer.worker_task

    assert sink.health_attempts == [0]
    assert sink.writes == [(logical,)]
    assert writer.failure_count == 0
    assert writer.buffered_count == writer.buffered_bytes == 0


@pytest.mark.asyncio
async def test_automatic_health_probe_rechecks_queue_after_waiting_for_flush_lock(
    monkeypatch,
):
    clock = VirtualClock()
    sink = RecordingSink(clock, fail_writes=set(), fail_health={1})
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    waiter = install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("timeout"),
            WaitStep("stop"),
        ],
    )
    assert not await writer._check_sink_health()
    await writer._flush_lock.acquire()
    worker = asyncio.create_task(writer._worker_loop())
    await waiter.first_wake_completed.wait()
    logical = record(1)
    try:
        assert sink.health_attempts == [0]
        writer.add_nowait(logical)
        writer._flush_lock.release()
        await worker
    finally:
        if writer._flush_lock.locked():
            writer._flush_lock.release()
        if not worker.done():
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    assert sink.health_attempts == [0]
    assert attempt_times(sink) == [1_000]
    assert sink.writes == [(logical,)]
    assert writer.health_snapshot().state == PersistenceHealthState.HEALTHY
    assert writer.buffered_count == writer.buffered_bytes == 0


@pytest.mark.asyncio
async def test_manual_success_wakes_long_backoff_and_restores_normal_wait(monkeypatch):
    clock = VirtualClock()
    sink = RecordingSink(clock, fail_writes={1})
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        flush_interval_ms=30_000,
        retry_backoff_ms=10_000,
        max_retry_delay_ms=40_000,
    )
    bind_worker(writer)
    waiter = install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("hold"),
            WaitStep("stop"),
        ],
    )
    logical = record(1)
    writer.add_nowait(logical)
    assert not (await writer.flush()).successful
    worker = asyncio.create_task(writer._worker_loop())
    await waiter.holding.wait()
    clock.advance_to(200)
    try:
        assert (await writer.flush()).successful
        await waiter.original_wait_for(worker, timeout=2)
    finally:
        if not worker.done():
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    assert waiter.requested_timeouts == [10.0, 30.0]
    assert attempt_times(sink) == [0, 200]
    assert sink.writes == [(logical,)]
    assert writer.health_snapshot().state == PersistenceHealthState.HEALTHY


@pytest.mark.asyncio
async def test_explicit_failure_wakes_worker_from_ordinary_interval(monkeypatch):
    clock = VirtualClock()
    sink = RecordingSink(clock, fail_writes={1})
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=10,
        flush_interval_ms=10_000,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    waiter = install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("hold"),
            WaitStep("timeout"),
            WaitStep("stop"),
        ],
    )
    worker = asyncio.create_task(writer._worker_loop())
    await waiter.holding.wait()
    clock.advance_to(100)
    logical = record(1)
    assert writer.add_nowait(logical) == PersistenceAddOutcome.ACCEPTED
    try:
        assert not (await writer.flush()).successful
        await waiter.original_wait_for(worker, timeout=2)
    finally:
        if not worker.done():
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    assert waiter.requested_timeouts == [10.0, 1.0, 10.0]
    assert attempt_times(sink) == [100, 1_100]
    assert sink.writes == [(logical,)]
    assert writer.failure_count == 1
    assert writer.buffered_count == writer.buffered_bytes == 0


class PausedManualRetrySink(RecordingSink):
    def __init__(self, clock) -> None:
        super().__init__(clock, fail_writes={1})
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def write(self, records) -> None:
        if len(self.attempts) == 1:
            self.attempts.append((self.clock.milliseconds, tuple(records)))
            self.entered.set()
            await self.release.wait()
            await InMemoryMetricsSink.write(self, records)
            return
        await super().write(records)


@pytest.mark.asyncio
async def test_canceled_manual_retry_preserves_existing_backoff_deadline(monkeypatch):
    clock = VirtualClock()
    sink = PausedManualRetrySink(clock)
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        retry_backoff_ms=1_000,
        max_retry_delay_ms=4_000,
    )
    bind_worker(writer)
    install_wait(
        monkeypatch,
        writer,
        clock,
        [
            WaitStep("wake", 200),
            WaitStep("timeout"),
            WaitStep("stop"),
        ],
    )
    logical = record(1)
    writer.add_nowait(logical)
    assert not (await writer.flush()).successful
    deadline_before = writer._retry_deadline_ns
    clock.advance_to(200)
    canceled = asyncio.create_task(writer.flush())
    await sink.entered.wait()
    canceled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await canceled

    assert writer._retry_deadline_ns == deadline_before == 1_000_000_000
    assert writer.failure_count == 1
    assert writer.health_snapshot().consecutive_failures == 1
    assert writer.buffered_count == 1
    assert writer.buffered_bytes == logical.encoded_size_bytes
    assert not writer._flush_lock.locked()
    await writer._worker_loop()

    assert attempt_times(sink) == [0, 200, 1_000]
    assert all(batch[0] is logical for _, batch in sink.attempts)
    assert sink.writes == [(logical,)]
    assert writer.failure_count == 1
    assert writer.buffered_count == writer.buffered_bytes == 0

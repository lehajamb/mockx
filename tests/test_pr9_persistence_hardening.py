"""PR9 bounded persistence admission, retry, health, and shutdown tests."""

from __future__ import annotations

import asyncio

import pytest

from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricRecord,
    MetricsSinkError,
    PersistenceAddOutcome,
    PersistenceCloseOutcome,
    PersistenceFlushOutcome,
    PersistenceHealthState,
)
from tests.exact_helpers import exact_metrics


def record(number: int, *, payload: str = "value") -> MetricRecord:
    return MetricRecord.create(
        "bounded",
        {"kind": "test"},
        {"number": number, "payload": payload},
        run_id="pr9-buffer",
        event_sequence=number,
        event_timestamp_ns=number * 1_000,
    )


class BlockingSink(InMemoryMetricsSink):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def write(self, records) -> None:
        self.started.set()
        await self.release.wait()
        await super().write(records)


class RecoveringSink(InMemoryMetricsSink):
    def __init__(self, *, fail_health: bool = False) -> None:
        super().__init__()
        self.fail_health = fail_health
        self.fail_writes = True
        self.attempts: list[tuple[MetricRecord, ...]] = []

    async def health_check(self) -> None:
        if self.fail_health:
            raise MetricsSinkError("planned health outage")

    async def write(self, records) -> None:
        batch = tuple(records)
        self.attempts.append(batch)
        if self.fail_writes:
            raise MetricsSinkError("planned write outage")
        await super().write(batch)


@pytest.mark.asyncio
async def test_writer_rejects_after_close_and_caller_observes_rejection():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=2)
    assert (await writer.close()).outcome == PersistenceCloseOutcome.CLOSED
    assert writer.add_nowait(record(1)) == PersistenceAddOutcome.REJECTED_CLOSED
    assert writer.health_snapshot().rejected_closed_records == 1

    metrics = exact_metrics("TEST", persistence=writer)
    metrics.start_run("closed-caller")
    assert metrics.mark_integrity_failure("REJECTED_CLOSED", event_sequence=1)
    assert metrics.persistence_integrity_failure.reason == "REJECTED_CLOSED"
    assert not metrics.mark_integrity_failure("later")


@pytest.mark.asyncio
async def test_capacity_counts_inflight_records_and_bytes():
    sink = BlockingSink()
    first = record(1)
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        max_buffer_records=2,
        max_buffer_bytes=first.encoded_size_bytes * 2,
        flush_interval_ms=10_000,
    )
    await writer.start()
    assert writer.add_nowait(first) == PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED
    await sink.started.wait()
    second = record(2)
    assert writer.add_nowait(second) == PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED
    assert writer.add_nowait(record(3)) == PersistenceAddOutcome.REJECTED_FULL
    assert writer.buffered_count == 2
    assert writer.buffered_bytes <= writer.max_buffer_bytes
    sink.release.set()
    assert (await writer.flush()).successful
    assert writer.buffered_count == 0
    assert (await writer.close()).successful


@pytest.mark.asyncio
async def test_oversized_record_is_rejected_without_buffer_growth():
    oversized = record(1, payload="x" * 1_000)
    writer = BufferedMetricsWriter(
        InMemoryMetricsSink(),
        flush_batch_size=1,
        max_buffer_records=1,
        max_buffer_bytes=oversized.encoded_size_bytes - 1,
    )
    assert writer.add_nowait(oversized) == PersistenceAddOutcome.TOO_LARGE
    assert writer.buffered_count == 0
    assert writer.buffered_bytes == 0
    await writer.close()


@pytest.mark.asyncio
async def test_one_worker_is_coalesced_and_interval_flushes_partial_batch():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=10,
        flush_interval_ms=10,
    )
    await writer.start()
    worker = writer.worker_task
    await asyncio.gather(writer.start(), writer.start(), writer.start())
    assert writer.worker_task is worker
    assert worker is not None and not worker.done()

    assert writer.add_nowait(record(1)) == PersistenceAddOutcome.ACCEPTED
    await asyncio.sleep(0.03)
    assert sink.writes == [(record(1),)]
    assert writer.worker_task is worker
    await writer.close()


@pytest.mark.asyncio
async def test_long_outage_remains_bounded_and_backoff_is_capped():
    sink = RecoveringSink(fail_health=True)
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=2,
        max_buffer_records=4,
        max_buffer_bytes=record(1).encoded_size_bytes * 4,
        flush_interval_ms=5,
        retry_backoff_ms=5,
        max_retry_delay_ms=20,
        shutdown_timeout_ms=100,
    )
    await writer.start()
    outcomes = [writer.add_nowait(record(index)) for index in range(1, 21)]
    await asyncio.sleep(0.08)
    assert writer.buffered_count <= 4
    assert writer.buffered_bytes <= writer.max_buffer_bytes
    assert outcomes.count(PersistenceAddOutcome.REJECTED_FULL) >= 16
    assert writer._current_retry_delay_ms <= writer.max_retry_delay_ms
    assert writer.health_snapshot().state in {
        PersistenceHealthState.RETRYING,
        PersistenceHealthState.UNAVAILABLE,
    }
    result = await writer.close()
    assert result.outcome == PersistenceCloseOutcome.CLOSED_WITH_UNFLUSHED
    assert result.unflushed_records == writer.buffered_count


@pytest.mark.asyncio
async def test_full_buffer_never_evicts_queued_or_inflight_records():
    sink = BlockingSink()
    sample = record(1)
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=1,
        max_buffer_records=2,
        max_buffer_bytes=sample.encoded_size_bytes * 2,
        flush_interval_ms=10_000,
    )
    await writer.start()
    writer.add_nowait(sample)
    await sink.started.wait()
    writer.add_nowait(record(2))
    assert writer.add_nowait(record(3)) == PersistenceAddOutcome.REJECTED_FULL
    assert writer.buffered_count == 2
    sink.release.set()
    assert (await writer.flush()).successful
    assert sink.writes == [(record(1),), (record(2),)]
    assert writer.health_snapshot().dropped_records == 0
    await writer.close()


@pytest.mark.asyncio
async def test_retry_preserves_identity_and_health_recovers_historically():
    sink = RecoveringSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=10)
    await writer.start()
    logical = record(7)
    writer.add_nowait(logical)
    first = await writer.flush()
    assert first.outcome == PersistenceFlushOutcome.FAILED
    failed_health = writer.health_snapshot()
    assert failed_health.consecutive_failures == 1
    assert failed_health.lifetime_failures == 1

    sink.fail_writes = False
    assert (await writer.flush()).successful
    recovered = writer.health_snapshot()
    assert recovered.state == PersistenceHealthState.HEALTHY
    assert recovered.consecutive_failures == 0
    assert recovered.lifetime_failures == 1
    assert sink.attempts[0][0] is sink.attempts[1][0] is logical
    await writer.close()


@pytest.mark.asyncio
async def test_shutdown_flushes_accepted_records_or_reports_exact_failure():
    healthy = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(healthy, flush_batch_size=10)
    writer.add_nowait(record(1))
    writer.add_nowait(record(2))
    closed = await writer.close()
    assert closed.outcome == PersistenceCloseOutcome.CLOSED
    assert closed.records_written == 2
    assert closed.unflushed_records == 0

    failing = RecoveringSink()
    failed_writer = BufferedMetricsWriter(failing, flush_batch_size=10)
    failed_writer.add_nowait(record(3))
    failed = await failed_writer.close()
    assert failed.outcome == PersistenceCloseOutcome.CLOSED_WITH_UNFLUSHED
    assert failed.unflushed_records == 1


@pytest.mark.asyncio
async def test_shutdown_timeout_reports_exact_unflushed_records():
    sink = BlockingSink()
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=10,
        shutdown_timeout_ms=10,
    )
    writer.add_nowait(record(1))
    result = await writer.close()
    assert result.outcome == PersistenceCloseOutcome.TIMED_OUT
    assert result.records_written == 0
    assert result.unflushed_records == 1
    assert writer.closed

"""Cancellation must not discard persistence batches or owned broadcasts."""

import asyncio
from concurrent.futures import Future

import pytest

from src.mockx.lifecycle import LifecycleState
from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricRecord,
    PersistenceAddOutcome,
)
from tests.api_helpers import make_exchange
from tests.exact_helpers import exact_metrics


class PausedSink(InMemoryMetricsSink):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_write = False

    async def write(self, records):
        self.entered.set()
        await self.release.wait()
        if self.cancel_write:
            raise asyncio.CancelledError("sink canceled its write")
        await super().write(records)


def _record(number):
    return MetricRecord.create(
        "cancellation",
        {},
        {"value": number},
        run_id="cancel-run",
        event_sequence=number,
        event_timestamp_ns=number,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("internal_cancellation", [False, True])
async def test_canceled_write_restores_fifo_identity_and_capacity(
    internal_cancellation,
):
    sink = PausedSink()
    records = [_record(number) for number in range(1, 4)]
    byte_capacity = sum(record.encoded_size_bytes for record in records)
    writer = BufferedMetricsWriter(
        sink,
        flush_batch_size=2,
        max_buffer_records=3,
        max_buffer_bytes=byte_capacity,
    )
    metrics = exact_metrics("TEST", persistence=writer, prometheus_enabled=False)
    metrics.start_run("cancel-run")
    for record in records[:2]:
        writer.add_nowait(record)
    writing = asyncio.create_task(writer.flush())
    await asyncio.wait_for(sink.entered.wait(), timeout=2)
    # New admissions remain behind the batch whose write is interrupted.
    writer.add_nowait(records[2])
    assert writer.buffered_count == 3
    assert writer.buffered_bytes == byte_capacity
    if internal_cancellation:
        sink.cancel_write = True
        sink.release.set()
    else:
        writing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writing

    assert writer._inflight == ()
    assert writer.buffered_count == 3
    assert writer.buffered_bytes == byte_capacity
    assert writer.failure_count == 0
    assert not writer._flush_lock.locked()
    assert metrics.integrity_complete is True
    sink.cancel_write = False
    sink.release.set()
    assert (await writer.flush()).successful
    persisted = [record for batch in sink.writes for record in batch]
    assert persisted == records
    assert [record.event_sequence for record in persisted] == [1, 2, 3]
    assert writer.buffered_count == writer.buffered_bytes == 0
    assert writer.add_nowait(_record(4)) == PersistenceAddOutcome.ACCEPTED
    assert (await writer.close()).successful


@pytest.mark.asyncio
async def test_canceled_worker_can_restart_and_flush_the_retained_batch():
    sink = PausedSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=2, max_buffer_records=3)
    records = [_record(number) for number in range(1, 3)]
    await writer.start()
    for record in records:
        writer.add_nowait(record)
    await asyncio.wait_for(sink.entered.wait(), timeout=2)
    worker = writer.worker_task
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert writer._inflight == ()
    assert writer.buffered_count == 2
    assert writer.failure_count == 0
    sink.release.set()
    await writer.start()
    assert writer.worker_task is not worker
    assert (await writer.flush()).successful
    assert sink.writes == [tuple(records)]
    assert writer.buffered_count == writer.buffered_bytes == 0
    assert (await writer.close()).successful
    assert writer.worker_task.done()


@pytest.mark.asyncio
async def test_cancel_accepted_end_finishes_final_checkpoint_and_journal():
    from tests.api_helpers import api_config, make_native_exchange
    from src.mockx.configuration import validate_config

    sink = PausedSink()
    sink.release.set()
    exchange = make_native_exchange(
        validate_config(api_config(), environ={}), persistence_sink=sink
    )
    await exchange.start_components()
    await exchange.lifecycle.start_session("cancel-end")
    sink.entered.clear()
    sink.release.clear()
    ending = asyncio.create_task(exchange.lifecycle.end_session())
    try:
        await asyncio.wait_for(sink.entered.wait(), timeout=2)
        ending.cancel()
        await asyncio.sleep(0)
        assert not ending.done()
        sink.release.set()
        with pytest.raises(asyncio.CancelledError):
            await ending
        assert exchange.lifecycle.state == LifecycleState.STOPPED
        kinds = [record.symbol_map["kind"] for batch in sink.writes for record in batch]
        assert "CHECKPOINT_COMPLETE" in kinds
        assert "RUN_ENDED" in kinds
        assert not exchange.lifecycle.lock.locked()
    finally:
        sink.release.set()
        await asyncio.gather(ending, return_exceptions=True)
        await exchange.stop_components()

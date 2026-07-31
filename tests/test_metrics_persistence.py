from tests.exact_helpers import exact_engine, exact_metrics
from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""Buffered metric persistence reliability tests."""

import asyncio

import pytest

from src.mockx.engine import MatchingEngine
from src.mockx.latency import LatencyScheduler
from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricRecord,
    MetricsCollector,
    MetricsSinkError,
    PersistenceAddOutcome,
    PersistenceCloseOutcome,
    PersistenceFlushOutcome,
)
from src.mockx.models import (
    ExecutionParams,
    NewOrderCommand,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)


def record(number):
    return MetricRecord.create(
        "test",
        {"sequence": number},
        {"value": float(number)},
        run_id="buffer-test",
        metric_event_sequence=number,
        event_timestamp_ns=number * 1_000,
    )


class FailOnceSink(InMemoryMetricsSink):
    def __init__(self):
        super().__init__()
        self.fail_next = True

    async def write(self, records):
        if self.fail_next:
            self.fail_next = False
            raise MetricsSinkError("planned failure")
        await super().write(records)


class BlockingSink(InMemoryMetricsSink):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def write(self, records):
        self.started.set()
        await self.release.wait()
        await super().write(records)


@pytest.mark.asyncio
async def test_flushes_exact_batch_at_threshold():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=3)

    assert await writer.add(record(1)) == PersistenceAddOutcome.ACCEPTED
    assert await writer.add(record(2)) == PersistenceAddOutcome.ACCEPTED
    assert (
        await writer.add(record(3))
        == PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED
    )
    assert (await writer.flush()).successful

    assert sink.writes == [(record(1), record(2), record(3))]
    assert writer.buffered_count == 0


@pytest.mark.asyncio
async def test_close_flushes_records_below_threshold():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=3)
    await writer.add(record(1))
    await writer.add(record(2))

    assert (await writer.close()).successful

    assert sink.writes == [(record(1), record(2))]
    assert sink.close_calls == 1
    assert writer.buffered_count == 0


@pytest.mark.asyncio
async def test_collector_shutdown_flushes_attached_writer():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=3)
    metrics = exact_metrics("TEST", persistence=writer)
    metrics.start_run("shutdown")
    metrics._create_record(
        "shutdown",
        {"exchange": "TEST"},
        {"value": 1.0},
        1_000,
    )

    assert (await metrics.close()).successful

    assert len(sink.writes) == 1
    assert sink.writes[0][0].table == "shutdown"
    assert sink.close_calls == 1


@pytest.mark.asyncio
async def test_failed_flush_retains_ordered_records_for_one_retry():
    sink = FailOnceSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=10)
    await writer.add(record(1))
    await writer.add(record(2))

    assert (await writer.flush()).outcome == PersistenceFlushOutcome.FAILED
    assert writer.buffered_count == 2
    assert sink.writes == []

    assert (await writer.flush()).successful
    assert sink.writes == [(record(1), record(2))]
    assert writer.buffered_count == 0


@pytest.mark.asyncio
async def test_failed_final_close_is_explicit_and_seals_acceptance():
    sink = FailOnceSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=10)
    await writer.add(record(1))

    result = await writer.close()
    assert result.outcome == PersistenceCloseOutcome.CLOSED_WITH_UNFLUSHED
    assert result.unflushed_records == 1
    assert writer.closed
    assert writer.buffered_count == 1
    assert writer.add_nowait(record(2)) == PersistenceAddOutcome.REJECTED_CLOSED
    assert (await writer.close()).outcome == PersistenceCloseOutcome.ALREADY_CLOSED
    assert sink.writes == []


@pytest.mark.asyncio
async def test_record_added_during_flush_remains_for_next_batch():
    sink = BlockingSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=10)
    await writer.add(record(1))
    flush_task = asyncio.create_task(writer.flush())
    await sink.started.wait()

    await writer.add(record(2))
    assert writer.buffered_count == 2
    sink.release.set()
    assert (await flush_task).successful

    assert writer.buffered_count == 0
    assert sink.writes == [(record(1),), (record(2),)]


@pytest.mark.asyncio
async def test_repeated_close_is_idempotent():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=10)
    await writer.add(record(1))

    assert (await writer.close()).outcome == PersistenceCloseOutcome.CLOSED
    assert (
        (await writer.close()).outcome
        == PersistenceCloseOutcome.ALREADY_CLOSED
    )

    assert sink.writes == [(record(1),)]
    assert sink.close_calls == 1


@pytest.mark.asyncio
async def test_empty_flush_does_not_call_sink_write():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=10)

    assert (
        (await writer.flush()).outcome
        == PersistenceFlushOutcome.NOTHING_TO_FLUSH
    )

    assert sink.writes == []


@pytest.mark.asyncio
async def test_sink_failure_does_not_roll_back_valid_trade(caplog):
    sink = FailOnceSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=100)
    metrics = exact_metrics("TEST", persistence=writer)
    metrics.start_run("sink-failure")
    scheduler = LatencyScheduler()
    engine = exact_engine(
        ExecutionParams(
            network_latency_ms=0,
            engine_latency_ms=0,
            reject_prob=0.0,
            rand_seed=42,
        ),
        scheduler,
    )
    metrics.bind_engine(engine)
    engine.on_order_submitted = metrics.record_order_submitted
    engine.on_execution_report = metrics.record_order
    engine.on_trade = metrics.record_trade

    def submit(cl_ord_id, side):
        order_id = engine.submit_order(
            exact_command(
                cl_ord_id=cl_ord_id,
                participant_id=cl_ord_id,
                symbol="TEST",
                side=side,
                order_qty_lots=5,
                price_ticks=100,
                tif=TimeInForce.GTC,
                submitted_at=scheduler.current_time,
                session_id="FIX.4.4:MOCKX->CLIENT",
                session_alias="CLIENT",
            )
        )
        engine.process_order(ProcessOrderCommand(order_id, 0))
        return order_id

    passive_id = submit("PASSIVE", Side.SELL)
    aggressor_id = submit("AGGRESSOR", Side.BUY)
    buffered_before = writer.buffered_count

    with caplog.at_level("ERROR"):
        assert (await writer.flush()).outcome == PersistenceFlushOutcome.FAILED

    assert engine.orders[passive_id].filled_qty_lots == 5.0
    assert engine.orders[aggressor_id].filled_qty_lots == 5.0
    assert not engine.books["TEST"].bids
    assert not engine.books["TEST"].asks
    engine.books["TEST"].assert_invariants()
    assert metrics.snapshot().trades == 1
    assert writer.buffered_count == buffered_before
    assert metrics.snapshot().metric_sink_failures == 1
    assert any(
        "retained for retry" in record.getMessage()
        for record in caplog.records
    )

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
        number,
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
    writer = BufferedMetricsWriter(sink, batch_size=3)

    assert await writer.add(record(1))
    assert await writer.add(record(2))
    assert await writer.add(record(3))

    assert sink.writes == [(record(1), record(2), record(3))]
    assert writer.buffered_count == 0


@pytest.mark.asyncio
async def test_close_flushes_records_below_threshold():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, batch_size=3)
    await writer.add(record(1))
    await writer.add(record(2))

    assert await writer.close()

    assert sink.writes == [(record(1), record(2))]
    assert sink.close_calls == 1
    assert writer.buffered_count == 0


@pytest.mark.asyncio
async def test_collector_shutdown_flushes_attached_writer():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, batch_size=3)
    metrics = MetricsCollector("TEST", persistence=writer)
    metrics._create_point(
        "shutdown",
        {"exchange": "TEST"},
        {"value": 1.0},
        1,
    )

    assert await metrics.close()

    assert len(sink.writes) == 1
    assert sink.writes[0][0].measurement == "shutdown"
    assert sink.close_calls == 1


@pytest.mark.asyncio
async def test_failed_flush_retains_ordered_records_for_one_retry():
    sink = FailOnceSink()
    writer = BufferedMetricsWriter(sink, batch_size=10)
    await writer.add(record(1))
    await writer.add(record(2))

    assert not await writer.flush()
    assert writer.buffered_count == 2
    assert sink.writes == []

    assert await writer.flush()
    assert sink.writes == [(record(1), record(2))]
    assert writer.buffered_count == 0


@pytest.mark.asyncio
async def test_failed_final_close_remains_retryable():
    sink = FailOnceSink()
    writer = BufferedMetricsWriter(sink, batch_size=10)
    await writer.add(record(1))

    assert not await writer.close()
    assert not writer.closed
    assert writer.buffered_count == 1

    assert await writer.close()
    assert writer.closed
    assert sink.writes == [(record(1),)]
    assert sink.close_calls == 1


@pytest.mark.asyncio
async def test_record_added_during_flush_remains_for_next_batch():
    sink = BlockingSink()
    writer = BufferedMetricsWriter(sink, batch_size=10)
    await writer.add(record(1))
    flush_task = asyncio.create_task(writer.flush())
    await sink.started.wait()

    await writer.add(record(2))
    assert writer.buffered_count == 1
    sink.release.set()
    assert await flush_task

    assert sink.writes == [(record(1),)]
    assert writer.buffered_count == 1
    assert await writer.flush()
    assert sink.writes == [(record(1),), (record(2),)]


@pytest.mark.asyncio
async def test_repeated_close_is_idempotent():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, batch_size=10)
    await writer.add(record(1))

    assert await writer.close()
    assert await writer.close()

    assert sink.writes == [(record(1),)]
    assert sink.close_calls == 1


@pytest.mark.asyncio
async def test_empty_flush_does_not_call_sink_write():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, batch_size=10)

    assert await writer.flush()

    assert sink.writes == []


@pytest.mark.asyncio
async def test_sink_failure_does_not_roll_back_valid_trade(caplog):
    sink = FailOnceSink()
    writer = BufferedMetricsWriter(sink, batch_size=100)
    metrics = MetricsCollector("TEST", persistence=writer)
    scheduler = LatencyScheduler()
    engine = MatchingEngine(
        ExecutionParams(
            network_latency_ms=0,
            engine_latency_ms=0,
            reject_prob=0.0,
            rand_seed=42,
        ),
        scheduler,
    )
    engine.add_symbol("TEST")
    metrics.bind_engine(engine)
    engine.on_order_submitted = metrics.record_order_submitted
    engine.on_execution_report = metrics.record_order
    engine.on_trade = metrics.record_trade

    def submit(cl_ord_id, side):
        order_id = engine.submit_order(
            NewOrderCommand(
                cl_ord_id=cl_ord_id,
                symbol="TEST",
                side=side,
                qty=5.0,
                price=100.0,
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
        assert not await writer.flush()

    assert engine.orders[passive_id].filled_qty == 5.0
    assert engine.orders[aggressor_id].filled_qty == 5.0
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

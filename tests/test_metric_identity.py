"""Deterministic persisted metric identity regression tests."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from src.mockx.engine import MatchingEngine
from src.mockx.latency import LatencyScheduler
from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricsCollector,
    MetricsSinkError,
    SIM_ENGINE_LATENCY,
)
from src.mockx.models import (
    ExecutionParams,
    LatencyMeasurement,
    NewOrderCommand,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)
from src.mockx.tsdb_writer import InfluxDBMetricsSink
from tests.metrics_helpers import (
    assert_unique_influx_identities,
    influx_identity,
)


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
EPOCH_NS = 0


def make_metrics(*, run_id: str = "identity-run", batch_size: int = 10_000):
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, batch_size=batch_size)
    metrics = MetricsCollector("TEST", persistence=writer)
    metrics.start_run(run_id)
    return metrics, writer, sink


async def flush_records(
    writer: BufferedMetricsWriter,
    sink: InMemoryMetricsSink,
):
    assert await writer.flush()
    return tuple(record for batch in sink.writes for record in batch)


@pytest.mark.asyncio
async def test_identical_order_points_at_same_timestamp_have_unique_identity():
    metrics, writer, sink = make_metrics()
    tags = {"exchange": "TEST", "symbol": "TEST", "status": "NEW"}
    fields = {"order_id": 1, "qty": 1.0}

    metrics._create_point("orders", tags, fields, EPOCH_NS)
    metrics._create_point("orders", tags, fields, EPOCH_NS)
    records = await flush_records(writer, sink)

    assert [record.field_map["metric_event_sequence"] for record in records] == [
        1,
        2,
    ]
    assert [record.persistence_timestamp_ns for record in records] == [0, 1]
    assert_unique_influx_identities(records)


@pytest.mark.asyncio
async def test_multi_fill_and_both_report_sides_share_timestamp_without_collision():
    metrics, writer, sink = make_metrics()
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

    def submit(cl_ord_id, side, qty):
        order_id = engine.submit_order(
            NewOrderCommand(
                cl_ord_id=cl_ord_id,
                symbol="TEST",
                side=side,
                qty=qty,
                price=100.0,
                tif=TimeInForce.GTC,
                submitted_at=scheduler.current_time,
                session_id="FIX.4.4:MOCKX->CLIENT",
                session_alias="CLIENT",
            )
        )
        engine.process_order(ProcessOrderCommand(order_id, 0))

    submit("P1", Side.SELL, 1.0)
    submit("P2", Side.SELL, 2.0)
    submit("P3", Side.SELL, 3.0)
    submit("TAKER", Side.BUY, 6.0)

    records = await flush_records(writer, sink)
    trades = [record for record in records if record.measurement == "trades"]
    trade_reports = [
        record
        for record in records
        if record.measurement == "orders" and "exchange_trade_id" in record.field_map
    ]

    assert len(trades) == 3
    assert len(trade_reports) == 6
    assert {record.event_timestamp_ns for record in (*trades, *trade_reports)} == {
        EPOCH_NS
    }
    assert all("exec_id" in record.field_map for record in trade_reports)
    assert all("exchange_trade_id" in record.field_map for record in trades)
    assert_unique_influx_identities(records)


@pytest.mark.asyncio
async def test_repeated_latency_points_preserve_domain_ids_and_identity():
    metrics, writer, sink = make_metrics()
    for order_id, exec_id in ((1, "E1"), (2, "E2")):
        assert metrics.observe_latency(
            LatencyMeasurement(
                order_id=order_id,
                session_id="SESSION",
                metric=SIM_ENGINE_LATENCY,
                value=0.001,
                unit="seconds",
                timestamp=EPOCH,
                report_exec_id=exec_id,
            )
        )

    records = await flush_records(writer, sink)

    assert [record.field_map["order_id"] for record in records] == [1, 2]
    assert [record.field_map["session_id"] for record in records] == [
        "SESSION",
        "SESSION",
    ]
    assert [record.field_map["report_exec_id"] for record in records] == [
        "E1",
        "E2",
    ]
    assert_unique_influx_identities(records)


@pytest.mark.asyncio
async def test_book_and_order_points_share_timestamp_and_global_sequence():
    metrics, writer, sink = make_metrics()
    metrics._create_point(
        "orders",
        {"exchange": "TEST", "symbol": "TEST"},
        {"order_id": 1},
        EPOCH_NS,
    )
    metrics._create_point(
        "book",
        {"exchange": "TEST", "symbol": "TEST"},
        {"best_bid": 99.0, "best_ask": 101.0},
        EPOCH_NS,
    )

    records = await flush_records(writer, sink)

    assert [record.measurement for record in records] == ["orders", "book"]
    assert [record.field_map["metric_event_sequence"] for record in records] == [
        1,
        2,
    ]
    assert_unique_influx_identities(records)


@pytest.mark.asyncio
async def test_replay_is_deterministic_and_new_run_resets_sequence():
    async def produce(run_id):
        metrics, writer, sink = make_metrics(run_id=run_id)
        for measurement in ("orders", "latency", "book"):
            metrics._create_point(
                measurement,
                {"exchange": "TEST"},
                {"value": 1},
                EPOCH_NS,
            )
        return await flush_records(writer, sink)

    first = await produce("replay")
    replay = await produce("replay")
    other_run = await produce("other-run")

    assert first == replay
    assert [record.field_map["metric_event_sequence"] for record in other_run] == [
        1,
        2,
        3,
    ]
    assert [influx_identity(record) for record in first] != [
        influx_identity(record) for record in other_run
    ]


@pytest.mark.asyncio
async def test_sequence_resets_on_next_run_in_same_collector():
    metrics, writer, sink = make_metrics(run_id="first-run")
    metrics._create_point(
        "orders",
        {"exchange": "TEST"},
        {"order_id": 1},
        EPOCH_NS,
    )
    first = await flush_records(writer, sink)
    metrics.end_run()

    metrics.start_run("second-run")
    metrics._create_point(
        "orders",
        {"exchange": "TEST"},
        {"order_id": 1},
        EPOCH_NS,
    )
    assert await writer.flush()
    second = sink.writes[-1]

    assert first[0].metric_event_sequence == 1
    assert second[0].metric_event_sequence == 1
    assert first[0].run_id == "first-run"
    assert second[0].run_id == "second-run"
    assert influx_identity(first[0]) != influx_identity(second[0])


@pytest.mark.asyncio
async def test_same_timestamp_overflow_uses_low_cardinality_buckets():
    metrics, writer, sink = make_metrics(batch_size=10_000)
    for value in range(2_005):
        metrics._create_point(
            "orders",
            {"exchange": "TEST"},
            {"value": value},
            EPOCH_NS,
        )

    records = await flush_records(writer, sink)

    assert [
        records[index].tag_map["metric_event_bucket"]
        for index in (
            0,
            999,
            1_000,
            1_999,
            2_000,
            2_004,
        )
    ] == ["0", "0", "1", "1", "2", "2"]
    assert max(record.persistence_timestamp_ns for record in records) < 1_000
    assert [record.field_map["metric_event_sequence"] for record in records] == list(
        range(1, 2_006)
    )
    assert_unique_influx_identities(records)


@pytest.mark.asyncio
async def test_concurrent_producers_allocate_unique_contiguous_sequence():
    metrics, writer, sink = make_metrics()
    loop = asyncio.get_running_loop()

    def produce(value):
        metrics._create_point(
            "latency",
            {"exchange": "TEST", "type": "concurrent"},
            {"value": value},
            EPOCH_NS,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        await asyncio.gather(
            *(loop.run_in_executor(executor, produce, value) for value in range(200))
        )

    records = await flush_records(writer, sink)
    sequences = sorted(record.field_map["metric_event_sequence"] for record in records)

    assert sequences == list(range(1, 201))
    assert_unique_influx_identities(records)


class RecordingFailOnceSink(InMemoryMetricsSink):
    def __init__(self):
        super().__init__()
        self.attempts = []

    async def write(self, records):
        self.attempts.append(tuple(records))
        if len(self.attempts) == 1:
            raise MetricsSinkError("planned write failure")
        await super().write(records)


@pytest.mark.asyncio
async def test_failed_write_retry_preserves_exact_identity_without_reallocation():
    sink = RecordingFailOnceSink()
    writer = BufferedMetricsWriter(sink, batch_size=10)
    metrics = MetricsCollector("TEST", persistence=writer)
    metrics.start_run("retry")
    metrics._create_point(
        "orders",
        {"exchange": "TEST"},
        {"order_id": 1},
        EPOCH_NS,
    )

    assert not await writer.flush()
    assert await writer.flush()

    assert sink.attempts[0] == sink.attempts[1]
    assert influx_identity(sink.attempts[0][0]) == influx_identity(sink.attempts[1][0])
    assert sink.attempts[1][0].field_map["metric_event_sequence"] == 1


def test_influx_conversion_preserves_all_identity_metadata(monkeypatch):
    class FakePoint:
        def __init__(self, measurement):
            self.measurement = measurement
            self.tags = {}
            self.fields = {}
            self.timestamp_ns = None
            self.precision = None

        def tag(self, key, value):
            self.tags[key] = value
            return self

        def field(self, key, value):
            self.fields[key] = value
            return self

        def time(self, timestamp_ns, write_precision):
            self.timestamp_ns = timestamp_ns
            self.precision = write_precision
            return self

    monkeypatch.setattr("src.mockx.tsdb_writer.Point", FakePoint)
    metrics, writer, _ = make_metrics()
    for order_id in range(7, 10):
        metrics._create_point(
            "orders",
            {"exchange": "TEST"},
            {"order_id": order_id, "exec_id": f"E{order_id}"},
            EPOCH_NS,
        )
    with writer._buffer_lock:
        source = tuple(writer._buffer)

    points = [InfluxDBMetricsSink._to_point(record) for record in source]

    assert len(points) == len(source) == 3
    assert [point.tags for point in points] == [record.tag_map for record in source]
    assert [point.fields for point in points] == [record.field_map for record in source]
    assert [point.timestamp_ns for point in points] == [
        record.persistence_timestamp_ns for record in source
    ]
    assert [point.fields["metric_event_sequence"] for point in points] == [1, 2, 3]
    assert {point.fields["event_timestamp_ns"] for point in points} == {EPOCH_NS}
    assert {point.precision for point in points} == {"ns"}


def test_persistence_rejects_missing_run_backdating_and_submicrosecond_input():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink)
    metrics = MetricsCollector("TEST", persistence=writer)

    with pytest.raises(RuntimeError, match="active simulation run"):
        metrics._create_point("orders", {}, {"value": 1}, EPOCH_NS)

    metrics.start_run("validation")
    metrics._create_point("orders", {}, {"value": 1}, 1_000)
    with pytest.raises(ValueError, match="backdated"):
        metrics._create_point("orders", {}, {"value": 2}, EPOCH_NS)
    with pytest.raises(ValueError, match="microsecond-aligned"):
        metrics._create_point("orders", {}, {"value": 3}, 1_001)

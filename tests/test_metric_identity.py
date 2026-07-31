from tests.exact_helpers import exact_engine, exact_metrics
from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
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
    SIM_CANCEL_ENGINE_LATENCY,
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
from tests.metrics_helpers import (
    assert_unique_questdb_identities,
    questdb_identity,
)


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
EPOCH_NS = 0


def make_metrics(*, run_id: str = "identity-run", batch_size: int = 10_000):
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=batch_size)
    metrics = exact_metrics("TEST", persistence=writer)
    metrics.start_run(run_id)
    return metrics, writer, sink


async def flush_records(
    writer: BufferedMetricsWriter,
    sink: InMemoryMetricsSink,
):
    assert (await writer.flush()).successful
    return tuple(record for batch in sink.writes for record in batch)


@pytest.mark.asyncio
async def test_identical_order_points_at_same_timestamp_have_unique_identity():
    metrics, writer, sink = make_metrics()
    tags = {"exchange": "TEST", "symbol": "TEST", "status": "NEW"}
    fields = {"order_id": 1, "qty": 1.0}

    metrics._create_record("orders", tags, fields, EPOCH_NS)
    metrics._create_record("orders", tags, fields, EPOCH_NS)
    records = await flush_records(writer, sink)

    assert [record.column_map["metric_event_sequence"] for record in records] == [
        1,
        2,
    ]
    assert [record.timestamp_ns for record in records] == [0, 0]
    assert_unique_questdb_identities(records)


@pytest.mark.asyncio
async def test_multi_fill_and_both_report_sides_share_timestamp_without_collision():
    metrics, writer, sink = make_metrics()
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

    def submit(cl_ord_id, side, qty):
        order_id = engine.submit_order(
            exact_command(
                cl_ord_id=cl_ord_id,
                participant_id=cl_ord_id,
                symbol="TEST",
                side=side,
                order_qty_lots=qty,
                price_ticks=100,
                tif=TimeInForce.GTC,
                submitted_at=scheduler.current_time,
                session_id="FIX.4.4:MOCKX->CLIENT",
                session_alias="CLIENT",
            )
        )
        engine.process_order(ProcessOrderCommand(order_id, 0))

    submit("P1", Side.SELL, 1)
    submit("P2", Side.SELL, 2)
    submit("P3", Side.SELL, 3)
    submit("TAKER", Side.BUY, 6)

    records = await flush_records(writer, sink)
    trades = [record for record in records if record.table == "trades"]
    trade_reports = [
        record
        for record in records
        if record.table == "orders"
        and "exchange_trade_id" in record.column_map
    ]

    assert len(trades) == 3
    assert len(trade_reports) == 6
    assert {record.timestamp_ns for record in (*trades, *trade_reports)} == {
        EPOCH_NS
    }
    assert all("exec_id" in record.column_map for record in trade_reports)
    assert all("exchange_trade_id" in record.column_map for record in trades)
    assert_unique_questdb_identities(records)


@pytest.mark.asyncio
async def test_repeated_latency_points_preserve_domain_ids_and_identity():
    metrics, writer, sink = make_metrics()
    for order_id, exec_id in ((1, "E1"), (2, "E2")):
        assert metrics.observe_latency(
            LatencyMeasurement(
                order_id=order_id,
                session_id="SESSION",
                metric=SIM_ENGINE_LATENCY,
                value=1.0,
                unit="ms",
                timestamp=EPOCH,
                report_exec_id=exec_id,
            )
        )

    records = await flush_records(writer, sink)

    assert [record.column_map["order_id"] for record in records] == [1, 2]
    assert [record.column_map["session_id"] for record in records] == [
        "SESSION",
        "SESSION",
    ]
    assert [record.column_map["report_exec_id"] for record in records] == [
        "E1",
        "E2",
    ]
    assert_unique_questdb_identities(records)


@pytest.mark.asyncio
async def test_cancel_latency_persists_ingress_and_request_identity():
    metrics, writer, sink = make_metrics()
    assert metrics.observe_latency(
        LatencyMeasurement(
            order_id=None,
            session_id="FIX.4.4:MOCKX->CLIENT",
            metric=SIM_CANCEL_ENGINE_LATENCY,
            value=3.0,
            unit="ms",
            timestamp=EPOCH,
            run_generation=7,
            ingress_sequence=11,
            cl_ord_id="CXL-11",
            orig_cl_ord_id="ORDER-7",
        )
    )

    record = (await flush_records(writer, sink))[0]
    assert record.column_map["run_generation"] == 7
    assert record.column_map["ingress_sequence"] == 11
    assert record.column_map["cl_ord_id"] == "CXL-11"
    assert record.column_map["orig_cl_ord_id"] == "ORDER-7"
    assert record.column_map["session_id"] == "FIX.4.4:MOCKX->CLIENT"
    assert "order_id" not in record.column_map


@pytest.mark.asyncio
async def test_book_and_order_points_share_timestamp_and_global_sequence():
    metrics, writer, sink = make_metrics()
    metrics._create_record(
        "orders",
        {"exchange": "TEST", "symbol": "TEST"},
        {"order_id": 1},
        EPOCH_NS,
    )
    metrics._create_record(
        "book_snapshot",
        {"exchange": "TEST", "symbol": "TEST"},
        {"best_bid": 99.0, "best_ask": 101.0},
        EPOCH_NS,
    )

    records = await flush_records(writer, sink)

    assert [record.table for record in records] == [
        "orders",
        "book_snapshot",
    ]
    assert [record.column_map["metric_event_sequence"] for record in records] == [
        1,
        2,
    ]
    assert_unique_questdb_identities(records)


@pytest.mark.asyncio
async def test_replay_is_deterministic_and_new_run_resets_sequence():
    async def produce(run_id):
        metrics, writer, sink = make_metrics(run_id=run_id)
        for table in ("orders", "latency", "book_snapshot"):
            metrics._create_record(
                table,
                {"exchange": "TEST"},
                {"value": 1},
                EPOCH_NS,
            )
        return await flush_records(writer, sink)

    first = await produce("replay")
    replay = await produce("replay")
    other_run = await produce("other-run")

    assert first == replay
    assert [record.column_map["metric_event_sequence"] for record in other_run] == [
        1,
        2,
        3,
    ]
    assert [questdb_identity(record) for record in first] != [
        questdb_identity(record) for record in other_run
    ]


@pytest.mark.asyncio
async def test_sequence_resets_on_next_run_in_same_collector():
    metrics, writer, sink = make_metrics(run_id="first-run")
    metrics._create_record(
        "orders",
        {"exchange": "TEST"},
        {"order_id": 1},
        EPOCH_NS,
    )
    first = await flush_records(writer, sink)
    metrics.end_run()

    metrics.start_run("second-run")
    metrics._create_record(
        "orders",
        {"exchange": "TEST"},
        {"order_id": 1},
        EPOCH_NS,
    )
    assert (await writer.flush()).successful
    second = sink.writes[-1]

    assert first[0].metric_event_sequence == 1
    assert second[0].metric_event_sequence == 1
    assert first[0].run_id == "first-run"
    assert second[0].run_id == "second-run"
    assert questdb_identity(first[0]) != questdb_identity(second[0])


@pytest.mark.asyncio
async def test_many_same_timestamp_records_keep_the_actual_timestamp():
    metrics, writer, sink = make_metrics(batch_size=10_000)
    for value in range(2_005):
        metrics._create_record(
            "orders",
            {"exchange": "TEST"},
            {"value": value},
            EPOCH_NS,
        )

    records = await flush_records(writer, sink)

    assert {record.timestamp_ns for record in records} == {EPOCH_NS}
    assert [
        record.column_map["metric_event_sequence"] for record in records
    ] == list(range(1, 2_006))
    assert_unique_questdb_identities(records)


@pytest.mark.asyncio
async def test_concurrent_producers_allocate_unique_contiguous_sequence():
    metrics, writer, sink = make_metrics()
    loop = asyncio.get_running_loop()

    def produce(value):
        metrics._create_record(
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
    sequences = sorted(
        record.column_map["metric_event_sequence"] for record in records
    )

    assert sequences == list(range(1, 201))
    assert_unique_questdb_identities(records)


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
    writer = BufferedMetricsWriter(sink, flush_batch_size=10)
    metrics = exact_metrics("TEST", persistence=writer)
    metrics.start_run("retry")
    metrics._create_record(
        "orders",
        {"exchange": "TEST"},
        {"order_id": 1},
        EPOCH_NS,
    )

    assert not (await writer.flush()).successful
    assert (await writer.flush()).successful

    assert sink.attempts[0] == sink.attempts[1]
    assert questdb_identity(sink.attempts[0][0]) == questdb_identity(
        sink.attempts[1][0]
    )
    assert sink.attempts[1][0].column_map["metric_event_sequence"] == 1


def test_persistence_rejects_missing_run_and_backdating_but_accepts_nanoseconds():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink)
    metrics = exact_metrics("TEST", persistence=writer)

    with pytest.raises(RuntimeError, match="active simulation run"):
        metrics._create_record("orders", {}, {"value": 1}, EPOCH_NS)

    metrics.start_run("validation")
    metrics._create_record("orders", {}, {"value": 1}, 1_000)
    metrics._create_record("orders", {}, {"value": 2}, 1_001)
    with pytest.raises(ValueError, match="backdated"):
        metrics._create_record("orders", {}, {"value": 3}, EPOCH_NS)

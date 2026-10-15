"""Deterministic identities for the unified native command journal."""

import asyncio
from datetime import datetime, timezone

import pytest
from src.mockx.effects import CommandEffect, CommandEffects
from src.mockx.journal import ExchangeJournal
from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricRecord,
    MetricsSinkError,
    SIM_CANCEL_ENGINE_LATENCY,
    SIM_ENGINE_LATENCY,
)
from src.mockx.models import (
    ExecutionParams,
    LatencyMeasurement,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)
from src.mockx.latency import LatencyScheduler
from tests.exact_helpers import TEST_INSTRUMENT, exact_engine, exact_command
from tests.metrics_helpers import assert_unique_questdb_identities, questdb_identity

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


async def setup(run_id="run", sink=None):
    sink = sink or InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=10_000)
    journal = ExchangeJournal(writer, "TEST")
    await journal.start_run(run_id, "same scenario", (TEST_INSTRUMENT,), EPOCH)
    return journal, writer, sink


async def records(writer, sink):
    assert (await writer.flush()).successful
    return tuple(record for batch in sink.writes for record in batch)


@pytest.mark.asyncio
async def test_identical_points_at_one_timestamp_have_unique_identity():
    journal, writer, sink = await setup()
    effect = CommandEffect("REPORT_CREATED", {"order_id": 1, "order_qty_lots": 1})
    await journal.admit_command(CommandEffects("new", EPOCH, (effect, effect)))
    persisted = await records(writer, sink)
    assert {r.timestamp_ns for r in persisted} == {0}
    assert [r.event_sequence for r in persisted] == list(range(1, len(persisted) + 1))
    assert_unique_questdb_identities(persisted)
    await writer.close()


@pytest.mark.asyncio
async def test_multi_fill_and_both_report_sides_share_time_without_collision():
    journal, writer, sink = await setup()
    scheduler = LatencyScheduler()
    engine = exact_engine(
        ExecutionParams(
            network_latency_ms=0, engine_latency_ms=0, reject_prob=0, rand_seed=42
        ),
        scheduler,
    )
    effects = []
    engine.on_execution_report = lambda report: effects.append(
        CommandEffect("report", report)
    )
    engine.on_trade = lambda trade: effects.append(CommandEffect("trade", trade))
    engine.on_displayed_order_event = lambda event: effects.append(
        CommandEffect("book_change", event)
    )
    for name, side, qty in (
        ("P1", Side.SELL, 1),
        ("P2", Side.SELL, 2),
        ("P3", Side.SELL, 3),
        ("TAKER", Side.BUY, 6),
    ):
        effects.clear()
        order_id = engine.submit_order(
            exact_command(
                cl_ord_id=name,
                participant_id=name,
                symbol="TEST",
                side=side,
                order_qty_lots=qty,
                price_ticks=100,
                tif=TimeInForce.GTC,
                submitted_at=EPOCH,
                session_id="SESSION",
            )
        )
        engine.process_order(ProcessOrderCommand(order_id, 0))
        await journal.admit_command(CommandEffects("process", EPOCH, tuple(effects)))
    persisted = await records(writer, sink)
    trades = [r for r in persisted if r.symbol_map["kind"] == "TRADE"]
    reports = [
        r
        for r in persisted
        if r.symbol_map["kind"] == "REPORT_CREATED"
        and "exchange_trade_id" in r.column_map
    ]
    assert len(trades) == 3 and len(reports) == 6
    assert {r.timestamp_ns for r in (*trades, *reports)} == {0}
    assert_unique_questdb_identities(persisted)
    await writer.close()


@pytest.mark.asyncio
async def test_simulated_latency_keeps_exact_ns_and_request_identity():
    journal, writer, sink = await setup()
    measurements = (
        LatencyMeasurement(
            1, "SESSION", SIM_ENGINE_LATENCY, 1.0, "ms", EPOCH, report_exec_id="E1"
        ),
        LatencyMeasurement(
            None,
            "SESSION",
            SIM_CANCEL_ENGINE_LATENCY,
            3.0,
            "ms",
            EPOCH,
            ingress_sequence=11,
            cl_ord_id="CXL-11",
            orig_cl_ord_id="ORDER-7",
        ),
    )
    await journal.admit_command(
        CommandEffects(
            "latency", EPOCH, tuple(CommandEffect("latency", m) for m in measurements)
        )
    )
    latency = [
        r for r in await records(writer, sink) if r.symbol_map["kind"] == "LATENCY"
    ]
    assert [r.column_map["duration_ns"] for r in latency] == [1_000_000, 3_000_000]
    assert latency[0].column_map["exec_id"] == "E1"
    assert latency[1].column_map["ingress_sequence"] == 11
    assert latency[1].column_map["cl_ord_id"] == "CXL-11"
    assert latency[1].column_map["orig_cl_ord_id"] == "ORDER-7"
    assert_unique_questdb_identities(latency)
    await writer.close()


@pytest.mark.asyncio
async def test_deterministic_journal_and_distinct_run_ids_do_not_collide():
    async def produce(run_id):
        journal, writer, sink = await setup(run_id)
        for value in range(3):
            await journal.admit_command(
                CommandEffects(
                    "process", EPOCH, (CommandEffect("TIMING", {"order_id": value}),)
                )
            )
        result = await records(writer, sink)
        await writer.close()
        return result

    first = await produce("first")
    repeat = await produce("first")
    other = await produce("other")
    assert first == repeat
    assert [r.event_sequence for r in first] == [r.event_sequence for r in other]
    assert [questdb_identity(r) for r in first] != [questdb_identity(r) for r in other]


@pytest.mark.asyncio
async def test_sequence_restarts_in_fresh_run_while_previous_scope_is_retained():
    journal, writer, sink = await setup("first")
    await journal.end_run(EPOCH)
    first_cut = journal.sequence
    journal.detach_run()
    await journal.start_run("second", "same scenario", (TEST_INSTRUMENT,), EPOCH)
    await journal.admit_command(CommandEffects("delivery", EPOCH, (), run_id="first"))
    persisted = await records(writer, sink)
    first = [r for r in persisted if r.run_id == "first"]
    second = [r for r in persisted if r.run_id == "second"]
    assert first[0].event_sequence == second[0].event_sequence == 1
    assert first[-1].event_sequence == first_cut + 1
    assert_unique_questdb_identities(persisted)
    await writer.close()


@pytest.mark.asyncio
async def test_many_equal_time_records_keep_actual_timestamp():
    journal, writer, sink = await setup()
    for value in range(2005):
        await journal.admit_command(
            CommandEffects(
                "process", EPOCH, (CommandEffect("TIMING", {"order_id": value}),)
            )
        )
    persisted = await records(writer, sink)
    assert {r.timestamp_ns for r in persisted} == {0}
    assert [r.event_sequence for r in persisted] == list(range(1, len(persisted) + 1))
    assert_unique_questdb_identities(persisted)
    await writer.close()


@pytest.mark.asyncio
async def test_owner_loop_producers_allocate_contiguous_sequence():
    journal, writer, sink = await setup()
    await asyncio.gather(
        *(
            journal.admit_command(
                CommandEffects(
                    "process", EPOCH, (CommandEffect("TIMING", {"order_id": value}),)
                )
            )
            for value in range(200)
        )
    )
    persisted = await records(writer, sink)
    assert [r.event_sequence for r in persisted] == list(range(1, len(persisted) + 1))
    assert_unique_questdb_identities(persisted)
    await writer.close()


@pytest.mark.asyncio
async def test_retry_retains_the_exact_group_and_identity():
    class FailOnce(InMemoryMetricsSink):
        def __init__(self):
            super().__init__()
            self.attempts = []

        async def write(self, records):
            self.attempts.append(tuple(records))
            if len(self.attempts) == 1:
                raise MetricsSinkError("outage")
            await super().write(records)

    sink = FailOnce()
    journal, writer, sink = await setup(sink=sink)
    assert not (await writer.flush()).successful
    assert (await writer.flush()).successful
    assert sink.attempts[0] == sink.attempts[1]
    assert all(a is b for a, b in zip(*sink.attempts))
    await writer.close()


def test_record_timestamp_is_not_a_uniqueness_counter():
    first = MetricRecord.create(
        "exchange_events",
        {},
        {},
        run_id="run",
        event_sequence=1,
        event_timestamp_ns=1000,
    )
    second = MetricRecord.create(
        "exchange_events",
        {},
        {},
        run_id="run",
        event_sequence=2,
        event_timestamp_ns=1001,
    )
    assert first.timestamp_ns == 1000 and second.timestamp_ns == 1001
    with pytest.raises(ValueError):
        MetricRecord.create(
            "exchange_events",
            {},
            {},
            run_id="run",
            event_sequence=3,
            event_timestamp_ns=-1,
        )

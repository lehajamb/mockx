"""Operational observers are separate from required grouped journal admission."""

from datetime import datetime, timezone
from threading import Event, Thread

import pytest

from src.mockx.effects import CommandEffect, CommandEffects
from src.mockx.journal import ExchangeJournal, JournalAdmissionError
from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricRecord,
    PersistenceAddOutcome,
    SIM_ENGINE_LATENCY,
)
from src.mockx.models import (
    ExecutionReport,
    ExecutionType,
    LatencyMeasurement,
    OrderStatus,
    OrderType,
    SelfTradePrevention,
    Side,
    TimeInForce,
    Trade,
)
from tests.exact_helpers import TEST_INSTRUMENT, exact_metrics

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def report():
    return ExecutionReport(
        order_id=1,
        exec_id="E1",
        cl_ord_id="C1",
        symbol="TEST",
        instrument=TEST_INSTRUMENT,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        order_qty_lots=1,
        order_price_ticks=100,
        tif=TimeInForce.GTC,
        ord_status=OrderStatus.NEW,
        exec_type=ExecutionType.NEW,
        leaves_qty_lots=1,
        cum_qty_lots=0,
        canceled_qty_lots=0,
        filled_notional_tick_lots=0,
        submitted_at=EPOCH,
        timestamp=EPOCH,
        order_version=1,
    )


def trade():
    return Trade(
        id=1,
        symbol="TEST",
        price_ticks=100,
        qty_lots=1,
        notional_tick_lots=100,
        buyer_order_id=1,
        seller_order_id=2,
        timestamp=EPOCH,
        level=0,
        buyer_session_alias=None,
        seller_session_alias=None,
        buyer_participant_id="buyer",
        seller_participant_id="seller",
        aggressor_order_id=1,
        passive_order_id=2,
        aggressor_side=Side.BUY,
    )


PRODUCERS = [
    pytest.param(lambda m: m.record_order(report()), id="report"),
    pytest.param(lambda m: m.record_trade(trade()), id="trade"),
    pytest.param(
        lambda m: m.record_self_trade_prevention(
            SelfTradePrevention("TEST", "owner", 1, 2, 1, EPOCH)
        ),
        id="stp",
    ),
    pytest.param(
        lambda m: m.observe_latency(
            LatencyMeasurement(1, None, SIM_ENGINE_LATENCY, 1.0, "ms", EPOCH)
        ),
        id="latency",
    ),
    pytest.param(lambda m: m.record_fix_message("D", "in", 0), id="fix"),
    pytest.param(lambda m: m.record_fix_connection(1, 0), id="connection"),
    pytest.param(lambda m: m.update_pending_events(1, 0), id="queue"),
]


@pytest.mark.parametrize("produce", PRODUCERS)
def test_operational_producers_never_admit_rows_or_fail_trading(produce):
    writer = BufferedMetricsWriter(
        InMemoryMetricsSink(), flush_batch_size=1, max_buffer_records=1
    )
    metrics = exact_metrics("TEST", persistence=writer, prometheus_enabled=False)
    metrics.start_run("run")
    writer.add_nowait(
        MetricRecord.create(
            "exchange_events",
            {},
            {"event_count": 0},
            run_id="run",
            event_sequence=1,
            event_timestamp_ns=0,
        )
    )
    produce(metrics)
    assert writer.buffered_count == 1
    assert metrics.integrity_complete


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        PersistenceAddOutcome.REJECTED_FULL,
        PersistenceAddOutcome.REJECTED_CLOSED,
        PersistenceAddOutcome.FAILED,
    ],
)
async def test_required_group_admission_exposes_fault_and_reserves_ids(
    monkeypatch, outcome
):
    writer = BufferedMetricsWriter(InMemoryMetricsSink())
    journal = ExchangeJournal(writer, "TEST")
    await journal.start_run("run", "case", (TEST_INSTRUMENT,), EPOCH)
    before = journal.sequence

    async def reject(records):
        return outcome

    monkeypatch.setattr(writer, "add_group", reject)
    with pytest.raises(JournalAdmissionError) as error:
        await journal.admit_command(
            CommandEffects("new", EPOCH, (CommandEffect("report", report()),))
        )
    assert error.value.outcome == outcome
    assert journal.sequence == before + 2
    await writer.close()


@pytest.mark.asyncio
async def test_uncertain_enqueue_never_reuses_group_identity(monkeypatch):
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink)
    journal = ExchangeJournal(writer, "TEST")
    await journal.start_run("run", "case", (TEST_INSTRUMENT,), EPOCH)
    original = writer.add_group

    async def append_then_raise(records):
        await original(records)
        raise RuntimeError("uncertain enqueue")

    monkeypatch.setattr(writer, "add_group", append_then_raise)
    with pytest.raises(JournalAdmissionError):
        await journal.admit_command(
            CommandEffects("first", EPOCH, (CommandEffect("report", report()),))
        )
    monkeypatch.setattr(writer, "add_group", original)
    await journal.admit_command(CommandEffects("later", EPOCH, ()))
    await writer.flush()
    records = [r for batch in sink.writes for r in batch]
    assert [r.event_sequence for r in records] == list(range(1, len(records) + 1))
    assert len({r.event_sequence for r in records}) == len(records)
    await writer.close()


def test_lossy_overflow_policy_is_not_a_supported_contract():
    with pytest.raises(TypeError):
        BufferedMetricsWriter(InMemoryMetricsSink(), overflow_policy="drop_oldest")


def test_first_failure_is_sticky_and_notification_runs_outside_lock():
    metrics = exact_metrics("TEST", prometheus_enabled=False)
    metrics.start_run("run")
    notifications, unlocked = [], []

    def notify():
        done = Event()

        def inspect():
            notifications.append(metrics.persistence_integrity_failure)
            done.set()

        Thread(target=inspect, daemon=True).start()
        unlocked.append(done.wait(timeout=1))

    metrics.on_integrity_failure = notify
    assert metrics.mark_integrity_failure("first", event_sequence=1)
    first = metrics.persistence_integrity_failure
    assert not metrics.mark_integrity_failure("later", event_sequence=2)
    assert notifications == [first]
    assert unlocked == [True]
    metrics.end_run()
    assert metrics.persistence_integrity_failure is first
    metrics.start_run("next")
    assert metrics.integrity_complete


def test_notification_exception_cannot_escape_owner_failure_boundary():
    metrics = exact_metrics("TEST", prometheus_enabled=False)
    metrics.start_run("run")

    def fail():
        raise RuntimeError("observer failure")

    metrics.on_integrity_failure = fail
    assert metrics.mark_integrity_failure("incomplete")
    assert not metrics.integrity_complete

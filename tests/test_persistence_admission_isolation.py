"""Persistence admission cannot interrupt an exchange observer."""

from datetime import datetime, timezone
from threading import Event, Thread

import pytest

from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricsSinkError,
    PersistenceAddOutcome,
    PersistenceFlushOutcome,
    SIM_ENGINE_LATENCY,
)
from src.mockx.models import (
    ExecutionReport,
    ExecutionType,
    LatencyMeasurement,
    MarketDataSnapshot,
    OrderStatus,
    OrderType,
    SelfTradePrevention,
    Side,
    TimeInForce,
    Trade,
)
from tests.exact_helpers import TEST_INSTRUMENT, exact_metrics


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def collector(*, capacity=10, overflow_policy="reject_new", sink=None):
    writer = BufferedMetricsWriter(
        sink or InMemoryMetricsSink(),
        flush_batch_size=1,
        max_buffer_records=capacity,
        overflow_policy=overflow_policy,
    )
    metrics = exact_metrics("TEST", persistence=writer, prometheus_enabled=False)
    metrics.start_run("isolation-run")
    return metrics, writer


def report():
    return ExecutionReport(
        order_id=1, exec_id="E1", cl_ord_id="C1", symbol="TEST",
        instrument=TEST_INSTRUMENT, side=Side.BUY, order_type=OrderType.LIMIT,
        order_qty_lots=1, order_price_ticks=100, tif=TimeInForce.GTC,
        ord_status=OrderStatus.NEW, exec_type=ExecutionType.NEW,
        leaves_qty_lots=1, cum_qty_lots=0, canceled_qty_lots=0,
        filled_notional_tick_lots=0, submitted_at=EPOCH, timestamp=EPOCH,
        order_version=1,
    )


def trade():
    return Trade(
        id=1, symbol="TEST", price_ticks=100, qty_lots=1,
        notional_tick_lots=100, buyer_order_id=1, seller_order_id=2,
        timestamp=EPOCH, level=0, buyer_session_alias=None,
        seller_session_alias=None, buyer_participant_id="buyer",
        seller_participant_id="seller", aggressor_order_id=1,
        passive_order_id=2, aggressor_side=Side.BUY,
    )


PRODUCERS = [
    pytest.param(lambda m: m.record_order(report()), id="execution-report"),
    pytest.param(lambda m: m.record_trade(trade()), id="trade"),
    pytest.param(lambda m: m.record_self_trade_prevention(
        SelfTradePrevention("TEST", "owner", 1, 2, 1, EPOCH)
    ), id="self-trade-prevention"),
    pytest.param(lambda m: m.observe_latency(
        LatencyMeasurement(1, None, SIM_ENGINE_LATENCY, 1.0, "ms", EPOCH)
    ), id="latency"),
    pytest.param(lambda m: m.record_fix_message("D", "in", 0), id="fix-ingress"),
    pytest.param(lambda m: m.record_fix_connection(1, 0), id="fix-connection"),
    pytest.param(lambda m: m.update_book_metrics(
        MarketDataSnapshot("TEST", TEST_INSTRUMENT, 1, EPOCH, (), ())
    ), id="book-snapshot"),
    pytest.param(lambda m: m.update_pending_events(1, 0), id="pending-events"),
]


@pytest.mark.parametrize("produce", PRODUCERS)
def test_full_buffer_does_not_interrupt_any_metric_producer(produce):
    metrics, writer = collector(capacity=1)
    metrics.record_fix_message("A", "in", 0)

    produce(metrics)

    assert writer.buffered_count == 1
    assert metrics.integrity_complete is False
    failure = metrics.persistence_integrity_failure
    assert failure.run_id == "isolation-run"
    assert failure.metric_event_sequence == 2
    assert failure.event_timestamp_ns == 0
    assert failure.reason == "REJECTED_FULL"
    assert metrics.run_persistence_snapshot()["integrity_complete"] is False


@pytest.mark.parametrize("outcome", [
    PersistenceAddOutcome.REJECTED_FULL,
    PersistenceAddOutcome.REJECTED_CLOSED,
    PersistenceAddOutcome.FAILED,
])
def test_direct_admission_stays_strict_and_faults_the_run(monkeypatch, outcome):
    metrics, writer = collector()
    monkeypatch.setattr(writer, "add_nowait", lambda record: outcome)

    with pytest.raises(MetricsSinkError, match=outcome.value):
        metrics._create_record("test", {}, {"value": 1}, 10)

    assert metrics.integrity_complete is False
    assert metrics.persistence_integrity_failure.reason == outcome.value


def test_drop_oldest_acceptance_marks_permanent_loss():
    metrics, writer = collector(capacity=1, overflow_policy="drop_oldest")
    metrics.record_fix_message("A", "in", 0)
    metrics.record_fix_message("D", "in", 0)

    assert writer.buffered_count == 1
    assert writer.health_snapshot().dropped_records == 1
    assert metrics.integrity_complete is False
    assert metrics.persistence_integrity_failure.reason == "ACCEPTED_DROPPED_OLDEST"


@pytest.mark.asyncio
async def test_closed_writer_isolated_and_identity_remains_reserved():
    metrics, writer = collector()
    assert (await writer.close()).successful
    metrics.record_fix_message("D", "in", 100)
    metrics.record_fix_message("D", "in", 101)

    assert writer.health_snapshot().rejected_closed_records == 2
    assert metrics._metric_event_sequence == 2
    assert metrics._last_event_timestamp_ns == 101
    assert metrics.persistence_integrity_failure.metric_event_sequence == 1


@pytest.mark.asyncio
async def test_enqueue_then_exception_never_reuses_record_identity(monkeypatch):
    metrics, writer = collector()
    original = writer.add_nowait

    def append_then_raise(record):
        original(record)
        raise RuntimeError("observer failed after admission")

    monkeypatch.setattr(writer, "add_nowait", append_then_raise)
    metrics.record_fix_message("D", "in", 10)
    monkeypatch.setattr(writer, "add_nowait", original)
    metrics.record_fix_message("D", "in", 11)
    assert (await writer.flush()).successful

    records = [record for batch in writer.sink.writes for record in batch]
    assert [record.metric_event_sequence for record in records] == [1, 2]
    assert [record.timestamp_ns for record in records] == [10, 11]
    assert metrics.integrity_complete is False
    assert metrics.persistence_integrity_failure.metric_event_sequence == 1


@pytest.mark.asyncio
async def test_first_failure_is_sticky_callback_runs_once_outside_lock():
    metrics, writer = collector(capacity=1)
    notifications = []
    lock_released = []

    def notify():
        read_completed = Event()

        def read_fault():
            notifications.append(metrics.persistence_integrity_failure)
            read_completed.set()

        # Bound the wait so a regression fails instead of deadlocking the test.
        worker = Thread(target=read_fault, daemon=True)
        worker.start()
        lock_released.append(read_completed.wait(timeout=1))

    metrics.on_integrity_failure = notify
    metrics.record_fix_message("D", "in", 0)
    metrics.record_fix_message("D", "in", 1)
    first_failure = metrics.persistence_integrity_failure
    metrics.record_fix_message("D", "in", 2)
    assert (await writer.flush()).successful
    assert metrics.integrity_complete is False
    metrics.reset_run_metrics()
    metrics.end_run()

    assert notifications == [first_failure]
    assert lock_released == [True]
    assert metrics.persistence_integrity_failure is first_failure
    assert metrics.integrity_complete is False
    metrics.start_run("next-run")
    assert metrics.integrity_complete is True
    assert metrics.persistence_integrity_failure is None


def test_callback_exception_cannot_escape_the_producer():
    metrics, writer = collector(capacity=1)

    def fail_notification():
        raise RuntimeError("notification failure")

    metrics.on_integrity_failure = fail_notification
    metrics.record_fix_message("D", "in", 0)
    metrics.record_fix_message("D", "in", 1)
    assert metrics.integrity_complete is False


def test_record_validation_is_strict_but_ordinary_producer_is_protected():
    metrics, writer = collector()
    with pytest.raises(ValueError, match="non-negative integer"):
        metrics._create_record("test", {}, {}, -1)
    assert metrics.integrity_complete is True

    metrics.record_fix_message("D", "in", -1)
    assert metrics.integrity_complete is False
    assert metrics.persistence_integrity_failure.metric_event_sequence is None
    assert writer.buffered_count == 0


@pytest.mark.asyncio
async def test_retained_sink_failure_does_not_make_metrics_incomplete():
    class FailOnceSink(InMemoryMetricsSink):
        async def write(self, records):
            if not getattr(self, "failed", False):
                self.failed = True
                raise MetricsSinkError("temporary outage")
            await super().write(records)

    metrics, writer = collector(sink=FailOnceSink())
    metrics.record_fix_message("D", "in", 0)
    assert (await writer.flush()).outcome == PersistenceFlushOutcome.FAILED
    assert writer.buffered_count == 1
    assert metrics.integrity_complete is True
    assert (await writer.flush()).successful
    assert metrics.integrity_complete is True
    assert len(writer.sink.writes[0]) == 1


def test_disabled_persistence_is_complete_without_a_run():
    metrics = exact_metrics("TEST", prometheus_enabled=False)
    metrics.record_fix_message("D", "in", 0)
    assert metrics.integrity_complete is True
    assert metrics.persistence_integrity_failure is None

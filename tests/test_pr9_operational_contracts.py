"""PR9 snapshot, stats, readiness, retention, timing, and FIX contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.mockx.configuration import ConfigurationError, validate_config
from src.mockx.delivery import OutboundFixDeliveryQueue
from src.mockx.gateway import FIXApplication, fix
from src.mockx.latency import SchedulerFailedError
from src.mockx.main import MockExchange
from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricRecord,
    MetricsSinkError,
    PersistenceAddOutcome,
    PersistenceFlushOutcome,
    PersistenceHealthState,
)
from src.mockx.models import MarketDataSnapshot, PriceLevelSnapshot, Side
from src.mockx.effects import BookCheckpoint, BookOrderState
from src.mockx.journal import ExchangeJournal
from src.mockx.journal_reader import NativeJournalReader
from tests.exact_helpers import TEST_INSTRUMENT
from tests.api_helpers import make_native_exchange
from tests.api_helpers import api_config, make_exchange, process_order
from tests.exact_helpers import exact_metrics
from tests.test_fix_delivery_queue import NOW, SESSION, report


NOW_UTC = datetime(2026, 7, 29, tzinfo=timezone.utc)


def enabled_questdb() -> dict[str, object]:
    return {
        "url": "http://localhost:9000",
        "health_url": "http://localhost:9003",
        "username": "user",
        "password": "password",
        "flush_batch_size": 10,
        "flush_batch_bytes": 500_000,
        "max_buffer_records": 20,
        "max_buffer_bytes": 1_000_000,
        "flush_interval_ms": 10_000,
        "retry_backoff_ms": 10,
        "max_retry_delay_ms": 100,
        "shutdown_timeout_ms": 1_000,
        "request_timeout_ms": 1_000,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (("max_buffer_records", 9), ("max_retry_delay_ms", 9)),
)
def test_persistence_capacity_and_retry_relationships_validate(
    field: str,
    value: int,
):
    raw = api_config()
    raw["questdb"] = {**enabled_questdb(), field: value}
    with pytest.raises(ConfigurationError):
        validate_config(raw, environ={})


def test_obsolete_persistence_yaml_keys_are_rejected():
    raw = api_config()
    raw["questdb"] = {
        **enabled_questdb(),
        "batch_size": 10,
        "buffer_capacity": 20,
    }
    with pytest.raises(ConfigurationError):
        validate_config(raw, environ={})


def snapshot(
    metrics,
    sequence: int,
    *,
    bids: tuple[PriceLevelSnapshot, ...] = (),
    asks: tuple[PriceLevelSnapshot, ...] = (),
) -> MarketDataSnapshot:
    return MarketDataSnapshot(
        symbol="TEST",
        instrument=metrics.instruments.require("TEST"),
        sequence=sequence,
        timestamp=NOW_UTC,
        bids=bids,
        asks=asks,
        run_generation=1,
    )


def level(price: int, quantity: int) -> PriceLevelSnapshot:
    return PriceLevelSnapshot(
        price_ticks=price,
        level_qty_lots=quantity,
        strategy_qty_lots=0,
        market_qty_lots=quantity,
        order_count=1,
    )


@pytest.mark.asyncio
async def test_empty_one_sided_and_depth_shrink_checkpoints_reconstruct():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=100)
    journal = ExchangeJournal(writer, "TEST")
    await journal.start_run("run", "shapes", (TEST_INSTRUMENT,), NOW_UTC)
    shapes = (
        ((BookOrderState(1, "TEST", Side.BUY, 100, 2),), (((100, 2),), ())),
        ((BookOrderState(2, "TEST", Side.SELL, 101, 3),), ((), ((101, 3),))),
        ((), ((), ())),
        (
            (
                BookOrderState(3, "TEST", Side.BUY, 100, 2),
                BookOrderState(4, "TEST", Side.BUY, 99, 4),
            ),
            (((100, 2), (99, 4)), ()),
        ),
        ((BookOrderState(3, "TEST", Side.BUY, 100, 1),), (((100, 1),), ())),
    )
    for orders, expected in shapes:
        await journal.checkpoint(BookCheckpoint("run", NOW_UTC, 1, orders))
        await writer.flush()
        rows = [
            {**r.symbol_map, **r.column_map} for batch in sink.writes for r in batch
        ]
        latest = max(row.get("checkpoint_id", 0) for row in rows)
        spin = [row for row in rows if row.get("checkpoint_id") == latest]
        reader = NativeJournalReader()
        reader.bootstrap_checkpoint(spin)
        assert reader.book.depth("TEST") == expected
    await writer.close()


@pytest.mark.asyncio
async def test_run_start_persists_instrument_metadata_and_empty_checkpoint():
    sink = InMemoryMetricsSink()
    exchange = make_native_exchange(
        validate_config(api_config(), environ={}), persistence_sink=sink
    )
    await exchange.start_components()
    try:
        started = await exchange.lifecycle.start_session("initial-empty")
        await exchange.metrics_persistence.flush()
        records = [
            {**r.symbol_map, **r.column_map} for batch in sink.writes for r in batch
        ]
        assert sum(row["kind"] == "INSTRUMENT" for row in records) == len(
            exchange.engine.books
        )
        checkpoints = [row for row in records if row["kind"] == "CHECKPOINT_COMPLETE"]
        assert len(checkpoints) == 1
        assert checkpoints[0]["order_count"] == 0
        assert checkpoints[0]["run_id"] == started.run_id
        assert "book_snapshot" not in {r.table for batch in sink.writes for r in batch}
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_run_final_stats_are_isolated_and_lifetime_remains_cumulative():
    exchange = make_exchange()
    await exchange.start_components()
    try:
        await exchange.lifecycle.start_session("run-one")
        process_order(exchange, "ONE", side=Side.BUY, qty=1, price=100)
        first = await exchange.lifecycle.end_session()
        assert first.final_stats.scope == "current_run"
        assert first.final_stats.run_active is False
        assert first.final_stats.orders.submitted == 1
        assert not hasattr(first.final_stats, "process_lifetime")
        assert not hasattr(first.final_stats, "fix_delivery")

        after_first = exchange.api._stats_response()
        assert after_first.run_active is False
        assert after_first.orders.submitted == 0
        assert after_first.process_lifetime["orders"]["submitted"] == 1

        await exchange.lifecycle.start_session("run-two")
        second = await exchange.lifecycle.end_session()
        assert second.final_stats.orders.submitted == 0
        after_second = exchange.api._stats_response()
        assert after_second.process_lifetime["orders"]["submitted"] == 1
        assert b"mockx_orders_submitted_total 1.0" in (
            exchange.metrics.prometheus_payload()
        )
    finally:
        await exchange.stop_components()


def test_stats_and_prometheus_label_all_operational_sections_and_units():
    exchange = make_exchange()
    exchange.scheduler.schedule_event("pending", object(), 10)
    stats = exchange.api._stats_response().model_dump(mode="json")

    assert stats["scope"] == "current_run"
    assert stats["persistence"]["scope"] == "operational"
    assert stats["scheduler"]["scope"] == "current_run"
    assert stats["scheduler"]["pending_events"] == 1
    assert stats["fix_delivery"]["scope"] == "process_lifetime"
    assert "websocket" not in stats
    assert stats["process_lifetime"]["scope"] == "process_lifetime"

    body = exchange.metrics.prometheus_payload().decode()
    for name in (
        "mockx_scheduler_pending_events",
        "mockx_scheduler_failed",
        "mockx_scheduler_dead_letters_total",
        "mockx_scheduler_event_processing_limit_failures_total",
        "mockx_fix_delivery_queue_depth",
        "mockx_fix_delivery_retries_total",
        "mockx_fix_delivery_permanent_failures_total",
        "mockx_match_loop_duration_us",
        "mockx_book_commit_duration_us",
        "mockx_engine_command_duration_us",
        "mockx_report_creation_duration_us",
        "mockx_serialization_duration_us",
        "mockx_sim_cancel_inbound_latency_ms",
        "mockx_sim_cancel_engine_latency_ms",
        "mockx_sim_cancel_outbound_latency_ms",
        "mockx_sim_cancel_end_to_end_latency_ms",
    ):
        assert name in body
    assert "mockx_sim_cancel_inbound_latency_seconds" not in body


def test_all_five_cpu_duration_boundaries_are_measured_separately():
    exchange = make_exchange()
    process_order(exchange, "TIMED", side=Side.BUY, qty=1, price=100)
    delivery = exchange.fix_app.delivery_queue.snapshot_records()[0]
    exchange.fix_app._send_quickfix_execution_report(delivery.payload)

    latency = exchange.metrics.snapshot()
    assert latency.average_match_loop_duration_us is not None
    assert latency.average_book_commit_duration_us is not None
    assert latency.average_engine_command_duration_us is not None
    assert latency.average_report_creation_duration_us is not None
    assert latency.average_serialization_duration_us is not None


@pytest.mark.asyncio
async def test_scheduler_failure_observers_update_run_lifetime_and_prometheus():
    exchange = make_exchange()
    exchange.scheduler.max_events_per_advance = 1

    def reschedule(_value) -> None:
        exchange.scheduler.schedule_event("loop", None, 0)

    exchange.scheduler.register_handler("loop", reschedule)
    exchange.scheduler.schedule_event("loop", None, 0)
    with pytest.raises(SchedulerFailedError):
        await exchange.scheduler.run_until_idle()

    stats = exchange.api._stats_response().model_dump(mode="json")
    assert stats["scheduler"]["state"] == "FAILED"
    assert stats["scheduler"]["dead_letter_count"] == 1
    assert stats["scheduler"]["event_processing_limit_failures"] == 1
    assert stats["process_lifetime"]["scheduler"] == {
        "dead_letters": 1,
        "event_processing_limit_failures": 1,
    }
    body = exchange.metrics.prometheus_payload().decode()
    assert "mockx_scheduler_failed 1.0" in body
    assert "mockx_scheduler_dead_letters_total 1.0" in body
    assert "mockx_scheduler_event_processing_limit_failures_total 1.0" in body


class ToggleSink(InMemoryMetricsSink):
    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    async def write(self, records) -> None:
        if self.fail:
            raise MetricsSinkError("planned outage")
        await super().write(records)


@pytest.mark.asyncio
async def test_required_persistence_readiness_recovers_but_history_remains(
    monkeypatch,
):
    raw = api_config()
    raw["questdb"] = enabled_questdb()
    sink = ToggleSink()
    monkeypatch.setattr(
        "src.mockx.main.QuestDBMetricsSink",
        lambda **kwargs: sink,
    )
    exchange = make_native_exchange(
        validate_config(raw, environ={}), persistence_sink=sink
    )
    assert exchange.metrics_persistence is not None
    await exchange.start_components()
    try:
        sink.fail = True
        logical = MetricRecord.create(
            "health",
            {},
            {"value": 1},
            run_id="health",
            event_sequence=1,
            event_timestamp_ns=1_000,
        )
        assert exchange.metrics_persistence.add_nowait(logical) == (
            PersistenceAddOutcome.ACCEPTED
        )
        assert (await exchange.metrics_persistence.flush()).outcome == (
            PersistenceFlushOutcome.FAILED
        )
        status, degraded = exchange.api._readiness()
        assert status == 503
        assert degraded.core_ready is True
        assert degraded.traffic_ready is False
        assert degraded.persistence_required is True
        assert degraded.components["persistence"] == "retrying"

        sink.fail = False
        assert (await exchange.metrics_persistence.flush()).successful
        status, recovered = exchange.api._readiness()
        assert status == 200
        assert recovered.traffic_ready is True
        health = exchange.metrics.persistence_health_snapshot()
        assert health.state == PersistenceHealthState.HEALTHY
        assert health.consecutive_failures == 0
        assert health.lifetime_failures == 1
    finally:
        await exchange.stop_components()


def _complete_standard_header(message) -> None:
    header = message.getHeader()
    header.setField(fix.BeginString("FIX.4.4"))
    header.setField(fix.SenderCompID("MOCKX"))
    header.setField(fix.TargetCompID("CLIENT"))
    header.setField(fix.MsgSeqNum(1))
    header.setField(fix.SendingTime())


def test_session_and_business_rejects_validate_against_fix44():
    session_reject = FIXApplication._build_session_reject(
        text="Required tag missing",
        ref_seq_num=7,
        ref_msg_type=fix.MsgType_NewOrderSingle,
        ref_tag_id=11,
        session_reject_reason=1,
    )
    business_reject = FIXApplication._build_business_reject(
        text="Unsupported application message",
        ref_seq_num=8,
        ref_msg_type="Z",
        business_reject_reason=3,
        business_reject_ref_id="request-8",
    )
    dictionary = fix.DataDictionary("configs/FIX44.xml")
    for message in (session_reject, business_reject):
        _complete_standard_header(message)
        dictionary.validate(message)

    assert session_reject.getField(45) == "7"
    assert session_reject.getField(371) == "11"
    assert session_reject.getField(372) == fix.MsgType_NewOrderSingle
    assert session_reject.getField(373) == "1"
    assert business_reject.getField(45) == "8"
    assert business_reject.getField(372) == "Z"
    assert business_reject.getField(379) == "request-8"
    assert business_reject.getField(380) == "3"


def test_delivered_fix_payload_retention_is_bounded_without_pruning_pending():
    queue = OutboundFixDeliveryQueue(terminal_retention_records=2)
    first_payload = None
    for index in range(1, 6):
        payload = report(f"G00000001-E{index:08d}", index)
        first_payload = first_payload or payload
        delivery, fresh = queue.enqueue(
            delivery_id=f"G00000001-D{index:08d}",
            destination=SESSION,
            payload=payload,
            simulated_due_timestamp=NOW,
        )
        assert fresh
        due_command = queue.current_due_command(delivery.delivery_id)
        assert due_command is not None
        assert (
            queue.mark_due(
                due_command,
                simulated_timestamp=NOW,
            )
            is not None
        )
        queue.start_attempt(delivery.delivery_id, simulated_timestamp=NOW)
        queue.record_delivered(delivery.delivery_id)

    pending, fresh = queue.enqueue(
        delivery_id="G00000001-D99999999",
        destination=SESSION,
        payload=report("G00000001-E99999999", 999),
        simulated_due_timestamp=NOW,
    )
    assert fresh
    current = queue.snapshot()
    assert current.delivered == 5
    assert current.retained_delivered_records == 2
    assert current.pending == 1
    assert pending in queue.snapshot_records()
    assert len(queue.snapshot_records()) == 3

    duplicate, fresh = queue.enqueue(
        delivery_id="IGNORED",
        destination=SESSION,
        payload=first_payload,
        simulated_due_timestamp=NOW,
    )
    assert duplicate is None and fresh is False

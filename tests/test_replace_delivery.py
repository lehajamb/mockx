"""Replace reports retain immutable identity through failure and reset."""

import asyncio
from datetime import timedelta

import pytest

from src.mockx.models import OrderCancelReject, ExecutionReport, ExecutionType, FixDeliveryState, SessionIdentity
from src.mockx.metrics import BufferedMetricsWriter, InMemoryMetricsSink
from .test_fix_ingress_pipeline import _exchange, _new_order, _start, _stop
from .test_replace_fix import _replace

FAILURE = "METRICS_INCOMPLETE: Metrics were lost; this simulation must be rerun."


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("resets", [1, 2])
async def test_replace_report_retries_once_and_preserves_trace_across_reset(known, resets):
    exchange = _exchange(outbound_ms=5)
    sink = InMemoryMetricsSink()
    exchange.metrics.attach_persistence(BufferedMetricsWriter(sink))
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    session_id = session.toString()
    try:
        if known:
            exchange.fix_app.fromApp(_new_order("N-RETRY", 1), session)
            await exchange.fix_app.wait_for_ingress()
            await exchange.lifecycle.advance_to(
                exchange.scheduler.current_time + timedelta(milliseconds=5)
            )
        delivered.clear()
        exchange.fix_app.fromApp(_replace("R-RETRY", "N-RETRY", 2, qty=3), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        record = exchange.fix_app.delivery_queue.snapshot_records()[-1]
        key = (record.run_generation, record.payload.replace_ingress_sequence
               if isinstance(record.payload, ExecutionReport) else record.payload.ingress_sequence)
        assert exchange.fix_app.delivery_queue.pending_replace_keys() == {key}
        assert exchange.fix_app.delivery_queue.pending_cancel_keys() == set()
        exchange.fix_app.unregister_session(session_id)
        await exchange.lifecycle.advance_to(
            exchange.scheduler.current_time + timedelta(milliseconds=5)
        )
        assert exchange.fix_app.delivery_queue.get(record.delivery_id).state == FixDeliveryState.RETRY_PENDING
        assert key in exchange.latency_tracker.replaces
        for reset_index in range(resets):
            await exchange.lifecycle.end_session()
            await exchange.lifecycle.start_session(f"replace-retry-reset-{reset_index}")
            if reset_index < resets - 1:
                await exchange.lifecycle.advance_to(
                    exchange.scheduler.current_time + timedelta(milliseconds=3)
                )
        assert key in exchange.latency_tracker.replaces
        exchange.fix_app.register_session(SessionIdentity(session_id, alias="CLIENT"), object())
        await asyncio.sleep(0)
        assert delivered == [record.payload]
        assert exchange.fix_app.delivery_queue.get(record.delivery_id).state == FixDeliveryState.DELIVERED
        assert key not in exchange.latency_tracker.replaces
        expected_latency_ms = 5 + 3 * (resets - 1)
        snapshot = exchange.metrics.snapshot()
        assert snapshot.average_sim_replace_outbound_latency_ms == expected_latency_ms
        assert snapshot.average_sim_replace_end_to_end_latency_ms == expected_latency_ms
        assert snapshot.invalid_latency_measurements == 0
        samples = [
            sample for sample in exchange.latency_tracker.measurements
            if sample.metric.startswith("mockx_sim_replace_")
        ]
        assert len(samples) == 2
        assert all(sample.timestamp == exchange.scheduler.current_time for sample in samples)
        assert delivered[0] is record.payload
        exchange.fix_app.register_session(SessionIdentity(session_id, alias="CLIENT"), object())
        await asyncio.sleep(0)
        assert delivered == [record.payload]

        # Further current-run records must remain admissible after publishing
        # elapsed latency across earlier run clocks.
        exchange.fix_app.fromApp(_new_order("N-AFTER-RESET", 3), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(
            exchange.scheduler.current_time + timedelta(milliseconds=1)
        )
        assert exchange.lifecycle.state.value == "ACTIVE"
        assert exchange.metrics.persistence_integrity_failure is None
        assert (await exchange.metrics.flush()).successful
        current_rows = [
            row for batch in sink.writes for row in batch
            if row.run_id == exchange.lifecycle.run_id
        ]
        timestamps = [row.timestamp_ns for row in current_rows]
        assert timestamps == sorted(timestamps)
        persisted_latency = [
            row for row in current_rows
            if row.table == "latency"
            and row.symbol_map["type"].startswith("mockx_sim_replace_")
        ]
        assert len(persisted_latency) == 2
        assert all(row.column_map["duration_ms"] == expected_latency_ms for row in persisted_latency)
        assert all(row.timestamp_ns == 0 for row in persisted_latency)
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["enqueued", "received", "arrived"])
async def test_failed_run_settles_one_replace_at_each_stage(stage):
    exchange = _exchange(inbound_ms=5, engine_ms=3, outbound_ms=7)
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    try:
        exchange.fix_app.fromApp(_new_order("N-FAIL", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(
            exchange.scheduler.current_time + timedelta(milliseconds=15)
        )
        delivered.clear()
        exchange.fix_app.fromApp(_replace("R-FAIL", "N-FAIL", 2, qty=3), session)
        if stage == "enqueued":
            await exchange.fix_app.await_ingress_enqueued()
        else:
            await exchange.fix_app.wait_for_ingress()
            await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
            if stage == "arrived":
                await exchange.lifecycle.advance_to(
                    exchange.scheduler.current_time + timedelta(milliseconds=5)
                )
        exchange.fix_app.fail_pending_commands(FAILURE)
        exchange.fix_app.fail_pending_commands(FAILURE)
        await exchange.lifecycle.advance_to(
            exchange.scheduler.current_time + timedelta(milliseconds=7)
        )
        assert len(delivered) == 1
        assert isinstance(delivered[0], OrderCancelReject)
        assert delivered[0].reason.value == "EXCHANGE_UNAVAILABLE"
        order = next(iter(exchange.engine.orders.values()))
        assert order.cl_ord_id == "N-FAIL" and order.leaves_qty_lots == 5
        assert exchange.fix_app._ingress_receipts == {}
        assert not exchange.scheduler.event_queue
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_persistence_loss_during_detached_receive_batch_settles_replace():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        exchange.metrics.attach_persistence(BufferedMetricsWriter(
            InMemoryMetricsSink(), flush_batch_size=1,
            max_buffer_records=1, max_buffer_bytes=100_000,
        ))
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("N-LOSS", 1), session)
        exchange.fix_app.fromApp(_new_order("N-LOSS2", 2), session)
        exchange.fix_app.fromApp(_replace("R-LOSS", "N-LOSS", 3), session)
        await exchange.fix_app.wait_for_ingress()
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert len(delivered) == 3
        assert isinstance(delivered[-1], OrderCancelReject)
        assert delivered[-1].cl_ord_id == "R-LOSS"
        assert delivered[-1].reason.value == "EXCHANGE_UNAVAILABLE"
        assert not exchange.engine.orders and not exchange.scheduler.event_queue
    finally:
        await _stop(exchange)

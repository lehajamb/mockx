"""Validated fills remain truthful when a required publication path fails."""

from datetime import timedelta
from uuid import UUID

import pytest

from src.mockx.effects import CommandEffects
from src.mockx.journal import JournalAdmissionError
from src.mockx.journal_reader import NativeJournalReader
from src.mockx.lifecycle import LifecycleState
from src.mockx.metrics import PersistenceAddOutcome, PersistenceBackpressureTimeout
from src.mockx.models import ExecutionType, OrderStatus
from .test_fix_ingress_pipeline import _exchange, _new_order, _start


def rows(exchange):
    return [
        dict(record.symbol_map, **record.column_map)
        for record in exchange.questdb_writer.records
    ]


@pytest.mark.asyncio
async def test_run_identity_is_uuid_and_empty_start_checkpoint_is_complete():
    exchange = _exchange()
    await _start(exchange)
    try:
        UUID(exchange.lifecycle.run_id)
        assert exchange.lifecycle.scenario_label == "pr5"
        reader = NativeJournalReader()
        reader.apply_rows(rows(exchange))
        assert reader.run_id == exchange.lifecycle.run_id
        assert reader.label == "pr5"
        assert not reader.book.orders
        assert reader.next_itch_sequence == exchange.itch_feed.sequence_number
    finally:
        await exchange.lifecycle.end_session()
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_completed_commands_reconstruct_fifo_category_and_final_checkpoint():
    exchange = _exchange()
    sessions, delivered = await _start(exchange, "CLIENT", "CLIENT2")
    try:
        for msg, session in (
            (_new_order("a", 1, qty=7, price=100), sessions["CLIENT"]),
            (_new_order("b", 2, qty=5, price=100), sessions["CLIENT"]),
            (_new_order("t", 1, side="2", qty=8, price=100), sessions["CLIENT2"]),
        ):
            exchange.fix_app.fromApp(msg, session)
            await exchange.fix_app.wait_for_ingress()
            await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
            await exchange.metrics.flush()
            reader = NativeJournalReader()
            reader.apply_rows(rows(exchange))
            expected = [
                o for level in exchange.engine.books["TEST"].bids for o in level.orders
            ]
            actual = list(reader.book.orders.values())
            assert [
                (o.logical_order_id, o.qty_lots, o.liquidity_category) for o in actual
            ] == [
                (o.id, o.leaves_qty_lots, o.liquidity_category.value) for o in expected
            ]
        await exchange.lifecycle.capture_checkpoint()
        result = await exchange.lifecycle.end_session()
        reader = NativeJournalReader()
        reader.apply_rows(rows(exchange))
        assert reader.run_ended and not reader.run_failed
        assert result.final_stats.trades.count == 2
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_failed_group_admission_retains_every_truthful_fill_report(monkeypatch):
    exchange = _exchange()
    sessions, delivered = await _start(exchange, "CLIENT", "CLIENT2")
    exchange.fix_app.fromApp(_new_order("p1", 1, side="2", qty=2), sessions["CLIENT"])
    exchange.fix_app.fromApp(_new_order("p2", 2, side="2", qty=3), sessions["CLIENT"])
    await exchange.fix_app.wait_for_ingress()
    await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
    original = exchange.journal.admit_command

    async def fail_after_matching(effects):
        if effects.command_type == "order_process" and any(
            e.kind == "trade" for e in effects.effects
        ):
            raise JournalAdmissionError(
                PersistenceAddOutcome.TOO_LARGE,
                run_id=exchange.lifecycle.run_id,
                event_sequence=exchange.journal.sequence + 1,
            )
        return await original(effects)

    monkeypatch.setattr(exchange.journal, "admit_command", fail_after_matching)
    try:
        exchange.fix_app.fromApp(_new_order("take", 1, qty=5), sessions["CLIENT2"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        take = next(o for o in exchange.engine.orders.values() if o.cl_ord_id == "take")
        assert take.status == OrderStatus.FILLED and take.filled_qty_lots == 5
        reports = [r for r in delivered if getattr(r, "cl_ord_id", None) == "take"]
        assert [
            (r.last_qty_lots, r.cum_qty_lots)
            for r in reports
            if r.exec_type == ExecutionType.TRADE
        ] == [(2, 2), (3, 5)]
        assert not any(r.exec_type == ExecutionType.REJECTED for r in reports)
        assert exchange.lifecycle.state == LifecycleState.RUN_FAILED
        assert exchange.lifecycle.run_failure.code == "JOURNAL_ADMISSION_FAILED"
        assert not exchange.glimpse_service.active_generation
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_pressure_timeout_keeps_already_admitted_records_and_reports(monkeypatch):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)

    async def pressure():
        raise PersistenceBackpressureTimeout("admitted backlog did not drain")

    try:
        exchange.fix_app.fromApp(_new_order("new", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        monkeypatch.setattr(
            exchange.metrics_persistence, "wait_for_pressure_relief", pressure
        )
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert exchange.lifecycle.run_failure.code == "PERSISTENCE_BACKPRESSURE_TIMEOUT"
        await exchange.metrics.flush()
        assert any(
            r["kind"] == "COMMAND_COMPLETE"
            and r["command_type"] == "new_order_received"
            for r in rows(exchange)
        )
        assert exchange.metrics.integrity_complete
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_engine_failure_emits_no_completed_marker_for_failed_command(monkeypatch):
    from src.mockx.latency import SchedulerFailedError

    exchange = _exchange()
    sessions, delivered = await _start(exchange)

    def broken(command):
        raise RuntimeError("invariant audit failed")

    monkeypatch.setattr(exchange.engine, "process_order", broken)
    try:
        exchange.fix_app.fromApp(_new_order("bad", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        with pytest.raises(SchedulerFailedError):
            await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        await exchange.metrics.flush()
        assert not any(
            r["kind"] == "COMMAND_COMPLETE" and r["command_type"] == "order_process"
            for r in rows(exchange)
        )
        assert exchange.lifecycle.run_failure.code == "ENGINE_FAILED"
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_one_delivery_drain_can_commit_reports_from_multiple_uuid_runs():
    from types import SimpleNamespace

    exchange = _exchange()
    await _start(exchange)
    first = exchange.lifecycle.run_id
    await exchange.lifecycle.end_session()
    await exchange.lifecycle.start_session("second")
    second = exchange.lifecycle.run_id

    def deliver(_):
        for run_id, exec_id in ((first, "old-report"), (second, "new-report")):
            exchange._on_report_delivered(
                SimpleNamespace(order_id=1, run_id=run_id, exec_id=exec_id),
                exchange.scheduler.current_time,
            )

    try:
        await exchange._execute_committed_command("fix_delivery_due", deliver, None)
        await exchange.metrics.flush()
        timings = [
            r for r in rows(exchange) if r.get("metric_name") == "report_delivered"
        ]
        assert {(r["exec_id"], r["run_id"]) for r in timings} == {
            ("old-report", first),
            ("new-report", second),
        }
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_accepted_manual_checkpoint_settles_before_cancellation_unlocks(
    monkeypatch,
):
    import asyncio

    exchange = _exchange()
    await _start(exchange)
    entered, release = asyncio.Event(), asyncio.Event()
    original = exchange.journal.checkpoint

    async def paused(snapshot, reason="manual"):
        entered.set()
        await release.wait()
        return await original(snapshot, reason)

    monkeypatch.setattr(exchange.journal, "checkpoint", paused)
    task = asyncio.create_task(exchange.lifecycle.capture_checkpoint())
    try:
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert exchange.lifecycle.lock.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert any(
            r["kind"] == "CHECKPOINT_COMPLETE" and r.get("text") == "manual"
            for r in rows(exchange)
        )
        assert exchange.lifecycle.state == LifecycleState.ACTIVE
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_failed_end_prefix_flush_keeps_native_feed_coherent_for_retry(
    monkeypatch,
):
    from src.mockx.lifecycle import LifecycleError
    from src.mockx.metrics import PersistenceFlushResult, PersistenceFlushOutcome

    exchange = _exchange()
    await _start(exchange)

    async def fail():
        return PersistenceFlushResult(
            PersistenceFlushOutcome.FAILED, 0, 0, error="prefix unavailable"
        )

    monkeypatch.setattr(exchange.metrics, "flush", fail)
    try:
        with pytest.raises(LifecycleError):
            await exchange.lifecycle.end_session()
        assert exchange.lifecycle.state == LifecycleState.ACTIVE
        assert exchange.itch_feed.state.value == "ready"
        assert (
            exchange.glimpse_service.active_generation
            == exchange.lifecycle.run_generation
        )
        assert not any(r["kind"] == "RUN_ENDED" for r in rows(exchange))
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_asgi_startup_failure_is_not_a_successful_cli_run(monkeypatch):
    exchange = _exchange()

    class FailedServer:
        started = False

        def __init__(self, config):
            pass

        async def serve(self):
            return None

    monkeypatch.setattr("src.mockx.main.uvicorn.Server", FailedServer)
    try:
        with pytest.raises(RuntimeError, match="startup"):
            await exchange._run_api()
    finally:
        await exchange.aclose()

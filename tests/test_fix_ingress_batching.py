"""A burst uses one owner-loop wake without losing ingress boundaries."""

import asyncio
import threading

import pytest

from src.mockx.lifecycle import LifecycleIngressRejected, LifecycleState

from .test_fix_ingress_pipeline import _exchange, _new_order, _start, _stop


@pytest.mark.asyncio
async def test_concurrent_callbacks_share_one_wake_and_keep_acceptance_order(
    monkeypatch,
):
    exchange = _exchange()
    sessions, delivered = await _start(exchange, "CLIENT-A", "CLIENT-B")
    app = exchange.fix_app
    loop = asyncio.get_running_loop()
    original_wake = loop.call_soon_threadsafe
    wakes = []
    errors = []
    start = threading.Barrier(2)

    def wake(callback, *args, **kwargs):
        wakes.append(callback)
        return original_wake(callback, *args, **kwargs)

    def submit(client):
        try:
            start.wait(timeout=2)
            for index in range(65):
                app.fromApp(
                    _new_order(f"{client}-{index}", index + 1), sessions[client]
                )
        except Exception as exc:
            errors.append(exc)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(loop, "call_soon_threadsafe", wake)
            workers = [
                threading.Thread(target=submit, args=(client,)) for client in sessions
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=2)
            assert not any(worker.is_alive() for worker in workers)
        assert not errors
        assert len(wakes) == 1
        receipts = tuple(app._ingress_receipts.values())
        await asyncio.wait_for(app.wait_for_ingress(), 2)
        envelopes = [receipt.result() for receipt in receipts]
        assert [item.ingress_sequence for item in envelopes] == list(range(130))
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert [report.cl_ord_id for report in delivered] == [
            item.cl_ord_id for item in envelopes
        ]
    finally:
        await app.wait_for_ingress()
        await _stop(exchange)


@pytest.mark.asyncio
async def test_burst_coalesces_wakes_preserves_receipts_and_yields(monkeypatch):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    app = exchange.fix_app
    loop = asyncio.get_running_loop()
    original_wake = loop.call_soon_threadsafe
    wakes = []

    def wake(callback, *args, **kwargs):
        wakes.append(callback)
        return original_wake(callback, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(loop, "call_soon_threadsafe", wake)
            for index in range(193):
                app.fromApp(_new_order(f"BURST-{index}", index + 1), sessions["CLIENT"])
        assert len(wakes) == 1
        receipts = tuple(app._ingress_receipts.values())
        observed = []

        async def observe_fairness():
            while not all(receipt.done() for receipt in receipts):
                observed.append(sum(receipt.done() for receipt in receipts))
                await asyncio.sleep(0)

        observer = asyncio.create_task(observe_fairness())
        barrier = await asyncio.wait_for(app.wait_for_ingress(), 2)
        await observer
        assert barrier.through_sequence == 192
        assert any(0 < count < len(receipts) for count in observed)
        assert [receipt.result().ingress_sequence for receipt in receipts] == list(
            range(193)
        )
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert [report.cl_ord_id for report in delivered] == [
            f"BURST-{index}" for index in range(193)
        ]
        exchange.engine.books["TEST"].assert_invariants()

        # An idle queue can be woken again after the first drain exits.
        app.fromApp(_new_order("AFTER-IDLE", 194), sessions["CLIENT"])
        await asyncio.wait_for(app.wait_for_ingress(), 2)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert delivered[-1].cl_ord_id == "AFTER-IDLE"
    finally:
        await app.wait_for_ingress()
        await _stop(exchange)


@pytest.mark.asyncio
async def test_queued_burst_is_fenced_across_reset_without_dropping_rejections(
    monkeypatch,
):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    app = exchange.fix_app
    loop = asyncio.get_running_loop()
    original_wake = loop.call_soon_threadsafe
    held = []
    rejected = []
    monkeypatch.setattr(
        app,
        "_send_lifecycle_reject",
        lambda _session, code, **kwargs: rejected.append((code, kwargs["ref_seq_num"])),
    )
    try:
        # Hold only native-thread wakeups, while normal owner-loop transitions run.
        with monkeypatch.context() as patch:
            patch.setattr(
                loop,
                "call_soon_threadsafe",
                lambda callback, *args: held.append((callback, args)),
            )
            for index in range(130):
                app.fromApp(_new_order(f"OLD-{index}", index + 1), sessions["CLIENT"])
        old_receipts = tuple(app._ingress_receipts.values())
        await exchange.lifecycle.end_session()
        await exchange.lifecycle.start_session("next-generation")
        assert exchange.lifecycle.run_generation == 2
        for receipt in old_receipts:
            with pytest.raises(LifecycleIngressRejected, match="LIFECYCLE_CHANGED"):
                receipt.result()
        app.fromApp(_new_order("FRESH", 131), sessions["CLIENT"])
        for callback, args in held:
            original_wake(callback, *args)
        barrier = await asyncio.wait_for(app.wait_for_ingress(), 2)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert barrier.run_generation == 2
        assert barrier.through_sequence == 0
        assert rejected == [("LIFECYCLE_CHANGED", index + 1) for index in range(130)]
        assert [report.cl_ord_id for report in delivered] == ["FRESH"]
        assert len(exchange.engine.orders) == 1
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_queued_burst_failure_settles_all_barrier_receipts(monkeypatch):
    exchange = _exchange()
    sessions, _ = await _start(exchange)
    app = exchange.fix_app
    loop = asyncio.get_running_loop()
    original_wake = loop.call_soon_threadsafe
    held = []
    rejected = []
    monkeypatch.setattr(
        app,
        "_send_lifecycle_reject",
        lambda _session, code, **kwargs: rejected.append((code, kwargs["ref_seq_num"])),
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                loop,
                "call_soon_threadsafe",
                lambda callback, *args: held.append((callback, args)),
            )
            for index in range(130):
                app.fromApp(
                    _new_order(f"FAILED-{index}", index + 1), sessions["CLIENT"]
                )
        captured = app.ingress_high_water_mark()
        receipts = tuple(app._ingress_receipts.values())
        exchange.lifecycle._publish(LifecycleState.FAILED)
        for callback, args in held:
            original_wake(callback, *args)
        reached = await asyncio.wait_for(app.await_ingress_enqueued(), 2)
        assert reached == captured
        assert all(receipt.done() and receipt.result() is None for receipt in receipts)
        assert rejected == [("LIFECYCLE_FAILED", index + 1) for index in range(130)]
        assert not app._pending_ingress
        assert not app._ingress_receipts
        assert not exchange.engine.orders
    finally:
        exchange.lifecycle._publish(LifecycleState.ACTIVE)
        await _stop(exchange)

"""Diagnostics may observe the paced loop without blocking historical orders."""

import asyncio
from types import SimpleNamespace

import pytest

from examples.coinapi_replay import runner as module
from examples.coinapi_replay.source import SourceEvent
from examples.coinapi_replay.tracker import PendingCommandError
from src.mockx.models import Side
from tests.exact_helpers import TEST_INSTRUMENT


def event(index, time_ns):
    return SourceEvent(
        index,
        time_ns,
        time_ns,
        "ADD",
        str(index),
        str(index),
        Side.BUY,
        100,
        1,
        1,
        0,
        None,
    )


def isolated_runner(monkeypatch, events):
    async def no_op(*args, **kwargs):
        pass

    transport = SimpleNamespace(
        start=no_op,
        stop=no_op,
        fence=no_op,
        take_reports=lambda: [],
    )
    feed = SimpleNamespace(
        start=no_op,
        close=no_op,
        check=lambda: None,
        wait_for_snapshot=no_op,
        sequence=0,
        market_time_ns=0,
        received_ns=0,
        frames=1,
        conflated_sequences=0,
        bids=(),
        asks=(),
    )
    monkeypatch.setattr(module, "FixTransport", lambda *args: transport)
    monkeypatch.setattr(module, "FeedObserver", lambda *args: feed)
    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kwargs: SimpleNamespace(aclose=no_op)
    )
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.now / 1e9,
            monotonic_ns=lambda: clock.now,
        ),
    )
    source = SimpleNamespace(
        snapshot=(),
        start_ns=0,
        reference_depth=lambda: ((), ()),
        events=lambda: iter(events),
    )
    instance = module.ReplayRunner(
        source,
        TEST_INSTRUMENT,
        "http://unused",
        "unused",
        "test",
        module.ReplayOptions(
            controller=False,
            controller_order_quantity="1",
            max_lag_seconds=2,
        ),
    )

    async def request(method, path, **kwargs):
        if path.endswith("/start"):
            return {"run_generation": 1, "seed_orders": []}
        if path.endswith("/end"):
            return {"status": "ended"}
        return {"sequence": 0, "bids": [], "asks": []}

    instance._json = request
    stats = {
        "scheduler": {"realtime_lag_ms": 0, "current_time": "1970-01-01T00:00:00Z"},
        "fix_delivery": {"queue_depth": 0},
    }
    return instance, clock, stats


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stats", [False, True], ids=["slow", "failed"])
async def test_delayed_stats_response_does_not_block_next_source_order(
    monkeypatch, fail_stats
):
    instance, clock, stats = isolated_runner(
        monkeypatch, [event(1, 0), event(2, 1_000_000_000)]
    )
    stats_started, release_stats, second_sent = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    calls = 0

    async def delayed_stats():
        nonlocal calls
        calls += 1
        if calls == 2:
            stats_started.set()
            await release_stats.wait()
            if fail_stats:
                raise TimeoutError("delayed diagnostic timed out")
        return stats

    def send(intent, **kwargs):
        if intent.event_index == 1:
            clock.now = 600_000_000
        else:
            second_sent.set()

    instance._stats = delayed_stats
    instance._send = send
    task = asyncio.create_task(instance.run())
    try:
        await asyncio.wait_for(stats_started.wait(), 1)
        clock.now = 1_000_000_000
        # The response remains withheld until the next historical order is sent.
        await asyncio.wait_for(second_sent.wait(), 1)
        assert calls == 2  # At most one diagnostic request is outstanding.
        release_stats.set()
        if fail_stats:
            with pytest.raises(TimeoutError, match="diagnostic timed out"):
                await asyncio.wait_for(task, 1)
            assert instance.result["status"] == "failed"
        else:
            assert (await asyncio.wait_for(task, 1))["status"] == "completed"
    finally:
        release_stats.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_controller_uses_fresh_clock_and_oldest_unreleased_source_lag(
    monkeypatch,
):
    instance, clock, stats = isolated_runner(
        monkeypatch, [event(index, 0) for index in range(1, 130)]
    )

    async def get_stats():
        return stats

    instance._stats = get_stats
    instance._send = lambda *args, **kwargs: setattr(clock, "now", 600_000_000)
    decisions = []
    instance.controller.decide = lambda now, replay_lag_ns: decisions.append(
        (now, replay_lag_ns)
    )
    assert (await instance.run())["status"] == "completed"
    assert decisions[0] == (600_000_000, 600_000_000)


@pytest.mark.asyncio
async def test_interrupt_cancels_the_single_pending_stats_request(monkeypatch):
    instance, clock, stats = isolated_runner(
        monkeypatch, [event(1, 0), event(2, 10_000_000_000)]
    )
    started, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0

    async def blocked_stats():
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return stats

    instance._stats = blocked_stats
    instance._send = lambda *args, **kwargs: setattr(clock, "now", 600_000_000)
    task = asyncio.create_task(instance.run())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert instance.result["status"] == "interrupted"
    assert calls == 2


@pytest.mark.asyncio
async def test_each_source_release_measures_elapsed_work_inside_the_batch(monkeypatch):
    instance, clock, stats = isolated_runner(monkeypatch, [event(1, 0), event(2, 0)])

    async def get_stats():
        return stats

    instance._stats = get_stats
    instance._send = lambda *args, **kwargs: setattr(clock, "now", 600_000_000)
    result = await instance.run()
    assert result["status"] == "completed"
    assert result["release_lag"]["count"] == 2
    assert result["release_lag"]["mean_ms"] == 300
    assert result["release_lag"]["max_ms"] == 600


def observe_sleeps(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(
        module,
        "asyncio",
        SimpleNamespace(
            sleep=sleep,
            create_task=asyncio.create_task,
            gather=asyncio.gather,
            CancelledError=asyncio.CancelledError,
        ),
    )
    return delays


@pytest.mark.asyncio
async def test_newly_due_source_work_yields_without_an_extra_timer_wait(monkeypatch):
    instance, clock, stats = isolated_runner(
        monkeypatch, [event(1, 0), event(2, 1_000_000_000)]
    )
    delays = observe_sleeps(monkeypatch)

    async def get_stats():
        return stats

    def decide(*args, **kwargs):
        clock.now = 1_000_000_000

    instance._stats = get_stats
    instance._send = lambda *args, **kwargs: None
    instance.controller.decide = decide
    assert (await instance.run())["status"] == "completed"
    assert delays[0] == 0


@pytest.mark.asyncio
async def test_unresolved_deferred_only_work_keeps_a_timer_wait(monkeypatch):
    instance, _, stats = isolated_runner(monkeypatch, [event(1, 0)])
    delays = observe_sleeps(monkeypatch)
    attempts = 0

    async def get_stats():
        return stats

    def translate(_event):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PendingCommandError("replacement acknowledgement pending")

    instance._stats = get_stats
    instance.ledger.translate = translate
    assert (await instance.run())["status"] == "completed"
    assert delays[:2] == [0.001, 0.001]

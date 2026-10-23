"""The controller must age snapshots by source time and invalidate disconnects."""

from datetime import datetime, timezone
import json
import time

import pytest
import websockets

from examples.coinapi_replay.controller import ControllerConfig
from examples.coinapi_replay.runner import ReplayOptions, ReplayRunner
from tests.exact_helpers import TEST_INSTRUMENT
from tests.test_coinapi_runner import FakeVenue, configuration, prepared


def snapshot():
    return {"type": "market_data", "symbol": "TEST", "sequence": 1,
            "timestamp": "1970-01-01T00:00:00Z",
            "bids": [{"price": "99", "qty": "10"}],
            "asks": [{"price": "101", "qty": "10"}, {"price": "103", "qty": "10"}]}


class Stream:
    def __init__(self, messages, runner):
        self.messages, self.runner = messages, runner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def __aiter__(self):
        for message in self.messages:
            yield json.dumps(message)
        self.runner._eof = True


@pytest.mark.asyncio
async def test_feed_delay_and_heartbeats_do_not_make_an_old_book_fresh(monkeypatch):
    venue = FakeVenue()
    runner = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(), venue.http, venue,
                          options=ReplayOptions(profile="interactive"),
                          controller_config=ControllerConfig(enabled=True, max_book_age_ns=10_000_000))
    runner._base_time = datetime(1970, 1, 1, tzinfo=timezone.utc)
    runner._wall_start = time.monotonic_ns() - 1_000_000_000
    runner.controller.observe_target(204, time.monotonic_ns())
    observations = []
    observe = runner.controller.observe_book

    def capture(bids, asks, observed_ns, *, received_ns=None):
        observations.append((bids, asks, observed_ns))
        observe(bids, asks, observed_ns, received_ns=received_ns)

    runner.controller.observe_book = capture
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: Stream(
        [snapshot(), {"type": "heartbeat"}], runner))
    try:
        await runner._feed()
        assert runner.summary["feed_snapshots"] == 1
        assert runner.summary["max_feed_delay_ns"] >= 1_000_000_000
        assert observations[0][2] <= runner._wall_start
        assert runner.controller.decide(time.monotonic_ns()) is None
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_clean_feed_disconnect_invalidates_book_before_reconnect(monkeypatch):
    venue = FakeVenue()
    runner = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(), venue.http, venue,
                          options=ReplayOptions(profile="interactive"),
                          controller_config=ControllerConfig(enabled=True))
    runner._base_time = datetime(1970, 1, 1, tzinfo=timezone.utc)
    runner._wall_start = time.monotonic_ns()
    observations = []
    runner.controller.observe_book = lambda bids, asks, observed_ns, **kwargs: observations.append((bids, asks))
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: Stream([snapshot()], runner))
    try:
        await runner._feed()
        assert len(observations) == 2
        assert observations[-1] == ((), ())
    finally:
        await venue.http.aclose()

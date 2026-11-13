import asyncio

import pytest

from examples.coinapi_replay.feed import FeedObserver, TimingHistogram, timestamp_ns
from src.mockx.instruments import Instrument
from tests.exact_helpers import TEST_INSTRUMENT


@pytest.mark.asyncio
async def test_final_snapshot_fence_waits_for_websocket_delivery():
    feed = FeedObserver("http://localhost:8001", TEST_INSTRUMENT)
    payload = {
        "type": "market_data",
        "symbol": "TEST",
        "sequence": 5,
        "timestamp": "1970-01-01T00:00:01Z",
        "bids": [{"price": "100", "qty": "2"}],
        "asks": [{"price": "102", "qty": "3"}],
    }
    waiting = asyncio.create_task(feed.wait_for_snapshot(payload, timeout=0.1))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    feed.apply(payload)
    await waiting


@pytest.mark.asyncio
async def test_final_snapshot_fence_rejects_missing_or_wrong_depth():
    feed = FeedObserver("http://localhost:8001", TEST_INSTRUMENT)
    payload = {
        "type": "market_data",
        "symbol": "TEST",
        "sequence": 5,
        "timestamp": "1970-01-01T00:00:01Z",
        "bids": [],
        "asks": [],
    }
    with pytest.raises(TimeoutError, match="final market-data"):
        await feed.wait_for_snapshot(payload, timeout=0.01)
    feed.apply(payload)
    wrong = {**payload, "bids": [{"price": "100", "qty": "2"}]}
    with pytest.raises(RuntimeError, match="final market-data"):
        await feed.wait_for_snapshot(wrong, timeout=0.01)


@pytest.mark.asyncio
async def test_final_snapshot_fence_checks_levels_beyond_controller_depth():
    feed = FeedObserver("http://localhost:8001", TEST_INSTRUMENT)
    payload = {
        "type": "market_data",
        "symbol": "TEST",
        "sequence": 1,
        "timestamp": "1970-01-01T00:00:01Z",
        "bids": [{"price": str(100 - i), "qty": "2"} for i in range(15)],
        "asks": [{"price": str(102 + i), "qty": "3"} for i in range(15)],
    }
    feed.apply(payload)
    wrong = {**payload, "asks": [dict(level) for level in payload["asks"]]}
    wrong["asks"][12]["qty"] = "4"
    with pytest.raises(RuntimeError, match="final market-data"):
        await feed.wait_for_snapshot(wrong, timeout=0.01)


def test_clock_projection_is_exact_at_microsecond_resolution():
    assert timestamp_ns("1970-01-01T00:00:01.000001Z") == 1_000_001_000


def test_histogram_is_bounded_but_tracks_all_samples_and_maximum():
    values = TimingHistogram()
    for _ in range(100_000):
        values.add_ms(1.25)
    values.add_ms(1500)
    result = values.summary()
    assert len(values.buckets) == 2049
    assert result["count"] == 100_001
    assert result["p99_upper_bound_ms"] == 1.5
    assert result["max_ms"] == 1500

"""Observe the public WebSocket feed without privileged engine access."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import time

from websockets.asyncio.client import connect


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def timestamp_ns(value: str) -> int:
    delta = datetime.fromisoformat(value.replace("Z", "+00:00")) - EPOCH
    return (
        (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    ) * 1000


class FeedObserver:
    def __init__(self, base_url, instrument):
        self.url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        self.url += f"/ws/{instrument.symbol}"
        self.instrument = instrument
        self.ready = asyncio.Event()
        self.task = None
        self.error = None
        self.latest = None
        self.bids = ()
        self.asks = ()
        self.sequence = -1
        self.market_time_ns = 0
        self.received_ns = 0
        self.frames = 0
        self.conflated_sequences = 0
        self.stopping = False

    async def start(self, timeout=15):
        self.task = asyncio.create_task(self._read(), name="coinapi-feed")
        await asyncio.wait_for(self.ready.wait(), timeout)
        self.check()

    def check(self):
        if self.error:
            raise RuntimeError(self.error)

    async def wait_for_snapshot(self, snapshot, timeout):
        """Fence the public subscriber against the quiescent REST book cut."""
        deadline = time.monotonic() + timeout
        while self.sequence < snapshot["sequence"]:
            self.check()
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for final market-data snapshot")
            await asyncio.sleep(0.001)
        self.check()
        for side in ("bids", "asks"):
            # Both public interfaces use the same canonical level schema. Keep
            # controller work bounded, but verify every final displayed level.
            if self.latest is None or self.latest.get(side) != snapshot[side]:
                raise RuntimeError(
                    "Observed final market-data depth differs from exchange"
                )

    def apply(self, payload):
        if payload.get("type") == "error":
            raise ValueError(f"Market-data error: {payload}")
        if payload.get("type") != "market_data":
            return
        if payload.get("symbol") != self.instrument.symbol:
            raise ValueError("Unexpected market-data symbol")
        sequence = payload["sequence"]
        if sequence < self.sequence:
            raise ValueError("Market-data sequence regressed")
        if sequence == self.sequence:
            return
        if self.sequence >= 0:
            self.conflated_sequences += max(0, sequence - self.sequence - 1)

        def levels(side):
            return tuple(
                (
                    self.instrument.price_to_ticks(row["price"]),
                    self.instrument.quantity_to_lots(row["qty"]),
                )
                for row in payload[side][:11]
            )

        self.bids, self.asks = levels("bids"), levels("asks")
        self.latest = payload
        self.sequence = sequence
        self.market_time_ns = timestamp_ns(payload["timestamp"])
        self.received_ns = time.monotonic_ns()
        self.frames += 1
        self.ready.set()

    async def _read(self):
        try:
            async with connect(self.url, max_size=16 * 1024 * 1024, max_queue=8) as ws:
                async for raw in ws:
                    self.apply(json.loads(raw))
            if not self.stopping:
                self.error = "Market-data connection closed during replay"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.stopping:
                self.error = f"Market-data connection failed: {exc}"
        finally:
            self.ready.set()

    async def close(self):
        self.stopping = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class TimingHistogram:
    """Bounded half-millisecond buckets; max and count cover the whole run."""

    def __init__(self):
        self.buckets = [0] * 2049
        self.count = 0
        self.maximum_ms = 0.0
        self.total_ms = 0.0

    def add_ms(self, value):
        value = max(0.0, float(value))
        self.count += 1
        self.total_ms += value
        self.maximum_ms = max(self.maximum_ms, value)
        self.buckets[min(2048, int(value * 2))] += 1

    def summary(self):
        rank = max(1, (self.count * 99 + 99) // 100)
        cumulative = 0
        percentile = 0.0
        for index, count in enumerate(self.buckets):
            cumulative += count
            if cumulative >= rank:
                percentile = (index + 1) / 2 if index < 2048 else self.maximum_ms
                break
        return {
            "count": self.count,
            "mean_ms": self.total_ms / self.count if self.count else 0,
            "p99_upper_bound_ms": percentile if self.count else 0,
            "max_ms": self.maximum_ms,
        }

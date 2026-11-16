"""Qualification wraps the existing C++ feed engine rather than replacing it."""

import asyncio
import os
from pathlib import Path
import socket
import struct
from types import SimpleNamespace

import pytest

from examples.coinapi_replay.native_probe import NativeProbe, parse_frame
from src.mockx.configuration import validate_config
from src.mockx.main import MockExchange
from src.mockx.models import Side
from tests.test_itch_integration import itch_config, submit


def test_console_frame_keeps_wire_price_and_quantity_exact():
    text = (
        "\x1b[2J\x1b[HMockX GLIMPSE/ITCH market feed  state=LIVE  session=MX00000001  next_seq=9\n"
        "packets=4  messages=3  ring_drops=0  gaps=0  resyncs=0\n\n"
        "BTC-USDT  trading_state=T\nBID PRICE     BID SHARES   ORDERS    ASK PRICE     ASK SHARES   ORDERS\n"
        f"{'99.0010':<14}{200:>14}{2:>9}    {'100.0020':<14}{300:>14}{1:>9}\n\nCtrl+C to stop\n"
    )
    frame = parse_frame(text, "BTC-USDT")
    assert frame["bids"] == [(990010, 200)]
    assert frame["asks"] == [(1000020, 300)]
    assert frame["next_sequence"] == 9
    assert frame["gaps"] == 0


@pytest.mark.asyncio
async def test_close_releases_active_soup_relay_before_waiting_for_server():
    connected = asyncio.Event()
    remote_tasks = set()

    async def remote(reader, writer):
        task = asyncio.current_task()
        remote_tasks.add(task)
        connected.set()
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            remote_tasks.discard(task)

    upstream = await asyncio.start_server(remote, "127.0.0.1", 0)
    config = SimpleNamespace(
        itch=SimpleNamespace(enabled=True),
        glimpse=SimpleNamespace(enabled=True),
        soupbintcp=SimpleNamespace(
            host="127.0.0.1", port=upstream.sockets[0].getsockname()[1]
        ),
    )
    probe = NativeProbe(config, SimpleNamespace(symbol="TEST"), executable="unused")
    probe.server = await asyncio.start_server(probe._soup_connection, "127.0.0.1", 0)
    _, client = await asyncio.open_connection(
        "127.0.0.1", probe.server.sockets[0].getsockname()[1]
    )
    try:
        await asyncio.wait_for(connected.wait(), 1)
        await asyncio.wait_for(probe.close(), 0.5)
        assert not probe.tasks
        await probe.close()  # Repeated owner cleanup is safe.
    finally:
        for task in tuple(probe.tasks):
            task.cancel()
        await asyncio.gather(*tuple(probe.tasks), return_exceptions=True)
        client.close()
        await client.wait_closed()
        probe.server.close()
        upstream.close()
        for task in tuple(remote_tasks):
            task.cancel()
        await asyncio.gather(*tuple(remote_tasks), return_exceptions=True)
        await probe.server.wait_closed()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_recovery_does_not_spend_its_gap_on_packets_before_glimpse_cut():
    config = SimpleNamespace(
        itch=SimpleNamespace(enabled=True), glimpse=SimpleNamespace(enabled=True)
    )
    probe = NativeProbe(config, SimpleNamespace(symbol="TEST"), executable="unused")
    forwarded = []
    probe.sender = SimpleNamespace(sendto=lambda data, address: forwarded.append(data))
    probe.latest = {
        "state": "LIVE",
        "next_sequence": 3013,
        "resyncs": 0,
        "gaps": 0,
        "ring_drops": 0,
    }
    probe.snapshot_cuts = [3013]
    recovery = asyncio.create_task(probe.request_recovery())
    try:
        await asyncio.sleep(0)
        stale = b"MX00000001" + struct.pack(">QH", 3012, 1) + b"stale"
        probe._relay_packet(stale)
        assert (
            probe.planned_drops == 0
        ), "A pre-cut seed packet cannot expose a real gap"
        assert forwarded == [stale]
        live = b"MX00000001" + struct.pack(">QH", 3013, 1) + b"live"
        probe._relay_packet(live)
        assert probe.planned_drops == 1
        assert forwarded == [stale]
        assert probe.result()["dropped_sequence"] == 3013
        assert probe.result()["upstream_next_sequence"] == 3014
    finally:
        recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)


def _port(kind):
    with socket.socket(socket.AF_INET, kind) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.asyncio
async def test_interrupted_close_finishes_owned_cleanup_and_is_idempotent():
    config = SimpleNamespace(
        itch=SimpleNamespace(enabled=True), glimpse=SimpleNamespace(enabled=True)
    )
    probe = NativeProbe(config, SimpleNamespace(symbol="TEST"), executable="unused")
    terminated, release = asyncio.Event(), asyncio.Event()
    closed = []

    class Process:
        returncode = None

        def terminate(self):
            terminated.set()

        async def wait(self):
            await release.wait()
            self.returncode = 0
            return 0

    probe.process = Process()
    probe.sender = SimpleNamespace(close=lambda: closed.append("sender"))
    owner = asyncio.create_task(probe.close())
    try:
        await asyncio.wait_for(terminated.wait(), 1)
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert closed == ["sender"]
        await probe.close()
        assert closed == ["sender"]
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


def _client():
    configured = os.environ.get("MOCKX_NATIVE_FEED_CLIENT")
    root = Path(__file__).resolve().parents[1]
    candidates = (
        [Path(configured)]
        if configured
        else [
            root
            / "examples/cpp_market_feed/build-replay/Release/mockx_market_feed.exe",
            root / "examples/cpp_market_feed/build-replay/mockx_market_feed",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip("Build cpp_market_feed/build-replay or set MOCKX_NATIVE_FEED_CLIENT")


@pytest.mark.asyncio
async def test_real_native_late_join_gap_recovery_and_exact_book():
    executable = _client()
    raw = itch_config()
    raw["moldudp64"]["multicast_port"] = _port(socket.SOCK_DGRAM)
    raw["glimpse"] = {"enabled": True}
    raw["soupbintcp"] = {
        "host": "127.0.0.1",
        "port": _port(socket.SOCK_STREAM),
        "session_prefix": "GS",
        "credentials": {"GLIMP": "SECRET"},
    }
    config = validate_config(raw, environ={})
    exchange = MockExchange(config)
    probe = NativeProbe(config, exchange.instruments.get("TEST"), executable=executable)
    await exchange.start_components()
    try:
        await exchange.lifecycle.start_session("native-probe")
        submit(exchange, "initial-bid", Side.BUY, 3, 99, "bid-owner")
        submit(exchange, "initial-ask", Side.SELL, 4, 101, "ask-owner")
        # The client joins after the seed exists: GLIMPSE supplies initial state.
        await probe.start()
        probe.assert_depth([(99, 3)], [(101, 4)])
        recovery = asyncio.create_task(probe.request_recovery())
        await asyncio.sleep(0.05)
        submit(exchange, "drop-this", Side.BUY, 2, 98, "bid-owner")
        await asyncio.sleep(0.03)
        submit(exchange, "expose-gap", Side.SELL, 1, 102, "ask-owner")
        await recovery
        await probe.wait_for_sequence(exchange.itch_feed.sequence_number)
        probe.assert_depth([(99, 3), (98, 2)], [(101, 4), (102, 1)])
        result = probe.result()
        assert result["planned_drops"] == 1
        assert result["gaps"] == 1
        assert result["resyncs"] == 1
        assert len(result["snapshot_cuts"]) == 2
        assert result["snapshot_cuts"][1] > result["snapshot_cuts"][0]
        assert result["source_gaps"] == 0
        await exchange.lifecycle.end_session()
        await probe.wait_for_end()
    finally:
        await probe.close()
        await exchange.stop_components()

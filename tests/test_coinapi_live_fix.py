"""CSV-to-book qualification over native QuickFIX sockets and public HTTP APIs."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import socket
import uuid

import httpx
import pytest
import uvicorn

from examples.coinapi_replay.controller import ControllerConfig
from examples.coinapi_replay.runner import ReplayError, ReplayOptions, ReplayRunner
from examples.coinapi_replay.source import prepare_csv, source_digest
from examples.coinapi_replay.transport import NativeFixTransport
from examples.fix_support import send_limit_order
from src.mockx.configuration import validate_config
from src.mockx.gateway import fix
from src.mockx.instruments import Instrument
from src.mockx.main import MockExchange
from .api_helpers import api_config


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "coinapi_replay" / "sample_l3.csv"
ROLES = ("replay_buy", "replay_sell", "controller", "strategy_bot")


def _settings(directory, acceptor, port, venue):
    role = "acceptor" if acceptor else "initiator"
    store, logs = directory / f"{role}-store", directory / f"{role}-logs"
    store.mkdir()
    logs.mkdir()
    lines = [
        "[DEFAULT]", f"ConnectionType={role}",
        "StartTime=00:00:00", "EndTime=00:00:00",
        f"FileStorePath={store.as_posix()}", f"FileLogPath={logs.as_posix()}",
        "UseDataDictionary=Y", f"DataDictionary={(ROOT / 'configs' / 'FIX44.xml').as_posix()}",
        "ValidateFieldsOutOfOrder=Y", "ValidateFieldsHaveValues=Y",
        "ResetOnLogon=Y", "ResetOnLogout=Y", "ResetOnDisconnect=Y",
        "HeartBtInt=30", "LogonTimeout=5", "LogoutTimeout=1", "ReconnectInterval=1",
    ]
    if acceptor:
        lines.append(f"SocketAcceptPort={port}")
    else:
        lines.extend(["SocketConnectHost=127.0.0.1", f"SocketConnectPort={port}"])
    for participant in ROLES:
        sender, target = (venue, participant) if acceptor else (participant, venue)
        lines.extend(["[SESSION]", "BeginString=FIX.4.4",
                      f"SenderCompID={sender}", f"TargetCompID={target}"])
    path = directory / f"{role}.cfg"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@asynccontextmanager
async def _wire(directory, *, seed_mode="bulk", profile="exact", source=SAMPLE,
                timeout=5, controller=False):
    if fix is None:
        pytest.skip("Native QuickFIX required")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    config = api_config(fix_enabled=True)
    # Native QuickFIX's process-wide registry can retain stopped acceptors
    # until their owners are collected. Each independent venue gets an ID.
    venue = "REPLAY_" + uuid.uuid4().hex[:12]
    config["fix"]["session_participants"] = {
        f"FIX.4.4:{venue}->{role}": role for role in ROLES
    }
    config = validate_config(config, environ={})
    instrument = Instrument.from_config("TEST", config.instruments["TEST"])
    exchange = MockExchange(config, str(_settings(directory, True, port, venue)))
    transport = NativeFixTransport(_settings(directory, False, port, venue), instrument)
    web_server = web_task = None
    try:
        await exchange.start_components()
        http_options = dict(transport=httpx.ASGITransport(app=exchange.app), base_url="http://test")
        if controller:
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                web_port = reserved.getsockname()[1]
            web_server = uvicorn.Server(uvicorn.Config(
                exchange.app, host="127.0.0.1", port=web_port, lifespan="off", log_level="error"))
            web_task = asyncio.create_task(web_server.serve())
            async with asyncio.timeout(5):
                while not web_server.started:
                    await asyncio.sleep(0.005)
            http_options = dict(base_url=f"http://127.0.0.1:{web_port}")
        with prepare_csv(source, instrument) as prepared:
            async with httpx.AsyncClient(**http_options) as admin:
                runner = ReplayRunner(prepared, instrument, config, admin, transport,
                                      options=ReplayOptions(seed_mode=seed_mode, profile=profile,
                                                            timeout=timeout),
                                      controller_config=ControllerConfig(
                                          enabled=controller, max_order_qty_lots=5,
                                          max_total_qty_lots=5, max_depth_levels=1,
                                          reaction_interval_ns=10_000_000))
                yield runner, exchange, transport
    finally:
        await transport.close()
        if web_server:
            web_server.should_exit = True
            await asyncio.wait_for(web_task, 5)
        await exchange.stop_components()


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_mode", ["bulk", "fix"])
async def test_native_exact_csv_replay_and_repeatability(tmp_path, seed_mode):
    """The same fixture qualifies twice, through either initialization path."""
    digests = []
    for repeat in range(2):
        directory = tmp_path / str(repeat)
        directory.mkdir()
        async with _wire(directory, seed_mode=seed_mode) as (runner, exchange, _):
            result = await runner.run()
            assert result["status"] == "qualified"
            assert result["source_events"] == 9
            assert result["validated_checkpoints"] == 10
            assert result["controller_requests"] == 0
            assert result["historical_requests"] == 9 + (5 if seed_mode == "fix" else 0)
            digests.append(source_digest(runner._ledger_source_orders()))
            exchange.engine.books["TEST"].assert_invariants()
    assert digests[0] == digests[1]


@pytest.mark.asyncio
async def test_native_rejects_source_identity_mismatch_even_with_equal_depth(tmp_path):
    """FIFO uncertainty is a qualification failure, not a book repair."""
    source = tmp_path / "fifo.csv"
    rows = SAMPLE.read_text().splitlines()
    # Same-price source IDs a1/a2: the recorded execution hits a2, but MockX
    # correctly matches the first installed order a1. L2 depth still agrees.
    rows[4] = rows[4].replace(";102;", ";101;")
    source.write_text("\n".join(rows[:6] + [rows[7].replace(";a1;", ";a2;")]) + "\n")
    async with _wire(tmp_path, source=source, timeout=3) as (runner, _, _):
        with pytest.raises(ReplayError, match="not qualified"):
            await runner.run()


@pytest.mark.asyncio
async def test_native_strategy_intercepts_historical_match_once(tmp_path):
    """A better strategy ask gets the historical buy; no passive double debit."""
    source = tmp_path / "interception.csv"
    rows = SAMPLE.read_text().splitlines()
    match = rows[7].replace("00:00:00.2000000Z", "00:00:00.4000000Z")
    # Give the strategy enough wall time to get a real FIX acknowledgment.
    source.write_text("\n".join(rows[:6] + [match]) + "\n")
    async with _wire(tmp_path, profile="interactive", source=source) as (runner, exchange, transport):
        reports = []
        transport.application.add_listener(reports.append)

        async def strategy():
            while not runner._started_run:
                await asyncio.sleep(0.001)
            assert await asyncio.to_thread(transport.application.wait_for_sessions, ["strategy_bot"], 3)
            send_limit_order(transport.application.session("strategy_bot"), cl_ord_id="STRATEGY-ASK",
                             symbol="TEST", side="2", quantity="3", price="100")

        task = asyncio.create_task(strategy())
        try:
            result = await runner.run()
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert result["historical_requests"] == 1
        assert result["status"] == "completed"
        source_order = runner.ledger.orders_snapshot()["a1"]
        assert source_order.leaves_qty_lots == 10
        assert source_order.cum_qty_lots == 0
        strategy_fills = [r for r in reports if r.cl_ord_id == "STRATEGY-ASK" and r.exec_type == "F"]
        aggressive_fills = [r for r in reports if r.sender_comp_id == "replay_buy" and r.exec_type == "F"]
        assert len(strategy_fills) == len(aggressive_fills) == 1
        assert strategy_fills[0].last_qty == "3.000"
        assert strategy_fills[0].trade_id == aggressive_fills[0].trade_id
        exchange.engine.books["TEST"].assert_invariants()


@pytest.mark.asyncio
async def test_native_controller_consumes_strategy_level_with_separate_attribution(tmp_path):
    """Real WebSocket depth triggers an ordinary controller FIX market order."""
    source = tmp_path / "controller.csv"
    rows = SAMPLE.read_text().splitlines()
    final = rows[10].replace("00:00:00.5000000Z", "00:00:01.0000000Z")
    source.write_text("\n".join(rows[:7] + [final]) + "\n")
    async with _wire(tmp_path, profile="interactive", source=source, controller=True) as (runner, exchange, transport):
        reports = []
        transport.application.add_listener(reports.append)

        async def strategy():
            while not runner._wall_start:
                await asyncio.sleep(0.001)
            assert await asyncio.to_thread(transport.application.wait_for_sessions, ["strategy_bot"], 3)
            send_limit_order(transport.application.session("strategy_bot"), cl_ord_id="CONTROLLED-ASK",
                             symbol="TEST", side="2", quantity="3", price="100")

        task = asyncio.create_task(strategy())
        try:
            result = await runner.run()
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert result["source_events"] == result["historical_requests"] == 2
        assert result["controller_requests"] == 1
        assert result["controller_fill_qty_lots"] == 3
        assert result["controller_fill_notional_ticks_lots"] == 300
        assert result["feed_snapshots"] > 0
        controller_fills = [r for r in reports if r.sender_comp_id == "controller" and r.exec_type == "F"]
        strategy_fills = [r for r in reports if r.cl_ord_id == "CONTROLLED-ASK" and r.exec_type == "F"]
        assert len(controller_fills) == len(strategy_fills) == 1
        assert controller_fills[0].trade_id == strategy_fills[0].trade_id
        assert not runner.pending and runner.controller.stopped
        exchange.engine.books["TEST"].assert_invariants()

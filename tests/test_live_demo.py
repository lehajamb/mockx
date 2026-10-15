"""Contracts for the continuous two-session FIX demo."""

from __future__ import annotations

import random
from pathlib import Path
import signal
from types import SimpleNamespace
import subprocess
import sys

import pytest

from examples import live_market as live_module
from examples.fix_support import FixReport, _transact_time, fix
from examples.live_market import (
    LiveMarket,
    LiveMarketState,
    LiveQuote,
    PlannedLevel,
    build_ladder,
    next_midpoint,
)
from src.mockx.configuration import load_config
from src.mockx.instruments import Instrument


ROOT = Path(__file__).resolve().parents[1]


def _quickfix_sessions(path: Path) -> list[dict[str, str]]:
    defaults: dict[str, str] = {}
    sessions: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[DEFAULT]":
            current = defaults
            continue
        if line == "[SESSION]":
            current = {}
            sessions.append(current)
            continue
        if current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
    return [{**defaults, **session} for session in sessions]


def test_live_demo_configs_have_reversed_sessions_and_distinct_owners():
    config = load_config(
        ROOT / "examples" / "live_exchange.yml",
        environ={},
    )
    acceptor = _quickfix_sessions(
        ROOT / "examples" / "live_acceptor.cfg"
    )
    client = _quickfix_sessions(ROOT / "examples" / "live_client.cfg")

    acceptor_ids = {
        (
            session["BeginString"],
            session["SenderCompID"],
            session["TargetCompID"],
        )
        for session in acceptor
    }
    reversed_client_ids = {
        (
            session["BeginString"],
            session["TargetCompID"],
            session["SenderCompID"],
        )
        for session in client
    }

    assert config.scheduler.mode == "external"
    assert config.admin_api.port == 8001
    assert config.questdb.url is not None
    assert acceptor_ids == reversed_client_ids
    assert {
        session["AllowUnknownMsgFields"] for session in client
    } == {"Y"}
    assert set(config.fix.session_participants) == {
        "FIX.4.4:MOCKX_DEMO->liquidity_bot",
        "FIX.4.4:MOCKX_DEMO->taker_bot",
    }
    assert len(set(config.fix.session_participants.values())) == 2


def test_docker_live_demo_uses_compose_service_addresses():
    config = load_config(
        ROOT / "examples" / "live_exchange_docker.yml",
        environ={},
    )
    acceptor = _quickfix_sessions(
        ROOT / "examples" / "live_acceptor.cfg"
    )
    client = _quickfix_sessions(
        ROOT / "examples" / "live_client_docker.cfg"
    )

    acceptor_ids = {
        (
            session["BeginString"],
            session["SenderCompID"],
            session["TargetCompID"],
        )
        for session in acceptor
    }
    reversed_client_ids = {
        (
            session["BeginString"],
            session["TargetCompID"],
            session["SenderCompID"],
        )
        for session in client
    }

    assert config.admin_api.host == "0.0.0.0"
    assert config.admin_api.port == 8000
    assert config.questdb.url is not None
    assert acceptor_ids == reversed_client_ids
    assert {session["SocketConnectHost"] for session in client} == {
        "mockx"
    }
    assert {session["SocketConnectPort"] for session in client} == {
        "4001"
    }
    assert set(config.fix.session_participants) == {
        "FIX.4.4:MOCKX_DEMO->liquidity_bot",
        "FIX.4.4:MOCKX_DEMO->taker_bot",
    }


def test_ladder_is_exact_symmetric_and_non_crossing():
    config = load_config(
        ROOT / "examples" / "live_exchange.yml",
        environ={},
    )
    instrument = Instrument.from_config(
        "BTC-USDT",
        config.instruments["BTC-USDT"],
    )
    midpoint = instrument.price_to_ticks("100.00")

    ladder = build_ladder(midpoint, levels=8)
    bids = [level for level in ladder if level.side == "BUY"]
    asks = [level for level in ladder if level.side == "SELL"]

    assert len(bids) == len(asks) == 8
    assert len(set(ladder)) == 16
    assert instrument.format_price(bids[0].price_ticks) == "99.95"
    assert instrument.format_price(asks[0].price_ticks) == "100.05"
    assert max(level.price_ticks for level in bids) < min(
        level.price_ticks for level in asks
    )


def test_midpoint_random_walk_is_seeded_and_bounded():
    first = random.Random(17)
    second = random.Random(17)

    def values(rng):
        current = 10_000
        result = []
        for _ in range(500):
            current = next_midpoint(
                current,
                initial_ticks=10_000,
                band_ticks=20,
                rng=rng,
            )
            result.append(current)
        return result

    first_values = values(first)
    assert first_values == values(second)
    assert min(first_values) >= 9_980
    assert max(first_values) <= 10_020


def test_quote_state_tracks_partial_fill_and_cancel_terminal_outcome():
    config = load_config(
        ROOT / "examples" / "live_exchange.yml",
        environ={},
    )
    instrument = Instrument.from_config(
        "BTC-USDT",
        config.instruments["BTC-USDT"],
    )
    state = LiveMarketState(instrument)
    quote = LiveQuote(
        cl_ord_id="MM-00000001",
        level=PlannedLevel("BUY", 9_995),
        quantity_lots=1_000,
        leaves_qty_lots=1_000,
    )
    state.register_quote(quote)
    state.record_report(
        FixReport(
            message_type="ExecutionReport",
            sender_comp_id="liquidity_bot",
            cl_ord_id=quote.cl_ord_id,
            orig_cl_ord_id=None,
            status="PARTIAL",
            exec_type="1",
            leaves_qty="0.500",
            last_price="99.95",
            last_qty="0.500",
            text=None,
        )
    )

    updated = state.active_quotes()
    assert len(updated) == 1
    assert updated[0].status == "PARTIAL"
    assert updated[0].leaves_qty_lots == 500

    state.record_report(
        FixReport(
            message_type="ExecutionReport",
            sender_comp_id="liquidity_bot",
            cl_ord_id="CXL-00000002",
            orig_cl_ord_id=quote.cl_ord_id,
            status="CANCELLED",
            exec_type="4",
            leaves_qty="0.000",
            last_price=None,
            last_qty=None,
            text="Order cancelled by request",
        )
    )

    assert not state.active_quotes()
    assert state.status(quote.cl_ord_id) == "CANCELLED"
    assert state.status("CXL-00000002") == "CANCELLED"


def test_live_market_help_is_available():
    result = subprocess.run(
        [sys.executable, "examples/live_market.py", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "--cycles" in result.stdout
    assert "--fix-config" in result.stdout
    assert "--base-url" in result.stdout
    assert "--dashboard-url" in result.stdout


def test_quickfix_helper_uses_native_utc_timestamp_constructor():
    if fix is None:
        return

    assert _transact_time().getString()


def test_termination_handler_interrupts_the_driver():
    with pytest.raises(KeyboardInterrupt):
        live_module._termination_requested(signal.SIGTERM, None)


def test_main_installs_and_restores_sigterm_handler(monkeypatch):
    calls = []
    previous_handler = object()

    def fake_signal(signal_number, handler):
        calls.append((signal_number, handler))
        return previous_handler

    monkeypatch.setattr(live_module.signal, "signal", fake_signal)
    monkeypatch.setattr(live_module, "run", lambda argv: 7)

    assert live_module.main(["ignored"]) == 7
    assert calls == [
        (signal.SIGTERM, live_module._termination_requested),
        (signal.SIGTERM, previous_handler),
    ]


def _fix_report(
    cl_ord_id: str,
    status: str,
    *,
    orig_cl_ord_id: str | None = None,
    leaves_qty: str | None = None,
) -> FixReport:
    return FixReport(
        message_type="ExecutionReport",
        sender_comp_id="liquidity_bot",
        cl_ord_id=cl_ord_id,
        orig_cl_ord_id=orig_cl_ord_id,
        status=status,
        exec_type="0",
        leaves_qty=leaves_qty,
        last_price=None,
        last_qty=None,
        text=None,
    )


def test_live_market_reconciles_quotes_and_uses_distinct_fix_sessions(
    monkeypatch,
):
    config = load_config(
        ROOT / "examples" / "live_exchange.yml",
        environ={},
    )
    instrument = Instrument.from_config(
        "BTC-USDT",
        config.instruments["BTC-USDT"],
    )
    state = LiveMarketState(instrument)
    pending = []
    sent_limits = []
    sent_cancels = []
    sent_markets = []

    class FakeApplication:
        @staticmethod
        def session(role):
            return f"session:{role}"

    class FakeAdmin:
        advances = 0

        def advance(self, milliseconds):
            assert milliseconds == 25
            self.advances += 1
            callbacks = tuple(pending)
            pending.clear()
            for callback in callbacks:
                callback()

    def send_limit(session_id, **fields):
        sent_limits.append((session_id, fields))
        pending.append(
            lambda: state.record_report(
                _fix_report(
                    fields["cl_ord_id"],
                    "NEW",
                    leaves_qty=fields["quantity"],
                )
            )
        )

    def send_cancel(session_id, **fields):
        sent_cancels.append((session_id, fields))
        pending.append(
            lambda: state.record_report(
                _fix_report(
                    fields["cl_ord_id"],
                    "CANCELLED",
                    orig_cl_ord_id=fields["orig_cl_ord_id"],
                    leaves_qty="0.000",
                )
            )
        )

    def send_market(session_id, **fields):
        sent_markets.append((session_id, fields))
        pending.append(
            lambda: state.record_report(
                _fix_report(fields["cl_ord_id"], "FILLED")
            )
        )

    monkeypatch.setattr(live_module, "send_limit_order", send_limit)
    monkeypatch.setattr(live_module, "send_cancel", send_cancel)
    monkeypatch.setattr(live_module, "send_market_order", send_market)
    monkeypatch.setattr(
        live_module.fix,
        "Side_BUY",
        "1",
        raising=False,
    )
    monkeypatch.setattr(
        live_module.fix,
        "Side_SELL",
        "2",
        raising=False,
    )

    admin = FakeAdmin()
    market = LiveMarket(
        app=FakeApplication(),
        state=state,
        admin=admin,
        instrument=instrument,
        id_prefix="TEST",
        levels=8,
        quote_quantity_lots=1_000,
        advance_ms=25,
        timeout=1,
        rng=random.Random(42),
    )

    market.reconcile(10_000)
    assert all(
        fields["cl_ord_id"].startswith("TEST-MM-")
        for _, fields in sent_limits
    )
    assert len(sent_limits) == 16
    assert not sent_cancels
    assert len(state.active_quotes()) == 16
    assert {session for session, _ in sent_limits} == {
        "session:liquidity_bot"
    }
    assert {fields["quantity"] for _, fields in sent_limits} == {
        "1.000"
    }
    assert all(
        instrument.price_to_ticks(fields["price"])
        == quote.level.price_ticks
        for (_, fields), quote in zip(
            sent_limits,
            state.active_quotes(),
        )
    )

    market.reconcile(10_001)
    assert len(sent_cancels) == 16
    assert len(sent_limits) == 32
    assert len(state.active_quotes()) == 16
    assert {session for session, _ in sent_cancels} == {
        "session:liquidity_bot"
    }

    market.take_liquidity()
    assert len(sent_markets) == 1
    assert sent_markets[0][0] == "session:taker_bot"
    assert admin.advances == 4


def _install_run_fakes(monkeypatch, market_action="ok"):
    events = []

    class FakeApplication:
        def __init__(self, *, print_reports):
            events.append(("application", print_reports))

        def add_listener(self, listener):
            events.append("listener")

        def wait_for_sessions(self, roles, timeout):
            events.append(("logons", tuple(roles), timeout))
            return True

    class FakeAdmin:
        instances = []

        def __init__(self, base_url, timeout):
            self.base_url = base_url
            self.timeout = timeout
            self.ended = []
            self.closed = False
            self.__class__.instances.append(self)

        def wait_ready(self, timeout):
            events.append(("ready", timeout))

        def start_session(self, run_id):
            events.append(("start", run_id))
            if market_action == "active_run":
                raise RuntimeError("simulation session already active")
            return {
                "session_id": "00000000-0000-4000-8000-000000000001",
                "run_generation": 1,
            }

        def end_session_if_owned(self, run_id):
            self.ended.append(run_id)
            events.append(("end", run_id))
            return True

        def book(self, symbol):
            return {
                "bids": [{"price": "99.95"}],
                "asks": [{"price": "100.05"}],
            }

        def close(self):
            self.closed = True
            events.append("admin_close")

    class FakeInitiator:
        instances = []

        def __init__(self, app, store, settings, log):
            self.started = False
            self.stopped = False
            self.__class__.instances.append(self)

        def start(self):
            self.started = True
            events.append("initiator_start")

        def stop(self):
            self.stopped = True
            events.append("initiator_stop")

    class FakeMarket:
        reconciliations = 0
        takes = 0

        def __init__(self, **kwargs):
            events.append("market")

        def reconcile(self, midpoint):
            self.__class__.reconciliations += 1
            events.append(("reconcile", midpoint))
            if market_action == "interrupt":
                raise KeyboardInterrupt
            if market_action == "error":
                raise RuntimeError("market failure")

        def take_liquidity(self):
            self.__class__.takes += 1
            return f"TAKER-{self.takes}"

    fake_fix = SimpleNamespace(
        ConfigError=type("FakeConfigError", (Exception,), {}),
        SessionSettings=lambda path: path,
        MemoryStoreFactory=lambda: object(),
        ScreenLogFactory=lambda settings: settings,
        SocketInitiator=FakeInitiator,
    )
    monkeypatch.setattr(live_module, "require_quickfix", lambda: None)
    monkeypatch.setattr(live_module, "fix", fake_fix)
    monkeypatch.setattr(
        live_module,
        "MultiSessionFIXApplication",
        FakeApplication,
    )
    monkeypatch.setattr(live_module, "DemoAdmin", FakeAdmin)
    monkeypatch.setattr(live_module, "LiveMarket", FakeMarket)
    return events, FakeAdmin, FakeInitiator, FakeMarket


def _run_fake_driver(*extra_args):
    return live_module.run(
        [
            "--config",
            str(ROOT / "examples" / "live_exchange.yml"),
            "--fix-config",
            "ignored.cfg",
            "--cycles",
            "2",
            "--interval",
            "0",
            *extra_args,
        ]
    )


def test_finite_run_waits_for_readiness_and_logons_then_cleans_up(
    monkeypatch,
):
    events, admins, initiators, markets = _install_run_fakes(monkeypatch)

    assert _run_fake_driver() == 0
    assert events.index(("ready", 15.0)) < events.index("initiator_start")
    assert events.index("initiator_start") < next(
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "logons"
    )
    assert next(
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "logons"
    ) < events.index(("start", "live-fix-demo"))
    assert markets.reconciliations == 2
    assert markets.takes == 2
    assert admins.instances[0].ended == ["00000000-0000-4000-8000-000000000001"]
    assert admins.instances[0].closed
    assert initiators.instances[0].stopped


def test_dashboard_url_changes_only_the_printed_browser_address(
    monkeypatch,
    capsys,
):
    events, admins, initiators, markets = _install_run_fakes(monkeypatch)

    assert (
        _run_fake_driver(
            "--dashboard-url",
            "http://localhost:3000/d/mockx-native",
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Open http://localhost:3000/d/mockx-native" in output
    assert admins.instances[0].base_url == "http://127.0.0.1:8001"
    assert markets.reconciliations == 2
    assert initiators.instances[0].stopped


def test_active_run_is_not_replaced_or_ended(monkeypatch):
    _, admins, initiators, markets = _install_run_fakes(
        monkeypatch,
        "active_run",
    )

    assert _run_fake_driver() == 1
    assert markets.reconciliations == 0
    assert admins.instances[0].ended == []
    assert admins.instances[0].closed
    assert initiators.instances[0].stopped


def test_driver_ends_owned_session_on_error_and_ctrl_c(monkeypatch):
    _, admins, initiators, _ = _install_run_fakes(
        monkeypatch,
        "error",
    )
    assert _run_fake_driver() == 1
    assert admins.instances[0].ended == ["00000000-0000-4000-8000-000000000001"]
    assert initiators.instances[0].stopped

    monkeypatch.undo()
    _, admins, initiators, _ = _install_run_fakes(
        monkeypatch,
        "interrupt",
    )
    assert _run_fake_driver() == 130
    assert admins.instances[0].ended == ["00000000-0000-4000-8000-000000000001"]
    assert initiators.instances[0].stopped

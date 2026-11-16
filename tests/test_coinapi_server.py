"""The owned child uses frozen configuration and survives diagnostic shutdowns."""

from io import StringIO
from contextlib import contextmanager
import threading
from types import SimpleNamespace

import pytest

from examples.coinapi_replay import server


def test_server_uses_generated_configuration_without_environment_overrides(monkeypatch):
    calls = []
    timer_calls = []

    @contextmanager
    def timer_resolution():
        timer_calls.append("enter")
        try:
            yield {}
        finally:
            timer_calls.append("exit")

    monkeypatch.setattr(server, "replay_timer_resolution", timer_resolution)
    monkeypatch.setattr(
        server.sys, "argv", ["server", "--config", "run.yml", "--fix-config", "fix.cfg"]
    )

    def load(path, *, environ):
        calls.append((path, environ))
        return "configuration"

    monkeypatch.setattr(server, "load_config", load)

    def run(config, fix):
        assert timer_calls == ["enter"]
        calls.append((config, fix))

    monkeypatch.setattr(
        server,
        "MockExchange",
        lambda config, fix: SimpleNamespace(run=lambda: run(config, fix)),
    )
    monkeypatch.setattr(
        server.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None)
    )
    server.main()
    assert calls == [("run.yml", {}), ("configuration", "fix.cfg")]
    assert timer_calls == ["enter", "exit"]


@pytest.mark.parametrize("command", ["", "stop\n"], ids=["parent-eof", "explicit-stop"])
def test_parent_shutdown_waits_for_server_initialization(command):
    called, finished = threading.Event(), threading.Event()
    exchange = SimpleNamespace(server=None, stop=called.set)
    worker = threading.Thread(
        target=server.watch_parent, args=(exchange, StringIO(command), finished)
    )
    worker.start()
    try:
        assert not called.wait(0.02)
        exchange.server = object()
        assert called.wait(1)
        worker.join(1)
        assert not worker.is_alive()
    finally:
        finished.set()
        worker.join(1)


def test_parent_shutdown_monitor_exits_when_initialization_never_completes():
    finished = threading.Event()
    finished.set()
    exchange = SimpleNamespace(
        server=None, stop=lambda: pytest.fail("server was never initialized")
    )
    server.watch_parent(exchange, StringIO(""), finished)

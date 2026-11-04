"""Startup must verify that the HTTP endpoint belongs to the owned child."""

from contextlib import contextmanager
from io import BytesIO
import json
from types import SimpleNamespace

import httpx
import pytest
import yaml

from examples.coinapi_replay import __main__ as launcher


@pytest.mark.asyncio
async def test_ready_endpoint_from_another_exchange_is_rejected(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request.url.path)
        body = (
            {"exchange": "someone-else"}
            if request.url.path == "/"
            else {"status": "ready"}
        )
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(launcher.httpx, "AsyncClient", lambda **kwargs: client)
    process = SimpleNamespace(poll=lambda: None, returncode=None)
    with pytest.raises(RuntimeError, match="different exchange"):
        await launcher.wait_ready(
            "http://test", process, 1, expected_exchange="owned-run"
        )
    assert "/" in requests


@pytest.mark.asyncio
async def test_child_exit_after_http_ready_does_not_pass_readiness(monkeypatch):
    client = httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"exchange": "owned-run"})
        ),
    )
    monkeypatch.setattr(launcher.httpx, "AsyncClient", lambda **kwargs: client)
    polls = iter([None, 7])
    process = SimpleNamespace(poll=lambda: next(polls), returncode=7)
    with pytest.raises(RuntimeError, match="exited"):
        await launcher.wait_ready(
            "http://test", process, 1, expected_exchange="owned-run"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_close_error", [False, True])
async def test_launch_records_complete_startup_and_unique_child_identity(
    tmp_path, monkeypatch, audit_close_error
):
    source = tmp_path / "input.csv"
    source.write_text("placeholder input")
    output = tmp_path / "run"
    args = launcher.parse_args(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--no-controller",
            "--native-client",
            str(tmp_path / "client.exe"),
        ]
    )
    if audit_close_error:
        args.audit = True
        original_open = launcher.Path.open

        def failed_audit_close():
            raise OSError("audit disk full")

        def open_file(path, *args, **kwargs):
            if path.name == "events.jsonl":
                return SimpleNamespace(close=failed_audit_close)
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(launcher.Path, "open", open_file)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(launcher, "time", SimpleNamespace(monotonic=lambda: clock.now))
    processes = []
    timer_calls = []

    @contextmanager
    def timer_resolution():
        timer_calls.append("enter")
        try:
            yield {"resolution_request_ms": 1, "resolution_request_status": "accepted"}
        finally:
            timer_calls.append("exit")

    monkeypatch.setattr(
        launcher, "replay_timer_resolution", timer_resolution, raising=False
    )

    class Process:
        def __init__(self, command, **kwargs):
            self.command, self.stdin, self.returncode = command, BytesIO(), None
            self.pid = 12345
            processes.append(self)

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    monkeypatch.setattr(launcher.subprocess, "Popen", Process)

    @contextmanager
    def source_context(*args, **kwargs):
        yield object()

    monkeypatch.setattr(launcher, "open_source", source_context)

    async def ready(url, process, timeout, *, expected_exchange):
        assert (
            yaml.safe_load((output / "exchange.yml").read_text())["exchange_name"]
            == expected_exchange
        )
        assert expected_exchange.startswith("MockX-CoinAPI-coinapi-")
        clock.now = 107.0

    monkeypatch.setattr(launcher, "wait_ready", ready)
    probes = []

    def native_probe(config, instrument, *, executable, timeout):
        probes.append((config, instrument, executable, timeout))
        return "native-probe"

    monkeypatch.setattr(launcher, "NativeProbe", native_probe, raising=False)

    class Runner:
        def __init__(self, *args, native_probe=None, memory_samplers=None, **kwargs):
            assert native_probe == "native-probe"
            assert set(memory_samplers) == {"exchange", "runner"}
            assert memory_samplers["exchange"].pid == 12345
            self.result = {}

        async def run(self):
            assert timer_calls == ["enter"]
            clock.now = 120.0
            self.result = {"status": "completed", "first_book_seconds": 2.0}
            return self.result

    monkeypatch.setattr(launcher, "ReplayRunner", Runner)
    assert await launcher.launch(args) == (1 if audit_close_error else 0)
    result = json.loads((output / "result.json").read_text())
    assert result["command_to_first_book_seconds"] == 9.0
    assert result["child_ready_seconds"] == 7.0
    assert probes[0][2] == args.native_client
    assert processes[0].returncode == 0
    assert timer_calls == ["enter", "exit"]
    assert result["resolution_request_ms"] == 1
    assert result["resolution_request_status"] == "accepted"
    if audit_close_error:
        assert result["status"] == "failed"
        assert "audit disk full" in result["cleanup_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,explicit_symbol,starts",
    [
        ("coinbase.csv", None, True),
        ("coinbase.csv", "COINBASE_SPOT_BTC_USDT", True),
        ("capture_SC-COINBASE_SPOT_ETH_USDT_S-ETH.csv", None, False),
    ],
)
async def test_launcher_asserts_source_identity_before_starting_owned_child(
    tmp_path, monkeypatch, filename, explicit_symbol, starts
):
    source = tmp_path / filename
    source.write_text(
        "time_exchange;time_coinapi;update_type;is_buy;entry_px;entry_sx;order_id\n"
        "2026-07-10T00:00:00Z;2026-07-10T00:00:00Z;SNAPSHOT;1;99;1;b\n"
        "2026-07-10T00:00:00Z;2026-07-10T00:00:00Z;SNAPSHOT;0;101;1;a\n"
        "2026-07-10T00:00:01Z;2026-07-10T00:00:01Z;0;1;98;1;c\n"
    )
    output = tmp_path / "run"
    arguments = ["--input", str(source), "--output", str(output)]
    if explicit_symbol:
        arguments += ["--source-symbol", explicit_symbol]
    args = launcher.parse_args(arguments)
    attempted = []

    def start_child(*args, **kwargs):
        attempted.append(True)
        raise RuntimeError("child-start-marker")

    monkeypatch.setattr(launcher.subprocess, "Popen", start_child)
    assert await launcher.launch(args) == 1
    assert bool(attempted) is starts
    result = json.loads((output / "result.json").read_text())
    assert result["source_symbol"] == "COINBASE_SPOT_BTC_USDT"
    assert ("child-start-marker" in result["error"]) is starts


@pytest.mark.asyncio
async def test_custom_destination_symbol_requires_an_explicit_source_identity(tmp_path):
    args = launcher.parse_args(
        ["--input", str(tmp_path / "input.csv"), "--symbol", "ETH-USDT"]
    )
    with pytest.raises(ValueError, match="source-symbol"):
        await launcher.launch(args)

"""Native transport releases process-wide session ownership after shutdown."""

import asyncio
import gc
from types import SimpleNamespace
import weakref

import pytest

from examples.coinapi_replay.transport import NativeFixTransport, fix
from examples.coinapi_replay import transport as transport_module
from tests.exact_helpers import TEST_INSTRUMENT


class FakeInitiator:
    def __init__(self, calls, *, fail_stop=False):
        self.calls = calls
        self.fail_stop = fail_stop

    def start(self):
        self.calls.append("start")

    def stop(self, force):
        self.calls.append(("stop", force))
        if self.fail_stop:
            raise RuntimeError("native stop failed")


def transport_fixture(*, started=True, fail_stop=False, qualifier=""):
    calls = []
    transport = NativeFixTransport.__new__(NativeFixTransport)
    transport.started = started
    transport.initiator = FakeInitiator(calls, fail_stop=fail_stop)
    transport._loop = None
    transport._receive = lambda report: None
    transport._roles = ()
    transport._drain_waiters = {}
    field = lambda value: SimpleNamespace(getValue=lambda: value)
    session = SimpleNamespace(
        getSessionQualifier=lambda: qualifier,
        getBeginString=lambda: field("FIX.4.4"),
        getTargetCompID=lambda: field("MOCKX"),
        getSenderCompID=lambda: field("replay_buy"),
    )
    # The real application holds a bound-method listener back to its transport.
    # Shutdown must release native session ownership even while this cycle lives.
    transport.application = SimpleNamespace(
        listeners=[transport._on_report],
        wait_for_sessions=lambda roles, timeout: True,
        session=lambda role: session,
    )
    return transport, calls


class FakeField:
    def __init__(self, number, value=""):
        self.number, self.value = number, value

    def getValue(self):
        return self.value


class FakeMessage:
    def __init__(self):
        self.fields = {}

    def getHeader(self):
        return self

    def setField(self, field):
        self.fields[field.number] = field.value

    def isSetField(self, number):
        return number in self.fields

    def getField(self, field):
        field.value = self.fields[field.number]


def fake_fix(send):
    return SimpleNamespace(Message=FakeMessage,
                           MsgType=lambda value="": FakeField(35, value),
                           TestReqID=lambda value="": FakeField(112, value),
                           Session=SimpleNamespace(sendToTarget=send))


def test_admin_callback_forwards_only_heartbeat_with_test_request_id(monkeypatch):
    monkeypatch.setattr(transport_module, "fix", fake_fix(lambda *_: True))
    heartbeats = []
    application = transport_module._ReplayFIXApplication(
        lambda role, token: heartbeats.append((role, token)),
    )
    session = SimpleNamespace(getSenderCompID=lambda: FakeField(49, "replay_buy"))
    message = FakeMessage()
    message.setField(FakeField(35, "0"))
    application.fromAdmin(message, session)
    assert not heartbeats
    message.setField(FakeField(112, "barrier-token"))
    application.fromAdmin(message, session)
    assert heartbeats == [("replay_buy", "barrier-token")]
    message.setField(FakeField(35, "1"))
    application.fromAdmin(message, session)
    assert len(heartbeats) == 1


@pytest.mark.asyncio
async def test_drain_waits_for_every_matching_heartbeat_after_report_callbacks(monkeypatch):
    transport, _ = transport_fixture()
    transport._loop = asyncio.get_running_loop()
    transport._roles = ("replay_buy", "replay_sell")
    transport.application.session = lambda role: role
    received, sent = [], {}
    transport._receive = received.append

    def send(message, role):
        assert message.fields[35] == "1"
        sent[role] = message.fields[112]
        return True

    monkeypatch.setattr(transport_module, "fix", fake_fix(send))
    task = asyncio.create_task(transport.drain(0.2))
    await asyncio.sleep(0)
    assert set(sent) == set(transport._roles)
    assert len(set(sent.values())) == 2
    transport._on_report("buy report")
    transport._on_heartbeat("replay_buy", sent["replay_buy"])
    transport._on_heartbeat("replay_sell", "wrong-token")
    await asyncio.sleep(0)
    assert not task.done()
    transport._on_report("sell report")
    transport._on_heartbeat("replay_sell", sent["replay_sell"])
    await task
    assert received == ["buy report", "sell report"]
    assert not transport._drain_waiters
    await transport.close()


@pytest.mark.asyncio
async def test_drain_timeout_clears_barriers_and_allows_a_later_attempt(monkeypatch):
    transport, _ = transport_fixture()
    transport._loop = asyncio.get_running_loop()
    transport._roles = ("replay_buy",)
    transport.application.session = lambda role: role
    sent = []

    def send(message, role):
        sent.append((role, message.fields[112]))
        return True

    monkeypatch.setattr(transport_module, "fix", fake_fix(send))
    with pytest.raises(TimeoutError):
        await transport.drain(0.001)
    assert not transport._drain_waiters
    retry = asyncio.create_task(transport.drain(0.2))
    await asyncio.sleep(0)
    assert sent[0][1] != sent[1][1]
    transport._on_heartbeat(*sent[0])
    await asyncio.sleep(0)
    assert not retry.done()
    transport._on_heartbeat(*sent[1])
    await retry
    await transport.close()


@pytest.mark.asyncio
async def test_drain_send_failure_is_visible_and_releases_pending_barriers(monkeypatch):
    transport, _ = transport_fixture()
    transport._loop = asyncio.get_running_loop()
    transport._roles = ("replay_buy",)
    monkeypatch.setattr(transport_module, "fix", fake_fix(lambda message, session: False))
    with pytest.raises(RuntimeError, match="TestRequest"):
        await transport.drain(0.1)
    assert not transport._drain_waiters
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_close_releases_initiator_despite_retained_listener_cycle(started):
    transport, calls = transport_fixture(started=started)
    reference = weakref.ref(transport.initiator)
    await transport.close()
    assert transport.initiator is None
    assert not transport.started and transport._receive is None
    # The thread-pool worker may still be discarding its completed work item.
    for _ in range(50):
        gc.collect()
        if reference() is None:
            break
        await asyncio.sleep(0.001)
    assert reference() is None
    await transport.close()
    assert calls == ([("stop", True)] if started else [])
    assert transport.application.listeners  # The transport itself is still retained.


@pytest.mark.asyncio
async def test_close_releases_references_even_when_native_stop_raises():
    transport, calls = transport_fixture(fail_stop=True)
    with pytest.raises(RuntimeError, match="native stop failed"):
        await transport.close()
    assert transport.initiator is None
    assert transport._receive is None
    assert not transport.started
    await transport.close()
    assert calls == [("stop", True)]


@pytest.mark.asyncio
async def test_start_rejects_qualified_sessions_and_close_still_releases_initiator():
    transport, calls = transport_fixture(started=False, qualifier="QUALIFIED")
    try:
        with pytest.raises(ValueError, match="unqualified FIX sessions"):
            await transport.start(lambda report: None, ["replay_buy"], 0.1)
    finally:
        await transport.close()
    assert calls == ["start", ("stop", True)]
    assert transport.initiator is None


@pytest.mark.asyncio
async def test_start_returns_acceptor_orientation_for_seed_ownership():
    transport, _ = transport_fixture(started=False)
    try:
        assert await transport.start(lambda report: None, ["replay_buy"], 0.1) == {
            "replay_buy": "FIX.4.4:MOCKX->replay_buy",
        }
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_native_session_ids_can_be_recreated_while_closed_transport_is_retained(tmp_path):
    if fix is None:
        pytest.skip("Native QuickFIX is required for the session registry regression")
    settings = tmp_path / "initiator.cfg"
    settings.write_text(
        "[DEFAULT]\nConnectionType=initiator\nStartTime=00:00:00\nEndTime=00:00:00\n"
        "HeartBtInt=30\nSocketConnectHost=127.0.0.1\nSocketConnectPort=9\n"
        f"FileStorePath={tmp_path / 'store'}\nFileLogPath={tmp_path / 'log'}\n"
        "UseDataDictionary=N\n[SESSION]\nBeginString=FIX.4.4\n"
        "SenderCompID=REPLAY_TRANSPORT_REGISTRY\nTargetCompID=REPLAY_TRANSPORT_VENUE\n",
        encoding="utf-8",
    )
    first = NativeFixTransport(settings, TEST_INSTRUMENT)
    second = None
    try:
        await first.close()
        # Keep `first` and its application/listener cycle alive deliberately.
        second = NativeFixTransport(settings, TEST_INSTRUMENT)
        assert second.initiator is not None
    finally:
        await first.close()
        if second is not None:
            await second.close()


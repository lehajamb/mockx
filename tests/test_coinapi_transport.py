from examples.coinapi_replay.transport import ReplayFIXApplication

import pytest
import threading
from types import SimpleNamespace


def test_callback_overflow_is_explicit_and_bounded():
    app = ReplayFIXApplication(capacity=2)
    app._receive("one")
    app._receive("two")
    app._receive("three")
    assert len(app.reports) == 2
    with pytest.raises(RuntimeError, match="overflow"):
        app.take_reports()


def test_callbacks_are_consumed_once_in_order():
    app = ReplayFIXApplication()
    app._receive("one")
    app._receive("two")
    assert app.take_reports() == ("one", "two")
    assert app.take_reports() == ()


class ProtocolMessage:
    def __init__(self, message_type, **fields):
        self.fields = {int(tag): value for tag, value in fields.items()}
        self.fields[35] = message_type

    def getHeader(self):
        return self

    def getField(self, tag):
        return self.fields[tag]

    def isSetField(self, tag):
        return tag in self.fields


@pytest.mark.parametrize(
    "message_type,callback", [("3", "fromAdmin"), ("j", "fromApp")]
)
def test_protocol_rejection_fails_immediately_with_its_reason(message_type, callback):
    app = ReplayFIXApplication()
    wake = threading.Event()
    app._fences["waiting"] = wake
    message = ProtocolMessage(
        message_type, **{"45": "17", "372": "D", "58": "Unsupported symbol"}
    )
    getattr(app, callback)(message, None)
    with pytest.raises(RuntimeError, match="17.*Unsupported symbol"):
        app.take_reports()
    assert wake.is_set()


def test_rejection_reason_is_bounded_and_not_hidden_by_logout():
    app = ReplayFIXApplication()
    app.fromAdmin(ProtocolMessage("3", **{"58": "bad field " * 1000}), None)
    reason = app.error
    assert reason and len(reason) <= 1024
    session = SimpleNamespace(
        getSenderCompID=lambda: SimpleNamespace(getValue=lambda: "replay_buy")
    )
    app.connected.add("replay_buy")
    app.onLogout(session)
    assert app.error == reason


def test_logon_wait_surfaces_protocol_error_without_timeout():
    app = ReplayFIXApplication()
    app.fromAdmin(ProtocolMessage("5", **{"58": "Authentication refused"}), None)
    with pytest.raises(RuntimeError, match="Authentication refused"):
        app.wait_for_sessions(["replay_buy"], 0)

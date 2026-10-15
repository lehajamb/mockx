"""Wire contracts of runnable native batch and replace example helpers."""

import subprocess
import sys
from types import SimpleNamespace

import pytest
import quickfix as fix
import quickfix44 as fix44
import httpx

from examples.fix_support import ListOrder, build_order_list, build_replace
from .api_helpers import ROOT


def field(message, tag):
    value = fix.StringField(tag)
    message.getField(value)
    return value.getValue()


def test_list_builder_keeps_decimal_text_and_dictionary_group_order():
    orders = (
        ListOrder("FIRST", "TEST", "1", "9007199254740993.001", "12345.0001"),
        ListOrder("SECOND", "TEST", "2", "0.000000000000000001", tif="3"),
    )
    message = build_order_list("L1", orders)
    assert field(message.getHeader(), 35) == "E"
    assert {tag: field(message, tag) for tag in (66, 394, 433, 414, 68, 73)} == {
        66: "L1", 394: "3", 433: "1", 414: "3", 68: "2", 73: "2"
    }
    first = fix44.NewOrderList.NoOrders()
    second = fix44.NewOrderList.NoOrders()
    message.getGroup(1, first)
    message.getGroup(2, second)
    assert field(first, 11) == "FIRST"
    assert field(first, 67) == "1"
    assert field(first, 38) == "9007199254740993.001"
    assert field(first, 44) == "12345.0001"
    assert field(second, 67) == "2"
    assert field(second, 40) == "1"
    assert field(second, 59) == "3"
    assert not second.isSetField(44)
    wire = message.toString()
    assert wire.index("11=FIRST") < wire.index("67=1") < wire.index("55=TEST")


def test_empty_list_cannot_be_sent():
    with pytest.raises(ValueError, match="at least one"):
        build_order_list("EMPTY", ())


def test_replace_builder_uses_total_quantity_and_list_identity():
    message = build_replace(
        cl_ord_id="NEXT", orig_cl_ord_id="CURRENT", symbol="TEST", side="1",
        quantity="7.000", price="99.99", list_id="L1",
    )
    assert field(message.getHeader(), 35) == "G"
    assert {tag: field(message, tag) for tag in (11, 41, 38, 40, 44, 59, 66)} == {
        11: "NEXT", 41: "CURRENT", 38: "7.000", 40: "2", 44: "99.99", 59: "1", 66: "L1"
    }


def test_batch_example_help_documents_both_paths():
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "batch_orders.py"), "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "list,pipeline" in result.stdout
    assert "--batch-size" in result.stdout


def test_batch_failure_cleans_up_fix_even_if_admin_cleanup_is_unavailable(monkeypatch, capsys):
    from examples import batch_orders
    operations = []

    class Initiator:
        def start(self):
            operations.append("start")
        def stop(self):
            operations.append("stop")

    class Application:
        def __init__(self, **kwargs):
            pass
        def add_listener(self, listener):
            pass
        def wait_for_sessions(self, *args):
            return True
        def session(self, role):
            return object()

    class Admin:
        def __init__(self, *args):
            pass
        def wait_ready(self, timeout):
            pass
        def start_session(self, label):
            return {"session_id": "owned-run", "run_generation": 1}
        def end_session_if_owned(self, run_id):
            assert run_id == "owned-run"
            raise httpx.ConnectError("admin unavailable")
        def close(self):
            operations.append("close")

    monkeypatch.setattr(batch_orders, "fix", SimpleNamespace(
        SessionSettings=lambda path: object(), SocketInitiator=lambda *args: Initiator(),
        MemoryStoreFactory=lambda: object(), ScreenLogFactory=lambda settings: object(),
        ConfigError=RuntimeError,
    ))
    monkeypatch.setattr(batch_orders, "MultiSessionFIXApplication", Application)
    monkeypatch.setattr(batch_orders, "DemoAdmin", Admin)
    def failed_submission(*args, **kwargs):
        raise RuntimeError("list submission failed")
    monkeypatch.setattr(batch_orders, "send_order_list", failed_submission)
    assert batch_orders.run([]) == 1
    assert operations == ["start", "stop", "close"]
    assert "list submission failed" in capsys.readouterr().err


def test_batch_example_reports_missing_quickfix_without_secondary_exception(monkeypatch, capsys):
    from examples import batch_orders
    monkeypatch.setattr(batch_orders, "fix", None)
    def unavailable():
        raise RuntimeError("QuickFIX is required")
    monkeypatch.setattr(batch_orders, "require_quickfix", unavailable)
    assert batch_orders.run([]) == 1
    assert "QuickFIX is required" in capsys.readouterr().err

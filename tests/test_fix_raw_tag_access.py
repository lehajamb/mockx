"""Keep exact FIX wire contracts while avoiding redundant SWIG field objects."""

import pytest

from examples import fix_support
from src.mockx.fix_fields import cancel_reject_fields, execution_report_fields
from src.mockx.gateway import fix
from src.mockx.models import Side
from tests.lifecycle_helpers import cancel, make_engine, submit
from tests.test_fix_ingress_pipeline import _exchange, _new_order, _session, _start


class FieldAccessProbe:
    def __init__(self, message, accesses):
        self.message = message
        self.accesses = accesses

    def getHeader(self):
        return FieldAccessProbe(self.message.getHeader(), self.accesses)

    def getField(self, field):
        self.accesses.append(("get", field))
        return self.message.getField(field)

    def setField(self, *args):
        self.accesses.append(("set", args[0]))
        return self.message.setField(*args)

    def __getattr__(self, name):
        return getattr(self.message, name)


TAGS = {
    "ClOrdID": 11,
    "ExecType": 150,
    "OrdStatus": 39,
    "ExecID": 17,
    "OrderID": 37,
    "Symbol": 55,
    "Side": 54,
    "LeavesQty": 151,
    "CumQty": 14,
    "AvgPx": 6,
    "OrderQty": 38,
    "OrdType": 40,
    "TimeInForce": 59,
    "OrigClOrdID": 41,
    "Price": 44,
    "LastPx": 31,
    "LastQty": 32,
    "TradeID": 1003,
    "Text": 58,
    "OrdRejReason": 103,
    "CxlRejResponseTo": 434,
    "CxlRejReason": 102,
}


def reference_wire(message_type, fields, factory):
    message = factory()
    message.getHeader().setField(fix.MsgType(message_type))
    for name, value in fields.items():
        if name in TAGS:
            message.setField(fix.StringField(TAGS[name], str(value)))
    return message.toString()


def test_execution_and_cancel_reject_wires_match_typed_fields_without_wrappers(
    monkeypatch,
):
    engine, reports, _, _ = make_engine()
    passive = submit(
        engine,
        cl_ord_id="PASSIVE",
        participant_id="P",
        side=Side.SELL,
        qty=5,
        price=100,
    )
    submit(
        engine, cl_ord_id="TAKER", participant_id="T", side=Side.BUY, qty=2, price=None
    )
    cancel(engine, passive)
    unknown = cancel(engine, 987)
    exchange = _exchange()
    factory = fix.Message
    accesses = []

    def copy_message(*args):
        native_args = tuple(
            arg.message if isinstance(arg, FieldAccessProbe) else arg for arg in args
        )
        return FieldAccessProbe(factory(*native_args), accesses)

    monkeypatch.setattr(fix, "Message", copy_message)
    for report in reports:
        fields = execution_report_fields(report)
        message = exchange.fix_app._send_quickfix_execution_report(report)
        assert message.toString() == reference_wire("8", fields, factory)
    message = exchange.fix_app._send_quickfix_cancel_reject(unknown)
    assert message.toString() == reference_wire(
        "9", cancel_reject_fields(unknown), factory
    )
    assert all(
        isinstance(field, int) for operation, field in accesses if operation == "set"
    )


@pytest.mark.asyncio
async def test_ingress_reads_exact_strings_directly_and_keeps_native_converters():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    accesses = []
    try:
        message = FieldAccessProbe(_new_order("RAW-ID", 1), accesses)
        exchange.fix_app.fromApp(message, sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert delivered[0].cl_ord_id == "RAW-ID"
        assert all(
            isinstance(field, int) or field.getField() in {34, 40, 54, 59}
            for operation, field in accesses
            if operation == "get"
        )
    finally:
        await exchange.stop_components()


@pytest.mark.parametrize("raw", ["", "12", " 1", "1 "])
@pytest.mark.parametrize("tag", [39, 150])
def test_report_char_fields_keep_quickfix_format_validation(tag, raw):
    message = fix.Message()
    message.getHeader().setField(35, "8")
    message.setField(11, "CHAR")
    message.setField(39, "0")
    message.setField(150, "0")
    message.setField(tag, raw)
    with pytest.raises(fix.IncorrectDataFormat):
        fix_support.MultiSessionFIXApplication()._parse_report(message, _session())


def test_example_report_projection_reads_string_tags_directly(monkeypatch):
    message = fix.Message()
    message.getHeader().setField(35, "8")
    for tag, value in {
        11: "ID",
        39: "1",
        150: "F",
        151: "0.00000001",
        31: "63211.12345678",
        32: "0.00000002",
        14: "0.00000002",
        38: "0.00000003",
        17: "EXEC",
        37: "ORDER",
        1003: "TRADE",
    }.items():
        message.setField(tag, value)
    application = fix_support.MultiSessionFIXApplication()
    monkeypatch.setattr(fix_support.time, "monotonic_ns", lambda: 123)
    expected = application._parse_report(message, _session())
    accesses = []
    actual = application._parse_report(FieldAccessProbe(message, accesses), _session())
    assert actual == expected
    assert actual.last_price == "63211.12345678"
    assert actual.leaves_qty == "0.00000001"
    assert all(
        isinstance(field, int) or field.getField() in {39, 150}
        for operation, field in accesses
        if operation == "get"
    )


@pytest.mark.parametrize("kind", ["limit", "market", "cancel", "replace"])
def test_example_requests_keep_wire_bytes_with_direct_string_tags(monkeypatch, kind):
    sent = []
    monkeypatch.setattr(
        fix_support, "_send", lambda message, session: sent.append(message)
    )
    monkeypatch.setattr(
        fix_support,
        "_transact_time",
        lambda: fix.StringField(60, "20260710-00:00:00.000"),
    )
    kwargs = dict(cl_ord_id="ID", symbol="TEST", side="1", quantity="0.00000003")
    if kind in {"limit", "replace"}:
        kwargs["price"] = "63211.12345678"
    if kind in {"cancel", "replace"}:
        kwargs["orig_cl_ord_id"] = "OLD"
    send = getattr(
        fix_support, "send_" + kind + ("_order" if kind in {"limit", "market"} else "")
    )
    factory = fix.Message
    fields = {
        11: "ID",
        55: "TEST",
        54: "1",
        38: "0.00000003",
        60: "20260710-00:00:00.000",
    }
    if kind in {"limit", "replace"}:
        fields.update({40: "2", 44: "63211.12345678", 59: "1"})
    elif kind == "market":
        fields.update({40: "1", 59: "3"})
    if kind in {"cancel", "replace"}:
        fields[41] = "OLD"
    expected = factory()
    expected.getHeader().setField(
        fix.MsgType({"limit": "D", "market": "D", "cancel": "F", "replace": "G"}[kind])
    )
    for tag, value in fields.items():
        expected.setField(fix.StringField(tag, value))
    accesses = []
    monkeypatch.setattr(fix, "Message", lambda: FieldAccessProbe(factory(), accesses))
    send(_session(), **kwargs)
    assert sent[0].toString() == expected.toString()
    assert all(
        isinstance(field, int) or field.getField() in {54, 59, 60}
        for operation, field in accesses
        if operation == "set"
    )

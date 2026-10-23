"""Replay clients must retain exact execution identity and cumulative fills."""

import pytest

from examples.fix_support import MultiSessionFIXApplication, fix


def test_replay_report_keeps_identity_and_cumulative_quantities():
    if fix is None:
        pytest.skip("Native QuickFIX required")
    message = fix.Message()
    message.getHeader().setField(fix.MsgType("8"))
    for tag, value in {
        11: "amended", 41: "seed", 37: "G00000001-O00000001",
        17: "G00000001-E00000002", 150: "F", 39: "1", 38: "0.010",
        14: "0.004", 151: "0.006", 31: "101.00", 32: "0.001",
        1003: "G00000001-T00000001",
    }.items():
        message.setField(fix.StringField(tag, value))
    session = fix.SessionID("FIX.4.4", "replay_buy", "MOCKX")
    report = MultiSessionFIXApplication()._parse_report(message, session)
    assert report.exec_id == "G00000001-E00000002"
    assert report.order_id == "G00000001-O00000001"
    assert report.cum_qty == "0.004"
    assert report.quantity == "0.010"
    assert report.trade_id == "G00000001-T00000001"
    assert report.received_ns > 0


def test_execution_price_and_size_never_round_trip_through_float():
    if fix is None:
        pytest.skip("Native QuickFIX required")
    message = fix.Message()
    message.getHeader().setField(fix.MsgType("8"))
    for tag, value in {11: "exact", 150: "F", 39: "2",
                       31: "1234567890.123456789", 32: "0.000000000123456789"}.items():
        message.setField(fix.StringField(tag, value))
    session = fix.SessionID("FIX.4.4", "replay_buy", "MOCKX")
    report = MultiSessionFIXApplication()._parse_report(message, session)
    assert report.last_price == "1234567890.123456789"
    assert report.last_qty == "0.000000000123456789"

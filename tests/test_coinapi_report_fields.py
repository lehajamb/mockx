"""Replay accounting must receive exact FIX quantities and logical identities."""

from examples.fix_support import MultiSessionFIXApplication, fix


def test_replay_report_preserves_exact_numeric_fields_and_identities():
    message = fix.Message()
    message.getHeader().setField(fix.MsgType("8"))
    for tag, value in {
        11: "replacement",
        41: "original",
        37: "order-1",
        17: "execution-1",
        39: "1",
        150: "F",
        151: "0.12345678",
        32: "0.00000001",
        31: "63212.10",
        14: "0.00000002",
        38: "0.12345680",
        1003: "trade-1",
    }.items():
        message.setField(fix.StringField(tag, value))
    report = MultiSessionFIXApplication()._parse_report(
        message, fix.SessionID("FIX.4.4", "replay_buy", "MOCKX_COINAPI")
    )
    assert report.cum_qty == "0.00000002"
    assert report.last_qty == "0.00000001"
    assert report.last_price == "63212.10"
    assert report.quantity == "0.12345680"
    assert report.order_id == "order-1"
    assert report.exec_id == "execution-1"
    assert report.trade_id == "trade-1"
    assert report.orig_cl_ord_id == "original"
    assert report.received_ns > 0

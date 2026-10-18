"""The documented client helper sends a full exact FIX replacement."""

from examples import fix_support


def test_replace_helper_sends_complete_exact_request(monkeypatch):
    sent = []
    session = object()
    monkeypatch.setattr(fix_support, "_send", lambda message, target: sent.append((message, target)))
    fix_support.send_replace(
        session,
        cl_ord_id="R1",
        orig_cl_ord_id="O1",
        symbol="TEST",
        side="1",
        quantity="123456789.001",
        price="61234.50",
    )
    message, target = sent.pop()
    assert target is session
    assert message.getHeader().getField(35) == "G"
    assert {tag: message.getField(tag) for tag in (11, 41, 55, 54, 38, 40, 44, 59)} == {
        11: "R1", 41: "O1", 55: "TEST", 54: "1", 38: "123456789.001",
        40: "2", 44: "61234.50", 59: "1",
    }
    assert message.isSetField(60)

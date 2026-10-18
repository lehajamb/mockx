"""Native FIX cancel/replace ingress and delivery regressions."""

from datetime import timedelta

import pytest

from src.mockx.gateway import fix
from src.mockx.fix_fields import execution_report_fields, cancel_reject_fields
from src.mockx.models import ExecutionReport, OrderStatus, OrderCancelReject, CancelRejectReason
from .test_fix_ingress_pipeline import _exchange, _new_order, _cancel, _start, _stop


def _replace(cl_ord_id, orig_cl_ord_id, seq, *, qty=5, price=100, side=fix.Side_BUY):
    message = _new_order(cl_ord_id, seq, qty=qty, price=price, side=side)
    message.getHeader().setField(fix.MsgType("G"))
    message.setField(fix.OrigClOrdID(orig_cl_ord_id))
    return message


@pytest.mark.asyncio
async def test_replace_reduction_advances_identity_and_supports_cancel():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("N", 1, qty=10), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        original = delivered[0]
        exchange.fix_app.fromApp(_replace("R", "N", 2, qty=6), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert len(delivered) == 2
        ack = delivered[-1]
        assert isinstance(ack, ExecutionReport)
        assert execution_report_fields(ack)["ExecType"] == "5"
        assert ack.cl_ord_id == "R" and ack.orig_cl_ord_id == "N"
        assert ack.order_id == original.order_id
        assert ack.order_qty_lots == ack.leaves_qty_lots == 6
        assert original.cl_ord_id == "N" and original.leaves_qty_lots == 10
        exchange.fix_app.fromApp(_cancel("C", "R", 3, qty=6), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert delivered[-1].ord_status == OrderStatus.CANCELLED
        assert delivered[-1].orig_cl_ord_id == "R"
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_replace_uses_inbound_engine_session_and_outbound_delays():
    exchange = _exchange(inbound_ms=5, engine_ms=3, outbound_ms=7, session_delay_ms=4)
    sessions, delivered = await _start(exchange)
    try:
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("N-LAT", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(
            exchange.scheduler.current_time + timedelta(milliseconds=19)
        )
        start = exchange.scheduler.current_time
        exchange.fix_app.fromApp(_replace("R-LAT", "N-LAT", 2, qty=3), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=11))
        assert next(iter(exchange.engine.orders.values())).cl_ord_id == "N-LAT"
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=12))
        assert next(iter(exchange.engine.orders.values())).cl_ord_id == "R-LAT"
        assert len(delivered) == 1
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=19))
        assert len(delivered) == 2
        assert execution_report_fields(delivered[-1])["ExecType"] == "5"
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_replace_unknown_reference_uses_replace_cancel_reject():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        exchange.fix_app.fromApp(_replace("R-UNKNOWN", "MISSING", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert len(delivered) == 1
        fields = cancel_reject_fields(delivered[0])
        assert fields["ClOrdID"] == "R-UNKNOWN"
        assert fields["OrigClOrdID"] == "MISSING"
        assert fields["CxlRejResponseTo"] == "2"
        assert fields["OrderID"] == "NONE"
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,code", [
    (38, "0", "NON_POSITIVE_QUANTITY"),
    (38, "1.5", "OFF_LOT_QUANTITY"),
    (38, "NaN", "NON_FINITE_NUMBER"),
    (44, "100.1", "OFF_TICK_PRICE"),
    (44, "Infinity", "NON_FINITE_NUMBER"),
    (55, "MISSING", "UNKNOWN_SYMBOL"),
    (54, "9", "UNSUPPORTED_SIDE"),
])
async def test_replace_exact_validation_preserves_existing_order(field, value, code):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("VALID", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        request = _replace("INVALID", "VALID", 2)
        request.setField(fix.StringField(field, value))
        exchange.fix_app.fromApp(request, session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        rejection = delivered[-1]
        assert isinstance(rejection, OrderCancelReject)
        assert rejection.reason == CancelRejectReason.INVALID_REQUEST
        assert code in rejection.text
        assert rejection.ord_status == OrderStatus.NEW
        assert cancel_reject_fields(rejection)["CxlRejResponseTo"] == "2"
        assert next(iter(exchange.engine.orders.values())).cl_ord_id == "VALID"
        exchange.engine.books["TEST"].assert_invariants()
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", [11, 41])
async def test_replace_missing_identifiers_use_business_rejection(tag):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    rejects = []
    exchange.fix_app.send_business_reject = lambda *args, **kwargs: rejects.append(kwargs)
    try:
        request = _replace("R", "N", 1)
        request.removeField(tag)
        exchange.fix_app.fromApp(request, sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        assert len(rejects) == 1
        assert rejects[0]["ref_msg_type"] == "G"
        assert rejects[0]["ref_seq_num"] == 1
        assert delivered == []
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_replace_default_gtc_and_noop_advance_identity_without_book_update():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("NOOP-N", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        snapshots = []
        exchange.engine.on_market_data_update = snapshots.append
        request = _replace("NOOP-R", "NOOP-N", 2)
        request.removeField(59)
        exchange.fix_app.fromApp(request, session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert delivered[-1].cl_ord_id == "NOOP-R"
        assert execution_report_fields(delivered[-1])["ExecType"] == "5"
        assert snapshots == []
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_replace_stale_reference_and_shared_dfg_duplicate_namespace():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("CHAIN-N", 1), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        exchange.fix_app.fromApp(_replace("CHAIN-R", "CHAIN-N", 2, qty=4), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        exchange.fix_app.fromApp(_replace("STALE", "CHAIN-N", 3), session)
        exchange.fix_app.fromApp(_replace("CHAIN-N", "CHAIN-R", 4), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert delivered[-2].reason == CancelRejectReason.STALE_ORDER_REFERENCE
        assert delivered[-1].reason == CancelRejectReason.DUPLICATE_CL_ORD_ID
        assert next(iter(exchange.engine.orders.values())).cl_ord_id == "CHAIN-R"
        exchange.fix_app.fromApp(_new_order("CHAIN-R", 5), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert delivered[-1].ord_status == OrderStatus.REJECTED
        assert "DUPLICATE_CL_ORD_ID" in delivered[-1].text
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_original_liquidity_executes_while_replace_is_in_flight():
    exchange = _exchange(inbound_ms=5, engine_ms=3, session_delay_ms=4)
    sessions, delivered = await _start(exchange, "CLIENT", "CLIENT2")
    try:
        session = sessions["CLIENT"]
        exchange.fix_app.fromApp(_new_order("RACE-N", 1, qty=10), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(
            exchange.scheduler.current_time + timedelta(milliseconds=12)
        )
        exchange.fix_app.fromApp(_replace("RACE-R", "RACE-N", 2, qty=3), session)
        exchange.fix_app.fromApp(
            _new_order("RACE-HIT", 1, qty=4, side=fix.Side_SELL), sessions["CLIENT2"]
        )
        await exchange.fix_app.wait_for_ingress()
        start = exchange.scheduler.current_time
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=8))
        order = next(order for order in exchange.engine.orders.values() if order.cl_ord_id == "RACE-N")
        assert order.filled_qty_lots == 4 and order.leaves_qty_lots == 6
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=12))
        ack = delivered[-1]
        assert ack.cl_ord_id == "RACE-R"
        assert execution_report_fields(ack)["ExecType"] == "5"
        assert ack.order_qty_lots == ack.cum_qty_lots == 4
        assert ack.leaves_qty_lots == ack.canceled_qty_lots == 0
        assert ack.ord_status == OrderStatus.FILLED
        assert ack.last_qty_lots is None
        exchange.engine.books["TEST"].assert_invariants()
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", [38, 40, 44, 54, 55])
async def test_replace_missing_required_terms_use_domain_rejection(tag):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        request = _replace("R-MISSING", "N-MISSING", 1)
        request.removeField(tag)
        exchange.fix_app.fromApp(request, sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert len(delivered) == 1
        assert isinstance(delivered[0], OrderCancelReject)
        assert cancel_reject_fields(delivered[0])["CxlRejResponseTo"] == "2"
        assert cancel_reject_fields(delivered[0])["OrderID"] == "NONE"
    finally:
        await _stop(exchange)

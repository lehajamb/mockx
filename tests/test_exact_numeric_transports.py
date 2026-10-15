"""Untrusted numeric text remains exact through FIX and ITCH boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from src.mockx.configuration import (
    ConfigurationError,
    MAX_INSTRUMENT_UNITS,
    validate_config,
)
from src.mockx.gateway import fix
from src.mockx.instruments import InstrumentCatalog
from src.mockx.itch_encoder import MAX_PRICE4, _price_to_itch
from .api_helpers import make_native_exchange as MockExchange
from src.mockx.models import (
    CancelReject,
    CancelRejectReason,
    ExecutionReport,
    FixDeliveryState,
    OrderStatus,
    SchedulerState,
)
from src.mockx.validation import (
    OrderNormalizer,
    OrderValidationError,
    RawOrderInput,
)

from .api_helpers import api_config
from .test_fix_ingress_pipeline import _cancel, _new_order, _start
from .test_itch_integration import itch_config


INVALID_NUMBERS = [
    ("quantity", "1.0000000000000000000000000000001", "OFF_LOT_QUANTITY"),
    ("quantity", "1E-999999999", "OFF_LOT_QUANTITY"),
    ("quantity", "1E999999999", "NUMBER_OUT_OF_RANGE"),
    ("price", "100.0000000000000000000000000000001", "OFF_TICK_PRICE"),
    ("price", "1E-999999999", "OFF_TICK_PRICE"),
    ("price", "1E999999999", "NUMBER_OUT_OF_RANGE"),
]


def numeric_exchange(**instrument_overrides) -> MockExchange:
    raw = api_config()
    raw["instruments"]["TEST"].update(tick_size="0.01", lot_size="0.001")
    raw["instruments"]["TEST"].update(instrument_overrides)
    raw["fix"]["session_participants"] = {"FIX.4.4:MOCKX->CLIENT": "participant-client"}
    return MockExchange(validate_config(raw, environ={}))


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "raw", "code"), INVALID_NUMBERS)
async def test_fix_rejects_exact_invalid_numeric_text_without_creating_order(
    field, raw, code
):
    exchange = numeric_exchange()
    sessions, delivered = await _start(exchange)
    business_rejects = []
    exchange.fix_app.send_business_reject = (
        lambda *args, **kwargs: business_rejects.append((args, kwargs))
    )
    try:
        message = _new_order("EXACT-INVALID", 1, qty=1, price=100)
        message.setField(fix.StringField(38 if field == "quantity" else 44, raw))
        exchange.fix_app.fromApp(message, sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        assert business_rejects == []
        assert len(delivered) == 1
        report = delivered[0]
        assert isinstance(report, ExecutionReport)
        assert report.ord_status == OrderStatus.REJECTED
        assert report.text.startswith(f"{code}:")
        assert report.order_qty_lots == report.leaves_qty_lots == 0
        assert exchange.engine.orders == {}
        assert exchange.engine.client_order_index == {}
        assert exchange.scheduler.state == SchedulerState.RUNNING
        assert exchange.scheduler.dead_letter is None
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [value for field, value, _ in INVALID_NUMBERS if field == "quantity"],
)
async def test_fix_cancel_invalid_numeric_quantity_preserves_resting_order(raw):
    exchange = numeric_exchange()
    sessions, delivered = await _start(exchange)
    try:
        exchange.fix_app.fromApp(
            _new_order("KEEP-RESTING", 1, qty=1, price=100),
            sessions["CLIENT"],
        )
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        order = next(iter(exchange.engine.orders.values()))
        before = exchange.engine.get_market_data_snapshot("TEST")
        delivered.clear()

        message = _cancel("INVALID-CANCEL", "KEEP-RESTING", 2, qty=1)
        message.setField(fix.StringField(38, raw))
        exchange.fix_app.fromApp(message, sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        assert len(delivered) == 1
        reject = delivered[0]
        assert isinstance(reject, CancelReject)
        assert reject.reason == CancelRejectReason.INVALID_REQUEST
        assert reject.cancel_cl_ord_id == "INVALID-CANCEL"
        assert reject.orig_cl_ord_id == "KEEP-RESTING"
        assert order.status == OrderStatus.NEW
        assert order.leaves_qty_lots == 1000
        assert exchange.engine.get_market_data_snapshot("TEST") == before
        assert exchange.scheduler.state == SchedulerState.RUNNING
        assert exchange.scheduler.dead_letter is None
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_valid_large_fix_values_serialize_identically_across_transport_retry():
    exchange = numeric_exchange(
        tick_size="0.0001",
        lot_size="1E-18",
        price_display_precision=4,
        quantity_display_precision=18,
    )
    sessions, _ = await _start(exchange)
    start = exchange.scheduler.current_time
    payloads = []
    fields = []

    def serialize_and_retry(_transport, payload):
        payloads.append(payload)
        wire = exchange.fix_app._send_quickfix_execution_report(payload)
        fields.append({tag: wire.getField(tag) for tag in (17, 38, 44, 151)})
        return len(payloads) > 1

    exchange.fix_app.transport_sender = serialize_and_retry
    try:
        message = _new_order("LARGE-EXACT", 1)
        message.setField(fix.StringField(38, "0.000000004294967295"))
        message.setField(fix.StringField(44, "200000.0000"))
        exchange.fix_app.fromApp(message, sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start)
        queued = exchange.fix_app.delivery_queue.snapshot_records()[0]
        assert queued.state == FixDeliveryState.RETRY_PENDING
        assert queued.payload.order_qty_lots == (1 << 32) - 1
        assert queued.payload.order_price_ticks == 2_000_000_000

        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=1000))
        assert len(payloads) == len(fields) == 2
        assert payloads[0] is payloads[1] is queued.payload
        assert (
            fields[0]
            == fields[1]
            == {
                17: queued.payload.exec_id,
                38: "0.000000004294967295",
                44: "200000.0000",
                151: "0.000000004294967295",
            }
        )
        final = exchange.fix_app.delivery_queue.get(queued.delivery_id)
        assert final.state == FixDeliveryState.DELIVERED
        assert final.attempt_count == 2
        assert exchange.scheduler.state == SchedulerState.RUNNING
    finally:
        await exchange.stop_components()


def itch_normalizer() -> OrderNormalizer:
    config = validate_config(
        itch_config(tick_size="0.0001", price_display_precision=4), environ={}
    )
    return OrderNormalizer(InstrumentCatalog.from_config(config), itch_compatible=True)


def itch_raw(**overrides) -> RawOrderInput:
    values = {
        "session_id": "ITCH:exact",
        "participant_id": "participant-itch",
        "cl_ord_id": "ITCH-EXACT",
        "symbol": "TEST",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "1",
        "price": "200000.0000",
    }
    values.update(overrides)
    return RawOrderInput(**values)


def test_itch_price_limit_rejects_one_unit_over_maximum_under_small_context():
    normalizer = itch_normalizer()
    with localcontext() as context:
        context.prec = 6
        with pytest.raises(OrderValidationError) as caught:
            normalizer.normalize(
                itch_raw(price="200000.0001"),
                submitted_at=datetime.now(timezone.utc),
            )
        assert caught.value.code.value == "NUMBER_OUT_OF_RANGE"
        assert caught.value.field == "price"


def test_itch_quantity_maximum_is_exact_under_small_context():
    normalizer = itch_normalizer()
    with localcontext() as context:
        context.prec = 6
        command = normalizer.normalize(
            itch_raw(quantity=str(0xFFFFFFFF)),
            submitted_at=datetime.now(timezone.utc),
        )
    assert command.order_qty_lots == 0xFFFFFFFF
    assert command.price_ticks == MAX_PRICE4


def test_itch_encoder_does_not_round_incompatible_tick_under_small_context():
    with localcontext() as context:
        context.prec = 4
        with pytest.raises(ValueError, match="exactly representable"):
            _price_to_itch(1, Decimal("0.12345"))


def test_itch_configuration_does_not_round_incompatible_tick_under_small_context():
    raw = itch_config(tick_size="0.12345", price_display_precision=5)
    with localcontext() as context:
        context.prec = 4
        with pytest.raises(ConfigurationError, match="not representable"):
            validate_config(raw, environ={})

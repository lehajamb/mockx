"""Native FIX copies keep dynamic report fields isolated and templates bounded."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.mockx.fix_fields import execution_report_fields
from src.mockx.gateway import fix
from src.mockx.instruments import Instrument
from src.mockx.models import (
    ExecutionReport,
    ExecutionType,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from tests.test_fix_ingress_pipeline import _exchange
from tests.test_fix_raw_tag_access import FieldAccessProbe, reference_wire


@pytest.fixture
def report():
    instrument = Instrument("TEST", Decimal("0.01"), Decimal("0.00000001"), 2, 8)
    now = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ExecutionReport(
        exec_id="E1",
        order_id=1,
        cl_ord_id="N1",
        symbol="TEST",
        instrument=instrument,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        order_qty_lots=100,
        order_price_ticks=6300000,
        tif=TimeInForce.GTC,
        ord_status=OrderStatus.NEW,
        exec_type=ExecutionType.NEW,
        leaves_qty_lots=100,
        cum_qty_lots=0,
        canceled_qty_lots=0,
        filled_notional_tick_lots=0,
        submitted_at=now,
        timestamp=now,
        order_version=0,
    )


def test_varying_prices_and_sizes_do_not_repeat_static_native_setters(
    monkeypatch, report
):
    app = _exchange().fix_app
    factory = fix.Message
    accesses = []

    def message(*args):
        args = tuple(
            arg.message if isinstance(arg, FieldAccessProbe) else arg for arg in args
        )
        return FieldAccessProbe(factory(*args), accesses)

    monkeypatch.setattr(fix, "Message", message)
    app._send_quickfix_execution_report(report)
    accesses.clear()
    for index in range(100):
        changed = replace(
            report,
            exec_id=f"E{index}",
            cl_ord_id=f"N{index}",
            order_id=index + 1,
            order_qty_lots=100 + index,
            leaves_qty_lots=100 + index,
            order_price_ticks=6300000 + index,
        )
        wire = app._send_quickfix_execution_report(changed)
        assert wire.toString() == reference_wire(
            "8", execution_report_fields(changed), factory
        )
    writes = [tag for operation, tag in accesses if operation == "set"]
    assert len(writes) == 600
    assert set(writes) == {11, 17, 37, 38, 44, 151}
    assert app._execution_report_template.cache_info().currsize == 1


def test_report_variants_keep_golden_wire_bytes_and_clone_isolation(report):
    app = _exchange().fix_app
    partial = replace(
        report,
        exec_type=ExecutionType.TRADE,
        ord_status=OrderStatus.PARTIAL,
        leaves_qty_lots=60,
        cum_qty_lots=40,
        filled_notional_tick_lots=40 * 6300000,
        last_price_ticks=6300000,
        last_qty_lots=40,
        trade_id=1,
    )
    variants = [
        report,
        replace(report, side=Side.SELL),
        partial,
        replace(
            partial,
            ord_status=OrderStatus.FILLED,
            leaves_qty_lots=0,
            cum_qty_lots=100,
            filled_notional_tick_lots=100 * 6300000,
            last_qty_lots=60,
            trade_id=2,
        ),
        replace(
            report,
            exec_type=ExecutionType.CANCELED,
            ord_status=OrderStatus.CANCELLED,
            leaves_qty_lots=0,
            canceled_qty_lots=100,
            cl_ord_id="C1",
            orig_cl_ord_id="N1",
        ),
        replace(
            report,
            exec_type=ExecutionType.CANCELED,
            ord_status=OrderStatus.CANCELLED,
            leaves_qty_lots=0,
            cum_qty_lots=40,
            canceled_qty_lots=60,
            filled_notional_tick_lots=40 * 6300000,
            cl_ord_id="C2",
            orig_cl_ord_id="N1",
        ),
        replace(
            report,
            exec_type=ExecutionType.REPLACED,
            cl_ord_id="R1",
            orig_cl_ord_id="N1",
            order_version=1,
        ),
        replace(
            report,
            exec_type=ExecutionType.REJECTED,
            ord_status=OrderStatus.REJECTED,
            leaves_qty_lots=0,
            canceled_qty_lots=100,
            text="UNKNOWN_SYMBOL: rejected",
        ),
        replace(
            report,
            order_type=OrderType.MARKET,
            order_price_ticks=None,
            tif=TimeInForce.IOC,
        ),
        replace(
            partial,
            order_type=OrderType.MARKET,
            order_price_ticks=None,
            tif=TimeInForce.IOC,
        ),
    ]
    for item in variants:
        expected = reference_wire("8", execution_report_fields(item), fix.Message)
        first = app._send_quickfix_execution_report(item)
        assert first.toString() == expected
        first.setField(58, "changed clone")
        first.setField(14, "999999")
        first.removeField(55)
        first.getHeader().setField(34, "777")
        second = app._send_quickfix_execution_report(item)
        assert second.toString() == expected
        assert not second.getHeader().isSetField(34)


def test_static_zero_formats_belong_to_the_instrument_and_cache_is_bounded(report):
    app = _exchange().fix_app
    for precision in (0, 3, 8):
        instrument = Instrument("TEST", Decimal("0.01"), Decimal("1"), 2, precision)
        item = replace(report, instrument=instrument)
        message = app._send_quickfix_execution_report(item)
        assert message.getField(14) == instrument.format_quantity(0)
    for index in range(150):
        symbol = f"S{index}"
        instrument = replace(report.instrument, symbol=symbol)
        app._send_quickfix_execution_report(
            replace(report, symbol=symbol, instrument=instrument)
        )
    info = app._execution_report_template.cache_info()
    assert info.currsize == info.maxsize == 128

"""Exact aggregate, book and derived-average numeric regressions."""

import asyncio
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_DOWN, ROUND_HALF_EVEN, localcontext

import pytest
from httpx import ASGITransport, AsyncClient

from src.mockx.configuration import validate_config
from src.mockx.instruments import Instrument, InstrumentCatalog
from src.mockx.main import MockExchange
from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricsCollector,
)
from src.mockx.effects import CommandEffect, CommandEffects
from src.mockx.journal import ExchangeJournal
from src.mockx.journal_reader import NativeJournalReader
from src.mockx.models import (
    DisplayedOrderAdded,
    ExecutionType,
    MarketDataSnapshot,
    OrderStatus,
    PriceLevelSnapshot,
    Side,
    TimeInForce,
    Trade,
    create_execution_report,
)
from tests.api_helpers import api_config, process_order
from tests.exact_helpers import exact_order


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
MAX_UNITS = (1 << 63) - 1


def exact_decimal(coefficient, exponent):
    """Construct test expectations without context-dependent arithmetic."""
    return Decimal((0, tuple(int(digit) for digit in str(coefficient)), exponent))


def altered_context(precision):
    context = Context(prec=precision, rounding=ROUND_DOWN, Emin=-9, Emax=9)
    for signal in context.traps:
        context.traps[signal] = False
    return context


def make_instrument(symbol="TEST", *, tick="0.03", lot="0.07", precision=2):
    return Instrument(symbol, Decimal(tick), Decimal(lot), precision, 2)


def canceled_report(instrument, identity, quantity):
    order = exact_order(
        id=identity,
        cl_ord_id=f"C-{identity}",
        symbol=instrument.symbol,
        side=Side.BUY,
        order_qty_lots=quantity,
        price_ticks=1,
        instrument=instrument,
        tif=TimeInForce.GTC,
        ts_submitted=NOW,
        participant_id="participant",
        status=OrderStatus.CANCELLED,
        canceled_qty_lots=quantity,
    )
    return create_execution_report(
        order, exec_type=ExecutionType.CANCELED, timestamp=NOW, exec_id=f"E-{identity}"
    )


def record_large_activity(
    metrics, instrument, identity, *, quantity=MAX_UNITS, price=MAX_UNITS
):
    trade = Trade(
        id=identity,
        symbol=instrument.symbol,
        price_ticks=price,
        qty_lots=quantity,
        notional_tick_lots=price * quantity,
        buyer_order_id=identity * 2,
        seller_order_id=identity * 2 + 1,
        timestamp=NOW,
        level=1,
        buyer_session_alias=None,
        seller_session_alias=None,
        buyer_participant_id="buyer",
        seller_participant_id="seller",
        aggressor_order_id=identity * 2,
        passive_order_id=identity * 2 + 1,
        aggressor_side=Side.BUY,
    )
    assert metrics.record_trade(trade)
    assert metrics.record_execution_report(
        canceled_report(instrument, identity, quantity)
    )


@pytest.mark.parametrize("precision", [2, 28, 80])
def test_current_and_lifetime_totals_are_exact_across_run_reset(precision):
    instrument = make_instrument()
    metrics = MetricsCollector(
        "TEST",
        instruments=InstrumentCatalog({"TEST": instrument}),
        prometheus_enabled=False,
    )
    with localcontext(Context(prec=100)):
        record_large_activity(metrics, instrument, 1)
        record_large_activity(metrics, instrument, 2)
    expected_quantity = exact_decimal(14 * MAX_UNITS, -2)
    expected_notional = exact_decimal(42 * MAX_UNITS * MAX_UNITS, -4)

    with localcontext(altered_context(precision)):
        snapshot = metrics.snapshot()
        lifetime = metrics.process_lifetime_snapshot()["trades"]
    assert Decimal(snapshot.traded_quantity) == expected_quantity
    assert Decimal(snapshot.traded_notional) == expected_notional
    assert Decimal(snapshot.canceled_quantity) == expected_quantity
    assert Decimal(lifetime["quantity"]) == expected_quantity
    assert Decimal(lifetime["notional"]) == expected_notional
    assert Decimal(lifetime["canceled_quantity"]) == expected_quantity
    assert metrics._symbol_totals["TEST"]["traded_qty_lots"] > MAX_UNITS

    metrics.reset_run_metrics()
    with localcontext(altered_context(precision)):
        assert Decimal(metrics.snapshot().traded_quantity) == 0
        assert (
            Decimal(metrics.process_lifetime_snapshot()["trades"]["notional"])
            == expected_notional
        )


def test_multiple_instrument_totals_preserve_small_terms_with_traps_enabled():
    first = make_instrument()
    second = Instrument(
        "SMALL",
        Decimal("0.000000000000000003"),
        Decimal("0.000000000000000007"),
        18,
        18,
    )
    metrics = MetricsCollector(
        "TEST",
        instruments=InstrumentCatalog({"TEST": first, "SMALL": second}),
        prometheus_enabled=False,
    )
    with localcontext(Context(prec=200)):
        record_large_activity(metrics, first, 1)
        record_large_activity(metrics, first, 2)
        record_large_activity(metrics, second, 3, quantity=3, price=5)
    expected_quantity = exact_decimal(14 * MAX_UNITS * 10**16 + 21, -18)
    expected_notional = exact_decimal(42 * MAX_UNITS * MAX_UNITS * 10**32 + 315, -36)
    context = altered_context(2)
    for signal in context.traps:
        context.traps[signal] = True
    with localcontext(context):
        snapshot = metrics.snapshot()
        lifetime = metrics.process_lifetime_snapshot()["trades"]
    assert Decimal(snapshot.traded_quantity) == expected_quantity
    assert Decimal(snapshot.traded_notional) == expected_notional
    assert Decimal(snapshot.canceled_quantity) == expected_quantity
    assert Decimal(lifetime["quantity"]) == expected_quantity
    assert Decimal(lifetime["notional"]) == expected_notional
    assert Decimal(lifetime["canceled_quantity"]) == expected_quantity


@pytest.mark.parametrize(
    "bid_ticks,ask_ticks,midpoint",
    [
        (1000000000000000, 1000000000000001, "10000000000000.00"),
        (1000000000000001, 1000000000000002, "10000000000000.02"),
    ],
)
def test_native_journal_preserves_exact_price_and_aggregate_units(
    bid_ticks, ask_ticks, midpoint
):
    instrument = make_instrument(tick="0.01")
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink)
    journal = ExchangeJournal(writer, "TEST")

    async def persist():
        await journal.start_run("exact-book", "precision", (instrument,), NOW)
        changes = tuple(
            CommandEffect(
                "book_change",
                DisplayedOrderAdded(1, i, "TEST", side, ticks, MAX_UNITS, NOW),
            )
            for i, side, ticks in (
                (1, Side.BUY, bid_ticks),
                (2, Side.BUY, bid_ticks),
                (3, Side.SELL, ask_ticks),
                (4, Side.SELL, ask_ticks),
            )
        )
        with localcontext(altered_context(3)):
            await journal.admit_command(CommandEffects("seed", NOW, changes))
        await writer.flush()
        await writer.close()

    asyncio.run(persist())
    rows = [
        {**record.symbol_map, **record.column_map}
        for batch in sink.writes
        for record in batch
    ]
    book = NativeJournalReader()
    book.apply_rows(rows)
    assert book.book.depth("TEST") == (
        ((bid_ticks, 2 * MAX_UNITS),),
        ((ask_ticks, 2 * MAX_UNITS),),
    )
    adds = [row for row in rows if row["kind"] == "BOOK_ADDED"]
    assert [row["price_ticks"] for row in adds] == [
        bid_ticks,
        bid_ticks,
        ask_ticks,
        ask_ticks,
    ]
    assert all(isinstance(row["qty_lots"], int) for row in adds)
    assert all("bids_json" not in row for row in rows)


@pytest.mark.parametrize("precision", [2, 28, 80])
def test_order_and_report_average_properties_use_explicit_28_digit_context(precision):
    instrument = make_instrument(tick="0.123456789012345678", precision=18)
    notional = 10000000000000000013
    order = exact_order(
        id=1,
        cl_ord_id="AVERAGE",
        symbol="TEST",
        side=Side.BUY,
        order_qty_lots=6,
        price_ticks=MAX_UNITS,
        instrument=instrument,
        tif=TimeInForce.GTC,
        ts_submitted=NOW,
        participant_id="participant",
        status=OrderStatus.PARTIAL,
        filled_qty_lots=3,
        filled_notional_tick_lots=notional,
    )
    report = create_execution_report(
        order,
        exec_type=ExecutionType.TRADE,
        timestamp=NOW,
        exec_id="AVERAGE-REPORT",
        last_qty_lots=3,
        last_price_ticks=1,
        trade_id=1,
    )
    exact_numerator = exact_decimal(123456789012345678 * notional, -18)
    reference = Context(prec=28, rounding=ROUND_HALF_EVEN).divide(
        exact_numerator, Decimal(3)
    )
    context = Context(prec=28) if precision == 28 else altered_context(precision)
    with localcontext(context):
        order_average = order.average_fill_price
        report_average = report.average_fill_price
    assert isinstance(order_average, Decimal)
    assert isinstance(report_average, Decimal)
    assert order_average == reference
    assert report_average == reference


def test_unfilled_average_property_shapes_are_preserved():
    order = exact_order(
        id=1,
        cl_ord_id="UNFILLED",
        symbol="TEST",
        side=Side.BUY,
        order_qty_lots=1,
        price_ticks=1,
        tif=TimeInForce.GTC,
        ts_submitted=NOW,
        participant_id="participant",
        status=OrderStatus.NEW,
    )
    report = create_execution_report(
        order, exec_type=ExecutionType.NEW, timestamp=NOW, exec_id="UNFILLED-REPORT"
    )
    with localcontext(altered_context(2)):
        assert order.average_fill_price is None
        assert report.average_fill_price == Decimal(0)


@pytest.mark.parametrize(
    "tick,precision,bid_ticks,ask_ticks,expected",
    [
        ("0.01", 2, 1000000000000001, 1000000000000002, "10000000000000.015"),
        ("1", 2, 100, 101, "100.50"),
        ("1", 0, 100, 101, "100.5"),
        ("1", 0, 100, 102, "101"),
        ("0.03", 4, 100, 101, "3.0150"),
        ("0.000000000000000001", 18, 1, 2, "0.0000000000000000015"),
    ],
)
def test_exact_half_tick_value_can_be_derived_without_binary_float(
    tick, precision, bid_ticks, ask_ticks, expected
):
    from src.mockx.instruments import decimal_product

    with localcontext(Context(prec=100)):
        midpoint = decimal_product(Decimal(tick), bid_ticks + ask_ticks) / Decimal(2)
    assert midpoint == Decimal(expected)

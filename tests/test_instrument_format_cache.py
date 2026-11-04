"""Repeated wire values reuse exact integer formatting without changing coercion."""

from decimal import Decimal, localcontext

import pytest

import src.mockx.instruments as instruments
from src.mockx.instruments import Instrument


def test_integer_display_reuses_one_bounded_cache_for_price_and_quantity(monkeypatch):
    instrument = Instrument("FORMAT-CACHE", Decimal(".0037"), Decimal(".00013"), 4, 5)
    original = instruments.decimal_product
    calls = []

    def observed(*values):
        calls.append(values)
        return original(*values)

    monkeypatch.setattr(instruments, "decimal_product", observed)
    assert instrument.format_price(91) == instrument.format_price(91) == "0.3367"
    assert instrument.format_quantity(0) == instrument.format_quantity(0) == "0.00000"
    assert len(calls) == 2
    assert instruments._format_integer_units.cache_info().maxsize == 16_384


@pytest.mark.parametrize(
    "value", [True, False, "123", 1.5, Decimal("-0"), -123, 2**63 - 1]
)
def test_cached_integer_formatting_preserves_exact_existing_boundary_behavior(value):
    instrument = Instrument("BOUNDARY", Decimal(".05"), Decimal(".00000001"), 2, 8)
    for formatter, decimal, precision in (
        (instrument.format_price, instrument.price_decimal, 2),
        (instrument.format_quantity, instrument.quantity_decimal, 8),
    ):
        # Prime potentially equal Python keys such as True and1.
        formatter(0)
        formatter(1)
        expected = instrument._format(decimal(value), precision)
        with localcontext() as context:
            context.prec = 2
            assert formatter(value) == expected


def test_noninteger_input_never_reuses_an_integer_cache_entry(monkeypatch):
    instrument = Instrument("BOOL-CACHE", Decimal(".0071"), Decimal(".0029"), 4, 4)
    instrument.format_price(1)
    original = instruments.decimal_product

    def observed(increment, value):
        if type(value) is not int:
            raise TypeError("uncached boundary validation")
        return original(increment, value)

    monkeypatch.setattr(instruments, "decimal_product", observed)
    with pytest.raises(TypeError, match="uncached boundary validation"):
        instrument.format_price(True)

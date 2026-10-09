"""Exact numeric boundary regressions independent of Decimal context."""

from decimal import (
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    localcontext,
)
from fractions import Fraction

from hypothesis import given, settings, strategies as st

import pytest
from pydantic import ValidationError

from src.mockx.configuration import InstrumentConfig, MAX_INSTRUMENT_UNITS
from src.mockx.instruments import (
    Instrument,
    InstrumentConversionError,
    parse_decimal,
)


def instrument(tick="0.01", lot="0.001", price_precision=2, quantity_precision=3):
    return Instrument(
        "EXACT", Decimal(tick), Decimal(lot), price_precision, quantity_precision,
    )


def numeric_context(name):
    context = Context()
    if name == "low_precision":
        context.prec = 2
        context.rounding = ROUND_DOWN
    elif name == "tight_exponents":
        context.Emin = -2
        context.Emax = 2
    elif name == "trapped_inexact":
        context.prec = 5
        context.traps[Inexact] = True
    return context


CONTEXTS = ["default", "low_precision", "tight_exponents", "trapped_inexact"]


@pytest.mark.parametrize("field", ["price", "quantity"])
@pytest.mark.parametrize(
    "value", ["1.00000000000000000000000000001", "0.99999999999999999999999999999"],
)
def test_invisible_decimal_tail_is_off_grid(field, value):
    conversion = getattr(
        instrument(), f"{field}_to_{'ticks' if field == 'price' else 'lots'}",
    )
    with pytest.raises(InstrumentConversionError) as caught:
        conversion(value)
    expected = "OFF_TICK_PRICE" if field == "price" else "OFF_LOT_QUANTITY"
    assert caught.value.code == expected


@pytest.mark.parametrize("field", ["price", "quantity"])
def test_off_grid_precedes_signed_64_bit_range_failure(field):
    conversion = getattr(
        instrument("1", "1", 0, 0),
        f"{field}_to_{'ticks' if field == 'price' else 'lots'}",
    )
    with pytest.raises(InstrumentConversionError) as caught:
        conversion(f"{MAX_INSTRUMENT_UNITS + 1}.00000000000000000000000000001")
    expected = "OFF_TICK_PRICE" if field == "price" else "OFF_LOT_QUANTITY"
    assert caught.value.code == expected


@pytest.mark.parametrize("context_name", CONTEXTS)
def test_exact_non_power_of_ten_increments_and_unit_bounds(context_name):
    subject = instrument("0.125", "0.25", 3, 2)
    with localcontext(numeric_context(context_name)):
        assert subject.price_to_ticks("1.375") == 11
        assert subject.quantity_to_lots("1.75") == 7
        assert subject.price_to_ticks("1152921504606846975.875") == MAX_INSTRUMENT_UNITS
        assert (
            subject.quantity_to_lots("2305843009213693951.75")
            == MAX_INSTRUMENT_UNITS
        )
        with pytest.raises(InstrumentConversionError) as caught:
            subject.price_to_ticks("1152921504606846976.000")
        assert caught.value.code == "NUMBER_OUT_OF_RANGE"


@pytest.mark.parametrize("field", ["price", "quantity"])
@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("1e1000000", "NUMBER_OUT_OF_RANGE"),
        ("1e-1000000", "OFF_INCREMENT"),
        ("1e-1000100", "OFF_INCREMENT"),
    ],
)
def test_extreme_exponents_produce_stable_validation_errors(field, value, code):
    conversion = getattr(
        instrument("1", "1", 0, 0),
        f"{field}_to_{'ticks' if field == 'price' else 'lots'}",
    )
    with pytest.raises(InstrumentConversionError) as caught:
        conversion(value)
    if code == "OFF_INCREMENT":
        code = "OFF_TICK_PRICE" if field == "price" else "OFF_LOT_QUANTITY"
    assert caught.value.code == code


@pytest.mark.parametrize("field", ["price", "quantity"])
@pytest.mark.parametrize(
    ("value", "code"),
    [("1e1000000", "OFF_INCREMENT"), ("3e1000000", "NUMBER_OUT_OF_RANGE")],
)
def test_huge_exponent_checks_non_decimal_increment_grid_before_bound(
    field, value, code,
):
    conversion = getattr(
        instrument("3", "3", 0, 0),
        f"{field}_to_{'ticks' if field == 'price' else 'lots'}",
    )
    with pytest.raises(InstrumentConversionError) as caught:
        conversion(value)
    if code == "OFF_INCREMENT":
        code = "OFF_TICK_PRICE" if field == "price" else "OFF_LOT_QUANTITY"
    assert caught.value.code == code


@pytest.mark.parametrize("field", ["price", "quantity"])
@pytest.mark.parametrize("context_name", CONTEXTS)
def test_shared_huge_exponents_cancel_without_expanding_value(field, context_name):
    subject = instrument("3.000e1000000", "3.000e1000000", 0, 0)
    conversion = getattr(
        subject, f"{field}_to_{'ticks' if field == 'price' else 'lots'}",
    )
    with localcontext(numeric_context(context_name)):
        assert conversion(Decimal("6.000e1000000")) == 2


@pytest.mark.parametrize("trapping", [False, True])
@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("not-a-number", "INVALID_NUMBER"),
        ("NaN", "NON_FINITE_NUMBER"),
        ("sNaN", "NON_FINITE_NUMBER"),
    ],
)
def test_numeric_error_code_does_not_depend_on_invalid_operation_trap(
    trapping, value, code,
):
    context = Context()
    context.traps[InvalidOperation] = trapping
    with localcontext(context):
        with pytest.raises(InstrumentConversionError) as caught:
            parse_decimal(value, field_name="price")
    assert caught.value.code == code


@pytest.mark.parametrize("field", ["tick_size", "lot_size"])
def test_inferred_precision_enforces_existing_eighteen_decimal_limit(field):
    values = {"tick_size": "1", "lot_size": "1", field: "1e-19"}
    with pytest.raises(ValidationError):
        InstrumentConfig(**values)


@pytest.mark.parametrize("field", ["tick_size", "lot_size"])
def test_eighteen_decimal_increment_is_supported(field):
    values = {"tick_size": "1", "lot_size": "1", field: "1e-18"}
    config = InstrumentConfig(**values)
    precision_field = (
        "price_display_precision"
        if field == "tick_size"
        else "quantity_display_precision"
    )
    assert getattr(config, precision_field) == 18


@pytest.mark.parametrize("precision", [None, 18])
def test_precision_cannot_hide_low_order_increment_digits(precision):
    with pytest.raises(ValidationError):
        InstrumentConfig(
            tick_size="1.00000000000000000000000000001",
            lot_size="1",
            price_display_precision=precision,
        )


@pytest.mark.parametrize("context_name", CONTEXTS)
def test_metadata_scale_is_exact_and_trailing_zeroes_are_insignificant(context_name):
    with localcontext(numeric_context(context_name)):
        config = InstrumentConfig(tick_size="0.01000000100", lot_size="0.2500")
        assert config.price_display_precision == 9
        assert config.quantity_display_precision == 2
        with pytest.raises(ValidationError):
            InstrumentConfig(
                tick_size="0.01000000100", lot_size="1",
                price_display_precision=2,
            )


@pytest.mark.parametrize("context_name", CONTEXTS)
def test_exact_products_and_fixed_scale_strings_survive_hostile_contexts(context_name):
    subject = instrument("0.125", "0.25", 3, 2)
    with localcontext(numeric_context(context_name)):
        assert subject.price_decimal(MAX_INSTRUMENT_UNITS) == Decimal(
            "1152921504606846975.875",
        )
        assert subject.quantity_decimal(MAX_INSTRUMENT_UNITS) == Decimal(
            "2305843009213693951.75",
        )
        assert subject.notional_decimal(
            MAX_INSTRUMENT_UNITS, MAX_INSTRUMENT_UNITS,
        ) == Decimal("2658455991569831745231153368257265664.03125")
        assert subject.format_price(MAX_INSTRUMENT_UNITS) == "1152921504606846975.875"
        assert subject.format_quantity(MAX_INSTRUMENT_UNITS) == "2305843009213693951.75"
        assert subject.format_notional(
            MAX_INSTRUMENT_UNITS * MAX_INSTRUMENT_UNITS,
        ) == "2658455991569831745231153368257265664.03125"


@pytest.mark.parametrize("context_name", CONTEXTS)
def test_large_integral_increment_keeps_all_significant_digits(context_name):
    subject = instrument("12345678901234567890123456789", "1", 0, 0)
    with localcontext(numeric_context(context_name)):
        assert subject.price_decimal(3) == Decimal("37037036703703703670370370367")
        assert subject.format_price(3) == "37037036703703703670370370367"


@pytest.mark.parametrize("context_name", CONTEXTS)
@pytest.mark.parametrize(
    ("notional", "expected"),
    [
        (1000000000000000001499999999999999999, "10000000000000000.01"),
        (1000000000000000001500000000000000000, "10000000000000000.02"),
        (1000000000000000002500000000000000000, "10000000000000000.02"),
        (1000000000000000002500000000000000001, "10000000000000000.03"),
    ],
)
def test_average_price_rounds_exact_ratio_once_at_display_precision(
    context_name, notional, expected,
):
    assert notional <= MAX_INSTRUMENT_UNITS * MAX_INSTRUMENT_UNITS
    quantity = 1_000_000_000_000_000_000
    assert quantity <= MAX_INSTRUMENT_UNITS
    with localcontext(numeric_context(context_name)):
        assert instrument().format_average_price(notional, quantity) == expected


@pytest.mark.parametrize("context_name", CONTEXTS)
def test_non_power_of_ten_average_and_unfilled_average_keep_display_padding(
    context_name,
):
    subject = instrument("0.25", "1", 2, 0)
    with localcontext(numeric_context(context_name)):
        assert subject.format_average_price(1, 2) == "0.12"
        assert subject.format_average_price(3, 2) == "0.38"
        assert subject.format_average_price(0, 0) == "0.00"


@pytest.mark.parametrize("field", ["price", "quantity"])
@settings(max_examples=200, derandomize=True, database=None)
@given(
    value_coefficient=st.integers(1, 2**256),
    value_exponent=st.integers(-40, 40),
    step_coefficient=st.integers(1, 2**128),
    step_exponent=st.integers(-18, 10),
    context_name=st.sampled_from(CONTEXTS),
)
def test_conversion_agrees_with_independent_fraction_oracle(
    field, value_coefficient, value_exponent, step_coefficient, step_exponent,
    context_name,
):
    value = Decimal((0, Decimal(value_coefficient).as_tuple().digits, value_exponent))
    step = Decimal((0, Decimal(step_coefficient).as_tuple().digits, step_exponent))
    ratio = Fraction(value) / Fraction(step)
    subject = Instrument("ORACLE", step, step, 18, 18)
    conversion = getattr(subject, f"{field}_to_{'ticks' if field == 'price' else 'lots'}")
    expected_code = None
    if ratio.denominator != 1:
        expected_code = "OFF_TICK_PRICE" if field == "price" else "OFF_LOT_QUANTITY"
    elif ratio.numerator > MAX_INSTRUMENT_UNITS:
        expected_code = "NUMBER_OUT_OF_RANGE"
    with localcontext(numeric_context(context_name)):
        if expected_code:
            with pytest.raises(InstrumentConversionError) as caught:
                conversion(value)
            assert caught.value.code == expected_code
        else:
            assert conversion(value) == ratio.numerator


@settings(max_examples=200, derandomize=True, database=None)
@given(
    step_coefficient=st.integers(1, 10**20),
    step_exponent=st.integers(-18, 6),
    notional=st.integers(0, MAX_INSTRUMENT_UNITS**2),
    quantity=st.integers(1, MAX_INSTRUMENT_UNITS),
    padding=st.integers(0, 18),
    context_name=st.sampled_from(CONTEXTS),
)
def test_display_average_agrees_with_high_precision_decimal_oracle(
    step_coefficient, step_exponent, notional, quantity, padding, context_name,
):
    step = Decimal((0, Decimal(step_coefficient).as_tuple().digits, step_exponent))
    precision = max(0, -step_exponent, padding)
    # These bounded operands require fewer than 100 significant digits to
    # distinguish a tie or either side of it; the oracle uses 200.
    with localcontext(Context(prec=200, rounding=ROUND_HALF_EVEN)):
        reference = step * Decimal(notional) / Decimal(quantity)
        expected = format(reference.quantize(Decimal((0, (1,), -precision))), "f")
    subject = Instrument("ORACLE", step, Decimal(1), precision, 0)
    with localcontext(numeric_context(context_name)):
        assert subject.format_average_price(notional, quantity) == expected


def test_long_coefficients_do_not_introduce_an_integer_string_length_limit():
    increment = "7" * 5000
    config = InstrumentConfig(tick_size=increment, lot_size="1")
    subject = Instrument.from_config("LONG", config)
    assert subject.price_to_ticks(increment) == 1
    assert subject.format_price(2) == "1" + "5" * 4999 + "4"

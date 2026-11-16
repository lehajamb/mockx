"""ITCH integer price conversion keeps exact boundaries without repeated Decimal work."""

from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest

from src.mockx import itch_encoder
from src.mockx.instruments import Instrument, InstrumentCatalog
from src.mockx.models import Side


def test_many_prices_validate_the_tick_multiplier_only_once(monkeypatch):
    cached = getattr(itch_encoder, "_price4_multiplier", None)
    if cached is not None:
        cached.cache_clear()
    original = itch_encoder.decimal_product
    calls = []

    def multiply(*values):
        calls.append(values)
        return original(*values)

    monkeypatch.setattr(itch_encoder, "decimal_product", multiply)
    for price in range(1, 101):
        assert itch_encoder._price_to_itch(price, Decimal("0.0137")) == price * 137
    assert calls == [(Decimal("0.0137"), 10_000)]


@pytest.mark.parametrize("price", [True, False, 1.0, "1", Decimal("1"), None])
def test_price_type_validation_remains_strict_after_warmup(price):
    assert itch_encoder._price_to_itch(1, Decimal("1")) == 10_000
    with pytest.raises(ValueError, match="price_ticks must be an integer"):
        itch_encoder._price_to_itch(price, Decimal("1"))


@pytest.mark.parametrize("tick", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_ticks_still_raise_numeric_validation_error(tick):
    with pytest.raises(ValueError, match="finite value"):
        itch_encoder._price_to_itch(1, Decimal(tick))


def test_exact_price_boundaries_and_tick_grid_ignore_decimal_context():
    with localcontext() as context:
        context.prec = 2
        assert (
            itch_encoder._price_to_itch(2_000_000_000, Decimal("0.0001"))
            == 2_000_000_000
        )
        assert itch_encoder._price_to_itch(20_000_000, Decimal("0.01")) == 2_000_000_000
        assert itch_encoder._price_to_itch(0, Decimal("0.01")) == 0
        for price in (-1, 20_000_001):
            with pytest.raises(ValueError, match="maximum"):
                itch_encoder._price_to_itch(price, Decimal("0.01"))
        with pytest.raises(ValueError, match="not exactly representable"):
            itch_encoder._price_to_itch(0, Decimal("0.00001"))


def test_huge_integral_tick_does_not_expand_decimal_exponent():
    huge_tick = Decimal("1e1000000000")
    assert itch_encoder._price_to_itch(0, huge_tick) == 0
    with pytest.raises(ValueError, match="maximum"):
        itch_encoder._price_to_itch(1, huge_tick)


def test_multiplier_cache_is_bounded_and_typed():
    cached = itch_encoder._price4_multiplier
    cached.cache_clear()
    for number in range(1, 301):
        assert itch_encoder._price_to_itch(1, Decimal(number)) == number * 10_000
    assert cached.cache_info().currsize == cached.cache_info().maxsize == 256
    assert cached.cache_parameters()["typed"] is True


def test_maximum_price_add_message_keeps_exact_golden_bytes():
    catalog = InstrumentCatalog(
        {"TEST": Instrument("TEST", Decimal("0.01"), Decimal("1"), 2, 0)}
    )
    encoder = itch_encoder.ITCHEncoder({"TEST": 1}, catalog)
    message = encoder.add_order_no_mpid(
        "TEST",
        0x0102030405060708,
        Side.BUY,
        5,
        20_000_000,
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        tracking_number=2,
    )
    assert message == (
        b"A\x00\x01\x00\x02"
        + bytes(6)
        + bytes.fromhex("0102030405060708")
        + b"B\x00\x00\x00\x05TEST    "
        + bytes.fromhex("77359400")
    )

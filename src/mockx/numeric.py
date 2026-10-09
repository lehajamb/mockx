"""Exact finite-decimal arithmetic for validation and canonical displays.

Matching uses integers. These helpers keep the boundaries independent of the
thread's Decimal context; only the explicitly approximate average uses one.
"""

from __future__ import annotations

from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    MAX_EMAX,
    MIN_EMIN,
    Overflow,
    ROUND_HALF_EVEN,
)
from math import gcd
from typing import Iterable


class NonIntegralUnits(ValueError):
    """A finite value is not an exact multiple of its increment."""


def parse_decimal_value(value: object) -> Decimal:
    """Preserve decimal text exactly, including deterministic parse failures."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value), context=Context(traps=[InvalidOperation]))


def _parts(value: Decimal, *, normalize: bool = False) -> tuple[int, int]:
    if not value.is_finite():
        raise ValueError("Exact decimal arithmetic requires a finite value")
    sign, digits, exponent = value.as_tuple()
    if normalize:
        if value.is_zero():
            return 0, 0
        end = len(digits)
        while digits[end - 1] == 0:
            end -= 1
        exponent += len(digits) - end
        digits = digits[:end]
    # Decimal -> int does not round or use Python's integer-string digit limit.
    coefficient = int(Decimal((sign, digits, 0)))
    return coefficient, exponent


def _from_parts(coefficient: int, exponent: int, *, negative_zero=False) -> Decimal:
    sign, digits, _ = Decimal(coefficient).as_tuple()
    if coefficient == 0 and negative_zero:
        sign = 1
    return Decimal((sign, digits, exponent))


def decimal_scale(value: Decimal) -> int:
    """Count significant fractional places without context normalization."""
    _, exponent = _parts(value, normalize=True)
    return max(0, -exponent)


def is_integral_decimal(value: Decimal) -> bool:
    return decimal_scale(value) == 0


def bounded_units(value: Decimal, increment: Decimal, maximum: int) -> int:
    """Resolve positive exact units, rejecting the grid before the range.

    After removing trailing zeros, a negative exponent difference cannot be
    integral. For a nonnegative difference the reduced denominator must divide
    a power of ten. Factor it before checking the bound so even huge off-grid
    values retain their field-specific rejection, without expanding exponents.
    """
    numerator, value_exponent = _parts(value, normalize=True)
    denominator, increment_exponent = _parts(increment, normalize=True)
    if numerator <= 0 or denominator <= 0 or maximum <= 0:
        raise ValueError("Unit conversion requires positive values and bounds")
    power = value_exponent - increment_exponent
    if power < 0:
        raise NonIntegralUnits()
    common = gcd(numerator, denominator)
    numerator //= common
    denominator //= common
    twos = (denominator & -denominator).bit_length() - 1
    denominator >>= twos
    fives = 0
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1 or power < max(twos, fives):
        raise NonIntegralUnits()
    two_power = power - twos
    five_power = power - fives
    if (
        numerator > maximum
        or two_power >= maximum.bit_length()
        or five_power >= maximum.bit_length()
    ):
        raise OverflowError("Unit count exceeds its bound")
    resolved = numerator * (2 ** two_power) * (5 ** five_power)
    if resolved > maximum:
        raise OverflowError("Unit count exceeds its bound")
    return resolved


def decimal_product(*values: Decimal | int) -> Decimal:
    """Multiply coefficients exactly, preserving the operands' decimal scale."""
    coefficient, exponent = 1, 0
    for value in values:
        part, scale = _parts(value if isinstance(value, Decimal) else Decimal(value))
        coefficient *= part
        exponent += scale
    return _from_parts(coefficient, exponent)


def decimal_sum(values: Iterable[Decimal]) -> Decimal:
    """Add finite decimals exactly, retaining zero and trailing-zero scale."""
    coefficient, exponent = 0, 0
    for value in values:
        part, scale = _parts(value)
        shared_scale = min(exponent, scale)
        coefficient = (
            (coefficient * (10 ** (exponent - shared_scale)) if coefficient else 0)
            + (part * (10 ** (scale - shared_scale)) if part else 0)
        )
        exponent = shared_scale
    return _from_parts(coefficient, exponent)


def format_decimal_ratio(
    numerator: Decimal, denominator: int, precision: int,
) -> str:
    """Round an exact ratio once, half-even, into a fixed-scale string."""
    if denominator <= 0 or precision < 0:
        raise ValueError("Formatting requires a positive divisor and nonnegative precision")
    coefficient, exponent = _parts(numerator)
    power = exponent + precision
    absolute = abs(coefficient)
    if absolute == 0:
        rounded = 0
    elif power < 0 and -power > len(numerator.as_tuple().digits):
        # Below one tenth of a displayed unit, even before division.
        rounded = 0
    else:
        if power >= 0:
            absolute *= 10 ** power
        else:
            denominator *= 10 ** (-power)
        rounded, remainder = divmod(absolute, denominator)
        comparison = 2 * remainder - denominator
        if comparison > 0 or (comparison == 0 and rounded % 2):
            rounded += 1
    if coefficient < 0:
        rounded = -rounded
    value = _from_parts(
        rounded, -precision, negative_zero=numerator.is_signed(),
    )
    return format(value, "f")


def average_decimal(numerator: Decimal, denominator: int) -> Decimal:
    """Derive a Decimal average with explicit 28-significant-digit half-even rounding."""
    context = Context(
        prec=28,
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    return context.divide(numerator, Decimal(denominator))

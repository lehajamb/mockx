"""Exact instrument conversion and display helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from types import MappingProxyType
from typing import Mapping

from .configuration import (
    ExchangeConfig,
    InstrumentConfig,
    MAX_INSTRUMENT_UNITS,
)


class InstrumentConversionError(ValueError):
    """Exact conversion failure with a stable machine-readable reason."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def parse_decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise InstrumentConversionError(
            "INVALID_NUMBER",
            f"{field_name} must be a decimal value",
        )
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InstrumentConversionError(
            "INVALID_NUMBER",
            f"{field_name} must be a decimal value",
        ) from exc
    if not parsed.is_finite():
        raise InstrumentConversionError(
            "NON_FINITE_NUMBER",
            f"{field_name} must be finite",
        )
    return parsed


@dataclass(frozen=True, slots=True)
class Instrument:
    """Immutable, normalized metadata for one matching-engine book."""

    symbol: str
    tick_size: Decimal
    lot_size: Decimal
    price_display_precision: int
    quantity_display_precision: int

    @classmethod
    def from_config(
        cls,
        symbol: str,
        config: InstrumentConfig,
    ) -> "Instrument":
        return cls(
            symbol=symbol,
            tick_size=config.tick_size,
            lot_size=config.lot_size,
            price_display_precision=int(config.price_display_precision or 0),
            quantity_display_precision=int(
                config.quantity_display_precision or 0
            ),
        )

    @staticmethod
    def _units(
        value: Decimal,
        increment: Decimal,
        *,
        field_name: str,
        off_increment_code: str,
    ) -> int:
        units = value / increment
        integral = units.to_integral_value()
        if units != integral:
            raise InstrumentConversionError(
                off_increment_code,
                f"{field_name} must be an exact multiple of {increment}",
            )
        resolved = int(integral)
        if resolved > MAX_INSTRUMENT_UNITS:
            raise InstrumentConversionError(
                "NUMBER_OUT_OF_RANGE",
                f"{field_name} exceeds signed 64-bit unit bounds",
            )
        return resolved

    def quantity_to_lots(self, raw: object) -> int:
        quantity = parse_decimal(raw, field_name="quantity")
        if quantity <= 0:
            raise InstrumentConversionError(
                "NON_POSITIVE_QUANTITY",
                "quantity must be greater than zero",
            )
        return self._units(
            quantity,
            self.lot_size,
            field_name="quantity",
            off_increment_code="OFF_LOT_QUANTITY",
        )

    def price_to_ticks(self, raw: object) -> int:
        price = parse_decimal(raw, field_name="price")
        if price <= 0:
            raise InstrumentConversionError(
                "NON_POSITIVE_PRICE",
                "price must be greater than zero",
            )
        return self._units(
            price,
            self.tick_size,
            field_name="price",
            off_increment_code="OFF_TICK_PRICE",
        )

    def price_decimal(self, price_ticks: int) -> Decimal:
        return self.tick_size * price_ticks

    def quantity_decimal(self, quantity_lots: int) -> Decimal:
        return self.lot_size * quantity_lots

    def notional_decimal(
        self,
        price_ticks: int,
        quantity_lots: int,
    ) -> Decimal:
        return (
            self.tick_size
            * self.lot_size
            * price_ticks
            * quantity_lots
        )

    @staticmethod
    def _format(value: Decimal, precision: int) -> str:
        quantum = Decimal(1).scaleb(-precision)
        return format(value.quantize(quantum, rounding=ROUND_HALF_EVEN), "f")

    def format_price(self, price_ticks: int) -> str:
        return self._format(
            self.price_decimal(price_ticks),
            self.price_display_precision,
        )

    def format_quantity(self, quantity_lots: int) -> str:
        return self._format(
            self.quantity_decimal(quantity_lots),
            self.quantity_display_precision,
        )

    def format_average_price(
        self,
        filled_notional_tick_lots: int,
        filled_qty_lots: int,
    ) -> str:
        if filled_qty_lots == 0:
            return self._format(Decimal(0), self.price_display_precision)
        value = (
            self.tick_size
            * Decimal(filled_notional_tick_lots)
            / Decimal(filled_qty_lots)
        )
        return self._format(value, self.price_display_precision)

    def format_notional(self, tick_lots: int) -> str:
        precision = (
            self.price_display_precision + self.quantity_display_precision
        )
        value = self.tick_size * self.lot_size * tick_lots
        return self._format(value, precision)


class InstrumentCatalog:
    """Immutable symbol-to-instrument lookup."""

    def __init__(self, instruments: Mapping[str, Instrument]):
        if not instruments:
            raise ValueError("InstrumentCatalog requires at least one instrument")
        self._instruments = MappingProxyType(dict(instruments))

    @classmethod
    def from_config(cls, config: ExchangeConfig) -> "InstrumentCatalog":
        return cls(
            {
                symbol: Instrument.from_config(symbol, metadata)
                for symbol, metadata in config.instruments.items()
            }
        )

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._instruments)

    def get(self, symbol: str) -> Instrument | None:
        return self._instruments.get(symbol.strip().upper())

    def require(self, symbol: str) -> Instrument:
        instrument = self.get(symbol)
        if instrument is None:
            raise KeyError(symbol)
        return instrument

    def __iter__(self):
        return iter(self._instruments)

    def __len__(self) -> int:
        return len(self._instruments)

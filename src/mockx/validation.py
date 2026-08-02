"""Central order-input normalization and stable validation failures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Optional

from .instruments import InstrumentCatalog, InstrumentConversionError
from .models import (
    NewOrderCommand,
    OrderType,
    Side,
    TimeInForce,
)


class OrderValidationCode(str, Enum):
    INVALID_NUMBER = "INVALID_NUMBER"
    NON_FINITE_NUMBER = "NON_FINITE_NUMBER"
    NON_POSITIVE_QUANTITY = "NON_POSITIVE_QUANTITY"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    OFF_LOT_QUANTITY = "OFF_LOT_QUANTITY"
    OFF_TICK_PRICE = "OFF_TICK_PRICE"
    NUMBER_OUT_OF_RANGE = "NUMBER_OUT_OF_RANGE"
    PRICE_REQUIRED = "PRICE_REQUIRED"
    PRICE_NOT_ALLOWED = "PRICE_NOT_ALLOWED"
    UNSUPPORTED_ORDER_TYPE = "UNSUPPORTED_ORDER_TYPE"
    UNSUPPORTED_TIF = "UNSUPPORTED_TIF"
    UNSUPPORTED_SIDE = "UNSUPPORTED_SIDE"
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
    INVALID_CL_ORD_ID = "INVALID_CL_ORD_ID"
    DUPLICATE_CL_ORD_ID = "DUPLICATE_CL_ORD_ID"
    INVALID_SESSION_ID = "INVALID_SESSION_ID"
    INVALID_PARTICIPANT_ID = "INVALID_PARTICIPANT_ID"


class OrderValidationError(ValueError):
    """Stable field-specific order rejection."""

    def __init__(
        self,
        code: OrderValidationCode | str,
        field: str,
        message: str,
    ):
        self.code = OrderValidationCode(code)
        self.field = field
        self.message = message
        super().__init__(f"[{self.code.value}] {message}")


@dataclass(frozen=True, slots=True)
class RawOrderInput:
    """Transport-neutral untrusted order values."""

    session_id: object
    participant_id: object
    cl_ord_id: object
    symbol: object
    side: object
    order_type: object
    quantity: object
    price: object = None
    time_in_force: object = None
    session_alias: Optional[str] = None


_VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]{1,64}$")


class OrderNormalizer:
    """The sole conversion boundary from transport values to integer domain."""

    def __init__(
        self,
        instruments: InstrumentCatalog,
        *,
        itch_compatible: bool = False,
    ):
        self.instruments = instruments
        self.itch_compatible = itch_compatible

    @staticmethod
    def _identifier(
        value: object,
        *,
        field: str,
        code: OrderValidationCode,
        max_length: int = 128,
    ) -> str:
        if not isinstance(value, str):
            raise OrderValidationError(code, field, f"{field} must be a string")
        if value != value.strip() or not value or len(value) > max_length:
            raise OrderValidationError(
                code,
                field,
                f"{field} must be nonempty, unpadded, and at most "
                f"{max_length} characters",
            )
        return value

    @staticmethod
    def _cl_ord_id(value: object) -> str:
        if not isinstance(value, str) or not _VISIBLE_ASCII.fullmatch(value):
            raise OrderValidationError(
                OrderValidationCode.INVALID_CL_ORD_ID,
                "cl_ord_id",
                "ClOrdID must contain 1-64 visible ASCII characters",
            )
        return value

    @staticmethod
    def _enum(
        value: object,
        enum_type,
        *,
        field: str,
        code: OrderValidationCode,
        default=None,
    ):
        if value is None and default is not None:
            return default
        if isinstance(value, enum_type):
            return value
        if isinstance(value, str):
            try:
                return enum_type(value.strip().upper())
            except ValueError:
                pass
        raise OrderValidationError(
            code,
            field,
            f"unsupported {field}: {value!r}",
        )

    def normalize(
        self,
        raw: RawOrderInput,
        *,
        submitted_at: datetime,
    ) -> NewOrderCommand:
        cl_ord_id = self._cl_ord_id(raw.cl_ord_id)
        session_id = self._identifier(
            raw.session_id,
            field="session_id",
            code=OrderValidationCode.INVALID_SESSION_ID,
        )
        participant_id = self._identifier(
            raw.participant_id,
            field="participant_id",
            code=OrderValidationCode.INVALID_PARTICIPANT_ID,
            max_length=64,
        )
        if not isinstance(raw.symbol, str) or not raw.symbol.strip():
            raise OrderValidationError(
                OrderValidationCode.UNKNOWN_SYMBOL,
                "symbol",
                "symbol must be a nonempty string",
            )
        symbol = raw.symbol.strip().upper()
        instrument = self.instruments.get(symbol)
        if instrument is None:
            raise OrderValidationError(
                OrderValidationCode.UNKNOWN_SYMBOL,
                "symbol",
                f"unsupported symbol: {symbol}",
            )
        side = self._enum(
            raw.side,
            Side,
            field="side",
            code=OrderValidationCode.UNSUPPORTED_SIDE,
        )
        order_type = self._enum(
            raw.order_type,
            OrderType,
            field="order_type",
            code=OrderValidationCode.UNSUPPORTED_ORDER_TYPE,
        )
        tif = self._enum(
            raw.time_in_force,
            TimeInForce,
            field="time_in_force",
            code=OrderValidationCode.UNSUPPORTED_TIF,
            default=TimeInForce.GTC,
        )
        try:
            quantity_lots = instrument.quantity_to_lots(raw.quantity)
        except InstrumentConversionError as exc:
            raise OrderValidationError(exc.code, "quantity", str(exc)) from exc
        if self.itch_compatible and quantity_lots > 0xFFFFFFFF:
            raise OrderValidationError(
                OrderValidationCode.NUMBER_OUT_OF_RANGE,
                "quantity",
                "quantity exceeds the ITCH unsigned 32-bit share field",
            )

        price_ticks = None
        if order_type == OrderType.LIMIT:
            if raw.price is None:
                raise OrderValidationError(
                    OrderValidationCode.PRICE_REQUIRED,
                    "price",
                    "LIMIT orders require a price",
                )
            try:
                price_ticks = instrument.price_to_ticks(raw.price)
            except InstrumentConversionError as exc:
                raise OrderValidationError(exc.code, "price", str(exc)) from exc
            if self.itch_compatible:
                scaled_tick = instrument.tick_size * 10_000
                wire_price = scaled_tick * price_ticks
                if (
                    wire_price != wire_price.to_integral_value()
                    or wire_price > 0xFFFFFFFF
                ):
                    raise OrderValidationError(
                        OrderValidationCode.NUMBER_OUT_OF_RANGE,
                        "price",
                        "price exceeds the ITCH unsigned 32-bit Price(4) field",
                    )
        elif raw.price is not None:
            raise OrderValidationError(
                OrderValidationCode.PRICE_NOT_ALLOWED,
                "price",
                "MARKET orders must not contain a price",
            )

        return NewOrderCommand(
            cl_ord_id=cl_ord_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            order_qty_lots=quantity_lots,
            price_ticks=price_ticks,
            tif=tif,
            submitted_at=submitted_at,
            session_id=session_id,
            session_alias=raw.session_alias,
            participant_id=participant_id,
        )

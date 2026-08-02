"""ITCH 5.0 binary message encoder for Nasdaq TotalView-ITCH.

All field offsets and lengths are verified against NQTVITCHSpecification.pdf.
All integer fields are big-endian (network byte order).

The 6-byte nanosecond timestamp is packed as bytes manually because Python's
struct module does not have a standard 6-byte integer format.
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone
from decimal import Decimal

from .instruments import InstrumentCatalog
from .models import Side

# ---------------------------------------------------------------------------
# ITCH 5.0 message types (single ASCII byte)
# ---------------------------------------------------------------------------
MSG_SYSTEM_EVENT = b"S"
MSG_STOCK_DIRECTORY = b"R"
MSG_STOCK_TRADING_ACTION = b"H"
MSG_ADD_ORDER_NO_MPID = b"A"
MSG_ADD_ORDER_MPID = b"F"
MSG_ORDER_EXECUTED = b"E"
MSG_ORDER_EXECUTED_WITH_PRICE = b"C"
MSG_ORDER_CANCEL = b"X"
MSG_ORDER_DELETE = b"D"
MSG_ORDER_REPLACE = b"U"
MSG_TRADE_NON_CROSS = b"P"
MSG_CROSS_TRADE = b"Q"
MSG_BROKEN_TRADE = b"B"

# System Event Codes
EVENT_START_OF_MESSAGES = b"O"
EVENT_START_OF_SYSTEM_HOURS = b"S"
EVENT_START_OF_MARKET_HOURS = b"Q"
EVENT_END_OF_MARKET_HOURS = b"M"
EVENT_END_OF_SYSTEM_HOURS = b"E"
EVENT_END_OF_MESSAGES = b"C"

# Trading States
TRADING_STATE_HALTED = b"H"
TRADING_STATE_PAUSED = b"P"
TRADING_STATE_QUOTATION_ONLY = b"Q"
TRADING_STATE_TRADING = b"T"

# Cross Types
CROSS_TYPE_OPENING = b"O"
CROSS_TYPE_CLOSING = b"C"
CROSS_TYPE_IPO_HALTED = b"H"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _pad_ascii(value: str, length: int) -> bytes:
    """Left-justify ASCII and pad on the right with spaces."""
    if not isinstance(value, str):
        raise ValueError("Fixed-width ASCII values must be strings")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"Value {value!r} must contain only ASCII") from exc
    if len(encoded) > length:
        raise ValueError(
            f"ASCII string {value!r} is longer than {length} bytes: "
            f"{len(encoded)} > {length}"
        )
    return encoded.ljust(length, b" ")


def _price_to_itch(price_ticks: int, tick_size: Decimal) -> int:
    """Convert MockX price_ticks to ITCH Price(4) integer.

    ITCH Price(4) has 4 implied decimal places (e.g. 100.5000 → 1005000).
    MockX internal: price = tick_size * price_ticks
    ITCH wire: price * 10^4 as unsigned 32-bit big-endian.
    """
    if not isinstance(price_ticks, int) or isinstance(price_ticks, bool):
        raise ValueError("price_ticks must be an integer")
    scaled_tick = tick_size * Decimal(10_000)
    if scaled_tick != scaled_tick.to_integral_value():
        raise ValueError(
            f"tick size {tick_size} is not exactly representable as ITCH Price(4)"
        )
    value = int(scaled_tick) * price_ticks
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("ITCH Price(4) must fit an unsigned 32-bit integer")
    return value


def _shares_from_lots(qty_lots: int) -> int:
    """Map one MockX integer lot to exactly one ITCH share."""
    if not isinstance(qty_lots, int) or isinstance(qty_lots, bool):
        raise ValueError("qty_lots must be an integer")
    if not 0 <= qty_lots <= 0xFFFFFFFF:
        raise ValueError("ITCH shares must fit an unsigned 32-bit integer")
    return qty_lots


def _uint(value: int, bits: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    if not 0 <= value <= (1 << bits) - 1:
        raise ValueError(f"{field_name} must fit an unsigned {bits}-bit integer")
    return value


def _fixed_bytes(value: bytes, length: int, field_name: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != length:
        raise ValueError(f"{field_name} must be exactly {length} byte(s)")
    if not value.isascii():
        raise ValueError(f"{field_name} must contain only ASCII bytes")
    return value


def _nanos_since_midnight(ts: datetime) -> int:
    """Convert a timezone-aware UTC datetime to nanoseconds since midnight."""
    if ts.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    utc = ts.astimezone(timezone.utc)
    midnight = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = utc - midnight
    total_seconds = delta.days * 86400 + delta.seconds
    total_nanos = total_seconds * 1_000_000_000 + delta.microseconds * 1000
    return total_nanos


def _pack_timestamp(ts: datetime) -> bytes:
    """Pack a datetime as a 6-byte nanosecond ITCH timestamp."""
    return _nanos_since_midnight(ts).to_bytes(6, "big")


class ITCHEncoder:
    """Stateless ITCH 5.0 binary message encoder."""

    def __init__(
        self,
        locate_codes: dict[str, int],
        instruments: InstrumentCatalog,
    ) -> None:
        self._locate_codes = locate_codes
        self._instruments = instruments

    def _locate(self, symbol: str) -> int:
        code = self._locate_codes.get(symbol)
        if code is None:
            raise KeyError(f"No ITCH locate code for symbol: {symbol}")
        return code

    def _tick_size(self, symbol: str) -> Decimal:
        return self._instruments.require(symbol).tick_size

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._locate_codes)

    # ------------------------------------------------------------------
    # 1.1 System Event Message (12 bytes)
    # ------------------------------------------------------------------

    def system_event(
        self,
        event_code: bytes,
        timestamp: datetime,
        tracking_number: int = 0,
    ) -> bytes:
        _uint(tracking_number, 16, "tracking_number")
        _fixed_bytes(event_code, 1, "event_code")
        return (
            MSG_SYSTEM_EVENT
            + struct.pack(">HH", 0, tracking_number)
            + _pack_timestamp(timestamp)
            + event_code
        )

    # ------------------------------------------------------------------
    # 1.2.1 Stock Directory (39 bytes)
    # ------------------------------------------------------------------

    def stock_directory(
        self,
        symbol: str,
        timestamp: datetime,
        *,
        market_category: bytes = b" ",
        financial_status: bytes = b" ",
        round_lot_size: int = 1,
        round_lots_only: bytes = b"N",
        issue_classification: bytes = b"C",
        issue_sub_type: bytes = b"  ",
        authenticity: bytes = b"P",
        short_sale_threshold: bytes = b" ",
        ipo_flag: bytes = b"N",
        luld_tier: bytes = b" ",
        etp_flag: bytes = b"N",
        etp_leverage_factor: int = 0,
        inverse_indicator: bytes = b"N",
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(round_lot_size, 32, "round_lot_size")
        _uint(etp_leverage_factor, 32, "etp_leverage_factor")
        for value, length, name in (
            (market_category, 1, "market_category"),
            (financial_status, 1, "financial_status"),
            (round_lots_only, 1, "round_lots_only"),
            (issue_classification, 1, "issue_classification"),
            (issue_sub_type, 2, "issue_sub_type"),
            (authenticity, 1, "authenticity"),
            (short_sale_threshold, 1, "short_sale_threshold"),
            (ipo_flag, 1, "ipo_flag"),
            (luld_tier, 1, "luld_tier"),
            (etp_flag, 1, "etp_flag"),
            (inverse_indicator, 1, "inverse_indicator"),
        ):
            _fixed_bytes(value, length, name)
        return (
            MSG_STOCK_DIRECTORY
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + _pad_ascii(symbol, 8)
            + market_category
            + financial_status
            + struct.pack(">I", round_lot_size)
            + round_lots_only
            + issue_classification
            + issue_sub_type
            + authenticity
            + short_sale_threshold
            + ipo_flag
            + luld_tier
            + etp_flag
            + struct.pack(">I", etp_leverage_factor)
            + inverse_indicator
        )

    # ------------------------------------------------------------------
    # 1.2.2 Stock Trading Action (25 bytes)
    # ------------------------------------------------------------------

    def stock_trading_action(
        self,
        symbol: str,
        trading_state: bytes,
        timestamp: datetime,
        *,
        reason: bytes = b"    ",
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _fixed_bytes(trading_state, 1, "trading_state")
        _fixed_bytes(reason, 4, "reason")
        return (
            MSG_STOCK_TRADING_ACTION
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + _pad_ascii(symbol, 8)
            + trading_state
            + b" "  # Reserved
            + reason
        )

    # ------------------------------------------------------------------
    # 1.3.1 Add Order – No MPID Attribution (36 bytes)
    # ------------------------------------------------------------------

    def add_order_no_mpid(
        self,
        symbol: str,
        order_ref_num: int,
        side: Side,
        qty_lots: int,
        price_ticks: int,
        timestamp: datetime,
        *,
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(order_ref_num, 64, "order_ref_num")
        tick_size = self._tick_size(symbol)
        shares = _shares_from_lots(qty_lots)
        itch_price = _price_to_itch(price_ticks, tick_size)
        if not isinstance(side, Side):
            raise ValueError("side must be a Side value")
        buy_sell = b"B" if side == Side.BUY else b"S"
        return (
            MSG_ADD_ORDER_NO_MPID
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + struct.pack(">Q", order_ref_num)
            + buy_sell
            + struct.pack(">I", shares)
            + _pad_ascii(symbol, 8)
            + struct.pack(">I", itch_price)
        )

    # ------------------------------------------------------------------
    # 1.3.2 Add Order with MPID Attribution (40 bytes)
    # ------------------------------------------------------------------

    def add_order_mpid(
        self,
        symbol: str,
        order_ref_num: int,
        side: Side,
        qty_lots: int,
        price_ticks: int,
        timestamp: datetime,
        attribution: str,
        *,
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(order_ref_num, 64, "order_ref_num")
        tick_size = self._tick_size(symbol)
        shares = _shares_from_lots(qty_lots)
        itch_price = _price_to_itch(price_ticks, tick_size)
        if not isinstance(side, Side):
            raise ValueError("side must be a Side value")
        buy_sell = b"B" if side == Side.BUY else b"S"
        return (
            MSG_ADD_ORDER_MPID
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + struct.pack(">Q", order_ref_num)
            + buy_sell
            + struct.pack(">I", shares)
            + _pad_ascii(symbol, 8)
            + struct.pack(">I", itch_price)
            + _pad_ascii(attribution, 4)
        )

    # ------------------------------------------------------------------
    # 1.4.1 Order Executed (31 bytes)
    # ------------------------------------------------------------------

    def order_executed(
        self,
        symbol: str,
        order_ref_num: int,
        executed_qty_lots: int,
        match_number: int,
        timestamp: datetime,
        *,
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(order_ref_num, 64, "order_ref_num")
        _uint(match_number, 64, "match_number")
        exec_shares = _shares_from_lots(executed_qty_lots)
        return (
            MSG_ORDER_EXECUTED
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + struct.pack(">Q", order_ref_num)
            + struct.pack(">I", exec_shares)
            + struct.pack(">Q", match_number)
        )

    # ------------------------------------------------------------------
    # 1.4.2 Order Executed With Price (36 bytes)
    # ------------------------------------------------------------------

    def order_executed_with_price(
        self,
        symbol: str,
        order_ref_num: int,
        executed_qty_lots: int,
        match_number: int,
        execution_price_ticks: int,
        is_printable: bool,
        timestamp: datetime,
        *,
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(order_ref_num, 64, "order_ref_num")
        _uint(match_number, 64, "match_number")
        tick_size = self._tick_size(symbol)
        exec_shares = _shares_from_lots(executed_qty_lots)
        itch_price = _price_to_itch(execution_price_ticks, tick_size)
        printable = b"Y" if is_printable else b"N"
        return (
            MSG_ORDER_EXECUTED_WITH_PRICE
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + struct.pack(">Q", order_ref_num)
            + struct.pack(">I", exec_shares)
            + struct.pack(">Q", match_number)
            + printable
            + struct.pack(">I", itch_price)
        )

    # ------------------------------------------------------------------
    # 1.4.3 Order Cancel (23 bytes)
    # ------------------------------------------------------------------

    def order_cancel(
        self,
        symbol: str,
        order_ref_num: int,
        cancelled_qty_lots: int,
        timestamp: datetime,
        *,
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(order_ref_num, 64, "order_ref_num")
        cancelled_shares = _shares_from_lots(cancelled_qty_lots)
        return (
            MSG_ORDER_CANCEL
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + struct.pack(">Q", order_ref_num)
            + struct.pack(">I", cancelled_shares)
        )

    # ------------------------------------------------------------------
    # 1.4.4 Order Delete (19 bytes)
    # ------------------------------------------------------------------

    def order_delete(
        self,
        symbol: str,
        order_ref_num: int,
        timestamp: datetime,
        *,
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(order_ref_num, 64, "order_ref_num")
        return (
            MSG_ORDER_DELETE
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + struct.pack(">Q", order_ref_num)
        )

    # ------------------------------------------------------------------
    # 1.4.5 Order Replace (35 bytes)
    # ------------------------------------------------------------------

    def order_replace(
        self,
        symbol: str,
        orig_order_ref_num: int,
        new_order_ref_num: int,
        new_qty_lots: int,
        new_price_ticks: int,
        timestamp: datetime,
        *,
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(orig_order_ref_num, 64, "orig_order_ref_num")
        _uint(new_order_ref_num, 64, "new_order_ref_num")
        tick_size = self._tick_size(symbol)
        shares = _shares_from_lots(new_qty_lots)
        itch_price = _price_to_itch(new_price_ticks, tick_size)
        return (
            MSG_ORDER_REPLACE
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + struct.pack(">Q", orig_order_ref_num)
            + struct.pack(">Q", new_order_ref_num)
            + struct.pack(">I", shares)
            + struct.pack(">I", itch_price)
        )

    # ------------------------------------------------------------------
    # 1.5.1 Trade Message (Non-Cross) (44 bytes)
    # ------------------------------------------------------------------

    def trade_non_cross(
        self,
        symbol: str,
        qty_lots: int,
        price_ticks: int,
        match_number: int,
        timestamp: datetime,
        *,
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(match_number, 64, "match_number")
        tick_size = self._tick_size(symbol)
        shares = _shares_from_lots(qty_lots)
        itch_price = _price_to_itch(price_ticks, tick_size)
        return (
            MSG_TRADE_NON_CROSS
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + struct.pack(">Q", 0)  # Order Reference Number always 0
            + b"B"  # Buy/Sell Indicator always "B" per 07/14/2014
            + struct.pack(">I", shares)
            + _pad_ascii(symbol, 8)
            + struct.pack(">I", itch_price)
            + struct.pack(">Q", match_number)
        )

    # ------------------------------------------------------------------
    # 1.5.2 Cross Trade (40 bytes)
    # ------------------------------------------------------------------

    def cross_trade(
        self,
        symbol: str,
        qty_lots: int,
        cross_price_ticks: int,
        match_number: int,
        cross_type: bytes,
        timestamp: datetime,
        *,
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(match_number, 64, "match_number")
        _fixed_bytes(cross_type, 1, "cross_type")
        tick_size = self._tick_size(symbol)
        shares = _uint(qty_lots, 64, "shares")
        itch_price = _price_to_itch(cross_price_ticks, tick_size)
        return (
            MSG_CROSS_TRADE
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + struct.pack(">Q", shares)
            + _pad_ascii(symbol, 8)
            + struct.pack(">I", itch_price)
            + struct.pack(">Q", match_number)
            + cross_type
        )

    # ------------------------------------------------------------------
    # 1.5.3 Broken Trade (19 bytes)
    # ------------------------------------------------------------------

    def broken_trade(
        self,
        symbol: str,
        match_number: int,
        timestamp: datetime,
        *,
        tracking_number: int = 0,
    ) -> bytes:
        locate = _uint(self._locate(symbol), 16, "stock_locate")
        _uint(tracking_number, 16, "tracking_number")
        _uint(match_number, 64, "match_number")
        return (
            MSG_BROKEN_TRADE
            + struct.pack(">HH", locate, tracking_number)
            + _pack_timestamp(timestamp)
            + struct.pack(">Q", match_number)
        )

"""GLIMPSE 5.0 point-in-time snapshot message encoding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .itch_encoder import ITCHEncoder, TRADING_STATE_TRADING
from .models import Side


MSG_END_OF_SNAPSHOT = b"G"
MAX_ITCH_SEQUENCE = 0xFFFFFFFFFFFFFFFF


@dataclass(frozen=True, slots=True)
class GlimpseOrderSnapshot:
    order_ref_num: int
    symbol: str
    side: Side
    qty_lots: int
    price_ticks: int

    def __post_init__(self) -> None:
        if self.order_ref_num <= 0:
            raise ValueError("GLIMPSE order reference must be positive")
        if not self.symbol:
            raise ValueError("GLIMPSE order symbol cannot be empty")
        if not isinstance(self.side, Side):
            raise ValueError("GLIMPSE order side must be a Side value")
        if self.qty_lots <= 0 or self.price_ticks <= 0:
            raise ValueError("GLIMPSE resting quantities and prices must be positive")


@dataclass(frozen=True, slots=True)
class GlimpseSnapshot:
    run_generation: int
    timestamp: datetime
    itch_sequence: int
    orders: tuple[GlimpseOrderSnapshot, ...]

    def __post_init__(self) -> None:
        if self.run_generation <= 0:
            raise ValueError("GLIMPSE snapshot requires a positive run generation")
        if self.timestamp.tzinfo is None:
            raise ValueError("GLIMPSE snapshot timestamp must be timezone-aware")
        if not 1 <= self.itch_sequence <= MAX_ITCH_SEQUENCE:
            raise ValueError("ITCH sequence must be between 1 and 2^64-1")


class GlimpseEncoder:
    """Encode one immutable GLIMPSE spin using shared ITCH message layouts."""

    def __init__(
        self,
        itch_encoder: ITCHEncoder,
        *,
        market_category: bytes,
        financial_status: bytes,
        round_lot_size: int,
    ) -> None:
        self.itch_encoder = itch_encoder
        self.market_category = market_category
        self.financial_status = financial_status
        self.round_lot_size = round_lot_size

    @staticmethod
    def end_snapshot(itch_sequence: int) -> bytes:
        if (
            not isinstance(itch_sequence, int)
            or isinstance(itch_sequence, bool)
            or not 1 <= itch_sequence <= MAX_ITCH_SEQUENCE
        ):
            raise ValueError("ITCH sequence must be between 1 and 2^64-1")
        return MSG_END_OF_SNAPSHOT + str(itch_sequence).encode("ascii").rjust(20, b" ")

    def encode(self, snapshot: GlimpseSnapshot) -> tuple[bytes, ...]:
        messages: list[bytes] = []
        tracking_number = 0

        def next_tracking() -> int:
            nonlocal tracking_number
            tracking_number = (tracking_number + 1) & 0xFFFF
            return tracking_number

        for symbol in self.itch_encoder.symbols:
            messages.append(
                self.itch_encoder.stock_directory(
                    symbol,
                    snapshot.timestamp,
                    market_category=self.market_category,
                    financial_status=self.financial_status,
                    round_lot_size=self.round_lot_size,
                    tracking_number=next_tracking(),
                )
            )
        for symbol in self.itch_encoder.symbols:
            messages.append(
                self.itch_encoder.stock_trading_action(
                    symbol,
                    TRADING_STATE_TRADING,
                    snapshot.timestamp,
                    tracking_number=next_tracking(),
                )
            )
        for order in snapshot.orders:
            messages.append(
                self.itch_encoder.add_order_no_mpid(
                    order.symbol,
                    order.order_ref_num,
                    order.side,
                    order.qty_lots,
                    order.price_ticks,
                    snapshot.timestamp,
                    tracking_number=next_tracking(),
                )
            )
        messages.append(self.end_snapshot(snapshot.itch_sequence))
        return tuple(messages)


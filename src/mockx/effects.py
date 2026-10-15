"""Immutable outputs at the validated exchange-command boundary."""

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class CommandEffect:
    kind: str
    payload: object

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("Command effect requires a kind")
        if isinstance(self.payload, Mapping):
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class CommandEffects:
    command_type: str
    timestamp: datetime
    effects: tuple[CommandEffect, ...]
    ingress_sequence: int | None = None
    order_id: int | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.command_type or self.timestamp.tzinfo is None:
            raise ValueError("Command effects require a type and aware timestamp")
        if not isinstance(self.effects, tuple):
            raise TypeError("Command effects must be an immutable tuple")
        if any(not isinstance(effect, CommandEffect) for effect in self.effects):
            raise TypeError("Command effects must contain immutable native effects")


@dataclass(frozen=True, slots=True)
class BookOrderState:
    order_id: int
    symbol: str
    side: object
    price_ticks: int
    qty_lots: int
    liquidity_category: str = "market"
    logical_order_id: int | None = None

    def __post_init__(self) -> None:
        if self.order_id <= 0 or self.price_ticks <= 0 or self.qty_lots <= 0:
            raise ValueError(
                "Checkpoint orders require positive IDs, price and quantity"
            )


@dataclass(frozen=True, slots=True)
class BookCheckpoint:
    run_id: str
    timestamp: datetime
    next_itch_sequence: int
    orders: tuple[BookOrderState, ...]

    def __post_init__(self) -> None:
        if not self.run_id or self.timestamp.tzinfo is None:
            raise ValueError("Checkpoint requires a run and aware timestamp")
        if self.next_itch_sequence < 1 or not isinstance(self.orders, tuple):
            raise ValueError("Checkpoint requires immutable orders and an ITCH cut")

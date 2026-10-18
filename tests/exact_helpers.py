"""Shared exact-arithmetic fixtures for the migrated domain contract."""

from decimal import Decimal
from typing import Any

from src.mockx.configuration import validate_config
from src.mockx.engine import MatchingEngine
from src.mockx.instruments import Instrument, InstrumentCatalog
from src.mockx.metrics import MetricsCollector
from src.mockx.models import (
    CancelOrderCommand,
    NewOrderCommand,
    Order,
    OrderStatus,
    OrderType,
)


TEST_INSTRUMENT = Instrument(
    symbol="TEST",
    tick_size=Decimal("1"),
    lot_size=Decimal("1"),
    price_display_precision=2,
    quantity_display_precision=3,
)
TEST_INSTRUMENTS = InstrumentCatalog({"TEST": TEST_INSTRUMENT})


def exact_command(**values: Any) -> NewOrderCommand:
    """Build an integer-domain command with explicit ownership."""
    order_qty_lots = values.pop("order_qty_lots")
    price_ticks = values.pop("price_ticks", None)
    order_type = values.pop(
        "order_type",
        OrderType.MARKET if price_ticks is None else OrderType.LIMIT,
    )
    return NewOrderCommand(
        order_type=order_type,
        order_qty_lots=order_qty_lots,
        price_ticks=price_ticks,
        participant_id=values.pop("participant_id"),
        **values,
    )


def exact_order(**values: Any) -> Order:
    """Build an integer-domain engine order with explicit ownership."""
    order_qty_lots = values.pop("order_qty_lots")
    price_ticks = values.pop("price_ticks", None)
    values.setdefault("displayed_order_ref", values.get("id"))
    return Order(
        order_type=values.pop(
            "order_type",
            OrderType.MARKET if price_ticks is None else OrderType.LIMIT,
        ),
        order_qty_lots=order_qty_lots,
        price_ticks=price_ticks,
        instrument=values.pop("instrument", TEST_INSTRUMENT),
        filled_qty_lots=values.pop("filled_qty_lots", 0),
        canceled_qty_lots=values.pop("canceled_qty_lots", 0),
        status=values.pop("status", OrderStatus.PENDING_NEW),
        participant_id=values.pop("participant_id"),
        **values,
    )


def exact_cancel_command(**values: Any) -> CancelOrderCommand:
    return CancelOrderCommand(
        order_qty_lots=values.pop("order_qty_lots"),
        **values,
    )


def exact_engine(*args: Any, **values: Any) -> MatchingEngine:
    if len(args) < 3:
        values.setdefault("instruments", TEST_INSTRUMENTS)
    return MatchingEngine(*args, **values)


def exact_metrics(*args: Any, **values: Any) -> MetricsCollector:
    values.setdefault("instruments", TEST_INSTRUMENTS)
    return MetricsCollector(*args, **values)


def test_config(
    *,
    fix_enabled: bool = False,
    scheduler_mode: str = "external",
    reject_prob: float = 0.0,
):
    return validate_config(
        {
            "exchange_name": "TEST",
            "instruments": {
                "TEST": {
                    "tick_size": "1",
                    "lot_size": "1",
                    "price_display_precision": 2,
                    "quantity_display_precision": 3,
                }
            },
            "latency": {"network_ms": 0, "engine_ms": 0},
            "execution": {
                "reject_prob": reject_prob,
                "random_seed": 42,
            },
            "admin_api": {
                "host": "127.0.0.1",
                "port": 8000,
                "enabled": True,
            },
            "fix": {
                "enabled": fix_enabled,
                "session_participants": (
                    {"TEST-SESSION": "participant-test"}
                    if fix_enabled
                    else {}
                ),
            },
            "scheduler": {"mode": scheduler_mode},
            "websocket": {},
            "cors_origins": [],
            "prometheus": {"enabled": True},
        },
        environ={},
    )

from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""Shared deterministic fixtures for API remediation tests."""

from pathlib import Path

from src.mockx.configuration import validate_config
from src.mockx.main import MockExchange
from src.mockx.models import (
    NewOrderCommand,
    OrderType,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)


ROOT = Path(__file__).resolve().parents[1]
FIX_CONFIG = ROOT / "configs" / "mockx1.cfg"


def api_config(
    *,
    fix_enabled: bool = False,
    heartbeat_seconds: float = 10.0,
    queue_capacity: int = 8,
    send_timeout_seconds: float = 5.0,
    heartbeat_stale_seconds: float | None = None,
    market_data_stale_seconds: float | None = None,
    fallback_poll_seconds: float = 2.0,
    reconnect_initial_seconds: float = 1.0,
    reconnect_max_seconds: float = 30.0,
    staleness_check_seconds: float = 1.0,
    prometheus_enabled: bool = True,
) -> dict:
    if heartbeat_stale_seconds is None:
        heartbeat_stale_seconds = max(30.0, heartbeat_seconds * 3)
    if market_data_stale_seconds is None:
        market_data_stale_seconds = max(30.0, heartbeat_seconds * 3)
    return {
        "exchange_name": "Test-Exchange",
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
            "reject_prob": 0.0,
            "random_seed": 42,
        },
        "admin_api": {
            "host": "127.0.0.1",
            "port": 8999,
            "enabled": True,
        },
        "fix": {
            "enabled": fix_enabled,
            "session_participants": {
                "FIX.4.4:MOCKX1->CLIENT1": "participant-1"
            },
        },
        "scheduler": {
            "mode": "external",
            "interval_ms": 10,
            "max_advance_ms": 86_400_000,
            "max_events_per_advance": 100_000,
        },
        "websocket": {
            "heartbeat_interval_seconds": heartbeat_seconds,
            "outbound_queue_capacity": queue_capacity,
            "send_timeout_seconds": send_timeout_seconds,
            "heartbeat_stale_seconds": heartbeat_stale_seconds,
            "market_data_stale_seconds": market_data_stale_seconds,
            "fallback_poll_seconds": fallback_poll_seconds,
            "reconnect_initial_seconds": reconnect_initial_seconds,
            "reconnect_max_seconds": reconnect_max_seconds,
            "staleness_check_seconds": staleness_check_seconds,
        },
        "cors_origins": [],
        "prometheus": {"enabled": prometheus_enabled},
    }


def make_exchange(**config_overrides) -> MockExchange:
    config = api_config(**config_overrides)
    fix_cfg = str(FIX_CONFIG) if config["fix"]["enabled"] else None
    return MockExchange(validate_config(config, environ={}), fix_cfg)


def process_order(
    exchange: MockExchange,
    cl_ord_id: str,
    side: Side,
    qty: int,
    price: int | None,
    *,
    tif: TimeInForce = TimeInForce.GTC,
) -> int:
    order_id = exchange.engine.submit_order(
        exact_command(
            cl_ord_id=cl_ord_id,
            symbol="TEST",
            side=side,
            order_type=(
                OrderType.MARKET if price is None else OrderType.LIMIT
            ),
            order_qty_lots=qty,
            price_ticks=price,
            tif=tif,
            submitted_at=exchange.scheduler.current_time,
            session_id="FIX.4.4:CLIENT->MOCKX",
            session_alias="CLIENT",
            participant_id=f"participant-{cl_ord_id}",
        )
    )
    exchange.engine.process_order(
        ProcessOrderCommand(order_id=order_id, expected_version=0)
    )
    return order_id

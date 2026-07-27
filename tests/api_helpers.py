"""Shared deterministic fixtures for API remediation tests."""

from pathlib import Path

from src.mockx.main import MockExchange
from src.mockx.models import (
    NewOrderCommand,
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
    prometheus_enabled: bool = True,
) -> dict:
    return {
        "exchange_name": "Test-Exchange",
        "symbols": ["TEST"],
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
        "fix": {"enabled": fix_enabled},
        "scheduler": {
            "mode": "external",
            "interval_ms": 10,
            "max_advance_ms": 86_400_000,
            "max_events_per_advance": 100_000,
        },
        "websocket": {
            "heartbeat_interval_seconds": heartbeat_seconds,
            "market_data_stale_seconds": max(5.0, heartbeat_seconds * 3),
            "fallback_poll_seconds": 2,
        },
        "cors_origins": [],
        "prometheus": {"enabled": prometheus_enabled},
    }


def make_exchange(**config_overrides) -> MockExchange:
    config = api_config(**config_overrides)
    fix_cfg = str(FIX_CONFIG) if config["fix"]["enabled"] else None
    return MockExchange(config, fix_cfg)


def process_order(
    exchange: MockExchange,
    cl_ord_id: str,
    side: Side,
    qty: float,
    price: float | None,
    *,
    tif: TimeInForce = TimeInForce.GTC,
) -> int:
    order_id = exchange.engine.submit_order(
        NewOrderCommand(
            cl_ord_id=cl_ord_id,
            symbol="TEST",
            side=side,
            qty=qty,
            price=price,
            tif=tif,
            submitted_at=exchange.scheduler.current_time,
            session_id="FIX.4.4:CLIENT->MOCKX",
            session_alias="CLIENT",
        )
    )
    exchange.engine.process_order(
        ProcessOrderCommand(order_id=order_id, expected_version=0)
    )
    return order_id

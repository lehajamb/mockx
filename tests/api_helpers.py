"""Shared deterministic fixtures for API remediation tests."""

from pathlib import Path
import socket
import pytest_asyncio

from src.mockx.configuration import validate_config
from src.mockx.main import MockExchange
from src.mockx.models import (
    OrderType,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)
from tests.exact_helpers import exact_command


ROOT = Path(__file__).resolve().parents[1]
FIX_CONFIG = ROOT / "configs" / "mockx1.cfg"
_native_exchanges = []


@pytest_asyncio.fixture(autouse=True)
async def cleanup_native_exchanges():
    """Every test owns and closes its mandatory listeners and writer."""
    start = len(_native_exchanges)
    try:
        yield
    finally:
        exchanges = _native_exchanges[start:]
        del _native_exchanges[start:]
        for exchange in reversed(exchanges):
            await exchange.stop_components()


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class MemoryPersistenceSink:
    """Typed persistence test double, exercising the real bounded writer."""

    def __init__(self):
        self.records = []
        self.closed = False

    async def health_check(self):
        return None

    async def write(self, records):
        self.records.extend(records)

    async def close(self):
        self.closed = True

    async def flush(self):
        return None


class NullFIXGateway:
    """Keep real FIX parsing/delivery while avoiding a test network acceptor."""

    def __init__(self, *args, application=None, **kwargs):
        self.application = application
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


def make_native_exchange(config, fix_cfg=None, *, persistence_sink=None, fix_gateway_factory=None):
    exchange = MockExchange(config, fix_cfg or str(FIX_CONFIG),
                        persistence_sink=persistence_sink or MemoryPersistenceSink(),
                        fix_gateway_factory=fix_gateway_factory or NullFIXGateway)
    _native_exchanges.append(exchange)
    return exchange


def api_config(
    *,
    prometheus_enabled: bool = True,
) -> dict:
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
        "questdb": {"url": "http://127.0.0.1:9000", "health_url": "http://127.0.0.1:9000"},
        "moldudp64": {"multicast_port": available_port()},
        "soupbintcp": {"port": available_port(), "credentials": {"TEST": "PASSWORD"}},
        "prometheus": {"enabled": prometheus_enabled},
    }


def make_exchange(**config_overrides) -> MockExchange:
    config = api_config(**config_overrides)
    return make_native_exchange(validate_config(config, environ={}))


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

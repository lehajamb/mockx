from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""Smoke tests for the mock exchange."""

import pytest
import asyncio
from pathlib import Path
from src.mockx.main import MockExchange
from src.mockx.cli import load_config
from src.mockx.configuration import validate_config
from src.mockx.models import NewOrderCommand, Order, OrderStatus, Side, TimeInForce
from datetime import datetime, timezone
from .api_helpers import api_config


FIX_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "mockx1.cfg"

@pytest.fixture
def test_config():
    """Create test configuration."""
    raw = api_config()
    raw["instruments"] = {
        symbol: {
            "tick_size": "0.01",
            "lot_size": "0.001",
            "price_display_precision": 2,
            "quantity_display_precision": 3,
        }
        for symbol in ("BTC-USDT", "ETH-USDT")
    }
    return validate_config(raw, environ={})


@pytest.mark.asyncio
async def test_engine_smoke():
    """Basic smoke test for the engine."""
    ex = MockExchange(validate_config(api_config(), environ={}))
    ex.fix_app.loop = asyncio.get_event_loop()

    # Create a passive buy order and an aggressive sell order
    buy_order = exact_command(
        cl_ord_id="BUY1",
        participant_id="BUY1",
        symbol="TEST",
        side=Side.BUY,
        order_qty_lots=5,
        price_ticks=100,
        tif=TimeInForce.GTC,
        submitted_at=datetime.now(timezone.utc),
    )
    sell_order = exact_command(
        cl_ord_id="SELL1",
        participant_id="SELL1",
        symbol="TEST",
        side=Side.SELL,
        order_qty_lots=10,
        price_ticks=100,
        tif=TimeInForce.GTC,
        submitted_at=datetime.now(timezone.utc),
    )

    # Submit passive order first, then aggressive
    buy_order_id = ex.engine.submit_order(buy_order)
    sell_order_id = ex.engine.submit_order(sell_order)

    # Execute all scheduled events
    await ex.scheduler.run_until_idle()

    buy_order = ex.engine.orders[buy_order_id]
    sell_order = ex.engine.orders[sell_order_id]

    # --- Assertions ---
    # 1. Check the status of both orders
    assert buy_order.status == OrderStatus.FILLED, f"Buy order status was {buy_order.status}"
    assert sell_order.status == OrderStatus.PARTIAL, f"Sell order status was {sell_order.status}"

    # 2. Check filled quantities
    assert buy_order.filled_qty_lots == 5
    assert sell_order.filled_qty_lots == 5

    # 3. Check average fill prices
    assert buy_order.average_fill_price == 100
    assert sell_order.average_fill_price == 100

    # 4. Check the order book state
    book = ex.engine.get_order_book_snapshot("TEST")
    assert book.get_best_bid_ticks() is None, "Book should have no bids"
    assert book.get_best_ask_ticks() == 100, "Book should have a resting ask"
    assert (
        book.asks[0].level_qty_lots == 5
    ), "Resting ask quantity should be 5"
        
def test_config_loading():
    """Test configuration loading."""
    config = validate_config(api_config(), environ={})
    assert config.exchange_name == "Test-Exchange"
    assert tuple(config.instruments) == ("TEST",)
    assert config.execution.random_seed == 42


def test_fix_delivery_retry_config_defaults_and_validation(tmp_path):
    config_path = tmp_path / "mockx.yml"
    config_path.write_text(
        """
exchange_name: Test
instruments:
  TEST: {tick_size: "1", lot_size: "1"}
latency: {network_ms: 0, engine_ms: 0}
execution: {reject_prob: 0.0, random_seed: 42}
admin_api: {host: 127.0.0.1, port: 8999}
""".strip(),
        encoding="utf-8",
    )
    loaded = load_config(str(config_path))
    assert loaded.fix.delivery_retry_interval_ms == 1_000
    assert loaded.fix.delivery_max_attempts_per_cycle == 3

    config_path.write_text(
        """
exchange_name: Test
instruments:
  TEST: {tick_size: "1", lot_size: "1"}
latency: {network_ms: 0, engine_ms: 0}
execution: {reject_prob: 0.0, random_seed: 42}
admin_api: {host: 127.0.0.1, port: 8999}
fix: {delivery_retry_interval_ms: 0}
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="fix.delivery_retry_interval_ms:",
    ):
        load_config(str(config_path))


@pytest.mark.asyncio
async def test_api_endpoints(test_config):
    """Test basic API endpoints."""
    from src.mockx.api import create_app
    from httpx import AsyncClient, ASGITransport
    
    # Create minimal engine
    ex = MockExchange(test_config)
    app = ex.app
    
    # Use ASGITransport to wrap the FastAPI app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Test health endpoint
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        
        # An exchange without an active simulation session is not ready.
        response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        
        # Test symbols endpoint
        response = await client.get("/symbols")
        assert response.status_code == 200
        assert "BTC-USDT" in response.json()["symbols"]
        
        # Test book endpoint
        response = await client.get("/book/BTC-USDT")
        assert response.status_code == 200
        data = response.json()
        assert data["symbol"] == "BTC-USDT"
        assert "bids" in data
        assert "asks" in data


def test_order_book_operations():
    """Test order book operations."""
    from src.mockx.models import OrderBook, Order, Side, TimeInForce
    
    book = OrderBook("TEST")
    
    # Add some orders
    for i in range(5):
        buy_order = exact_order(
            id=i,
            cl_ord_id=f"BUY{i}",
            participant_id=f"BUY{i}",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=10,
            price_ticks=100 - i,
            tif=TimeInForce.GTC,
            ts_submitted=datetime.now(timezone.utc),
            status=OrderStatus.NEW,
        )
        book.add_bid(buy_order)
        
        sell_order = exact_order(
            id=i + 10,
            cl_ord_id=f"SELL{i}",
            participant_id=f"SELL{i}",
            symbol="TEST",
            side=Side.SELL,
            order_qty_lots=10,
            price_ticks=101 + i,
            tif=TimeInForce.GTC,
            ts_submitted=datetime.now(timezone.utc),
            status=OrderStatus.NEW,
        )
        book.add_ask(sell_order)
    
    # Check book state
    assert len(book.bids) == 5
    assert len(book.asks) == 5
    assert book.get_best_bid_ticks() == 100
    assert book.get_best_ask_ticks() == 101
    assert (
        book.get_best_bid_ticks() + book.get_best_ask_ticks()
    ) / 2 == 100.5

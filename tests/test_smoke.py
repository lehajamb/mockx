"""Smoke tests for the mock exchange."""

import pytest
import asyncio
from pathlib import Path
from src.mockx.main import MockExchange
from src.mockx.models import NewOrderCommand, Order, OrderStatus, Side, TimeInForce
from datetime import datetime, timezone


FIX_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "mockx1.cfg"

@pytest.fixture
def test_config():
    """Create test configuration."""
    return {
        "exchange_name": "Test-Exchange",
        "symbols": ["BTC-USDT", "ETH-USDT"],
        "latency": {
            "network_ms": 10,
            "engine_ms": 5
        },
        "execution": {
            "partial_fill_prob": 0.1,
            "reject_prob": 0.01,
            "random_seed": 42
        },
        "admin_api": {
            "port": 8999
        },
        "prometheus": {
            "port": 9199
        }
    }


@pytest.mark.asyncio
async def test_engine_smoke():
    """Basic smoke test for the engine."""
    config = {
        "exchange_name": "Test-Exchange",
        "symbols": ["TEST"],
        "latency": {"network_ms": 0, "engine_ms": 0},
        "execution": {
            "partial_fill_prob": 0.0,
            "reject_prob": 0.0,
            "random_seed": 42
        },
        "admin_api": {"port": 8999},
        "prometheus": {"port": 9199}
    }

    fix_cfg = str(FIX_CONFIG)
    ex = MockExchange(config, fix_cfg)
    ex.fix_gateway.application.loop = asyncio.get_event_loop()

    # Create a passive buy order and an aggressive sell order
    buy_order = NewOrderCommand(
        cl_ord_id="BUY1",
        symbol="TEST",
        side=Side.BUY,
        qty=5,
        price=100,
        tif=TimeInForce.GTC,
        submitted_at=datetime.now(timezone.utc),
    )
    sell_order = NewOrderCommand(
        cl_ord_id="SELL1",
        symbol="TEST",
        side=Side.SELL,
        qty=10,
        price=100,
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
    assert buy_order.filled_qty == 5
    assert sell_order.filled_qty == 5

    # 3. Check average fill prices
    assert buy_order.avg_fill_price == 100
    assert sell_order.avg_fill_price == 100

    # 4. Check the order book state
    book = ex.engine.get_order_book_snapshot("TEST")
    assert book.get_best_bid() is None, "Book should have no bids"
    assert book.get_best_ask() == 100, "Book should have a resting ask"
    assert book.asks[0].qty == 5, "Resting ask quantity should be 5"
        
def test_config_loading():
    """Test configuration loading."""
    # This would normally load from a file
    config = {
        "exchange_name": "Test",
        "symbols": ["TEST"],
        "latency": {"network_ms": 10, "engine_ms": 5},
        "execution": {
            "partial_fill_prob": 0.0,
            "reject_prob": 0.0,
            "random_seed": 42
        },
        "admin_api": {"port": 8999},
        "prometheus": {"port": 9199}
    }
    
    # Validate required fields
    required_fields = ["exchange_name", "symbols", "latency", "execution", "admin_api"]
    for field in required_fields:
        assert field in config


@pytest.mark.asyncio
async def test_api_endpoints(test_config):
    """Test basic API endpoints."""
    from src.mockx.api import create_app
    from httpx import AsyncClient, ASGITransport
    
    # Create minimal engine
    fix_cfg = str(FIX_CONFIG)
    
    # Create exchange without FIX
    ex = MockExchange(test_config, fix_cfg)
    app = create_app(ex.engine, ex.metrics, test_config, ex.fix_gateway.application)
    
    # Use ASGITransport to wrap the FastAPI app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Test health endpoint
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        
        # Test readiness (should be ready since we have symbols)
        response = await client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        
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
        buy_order = Order(
            id=i,
            cl_ord_id=f"BUY{i}",
            symbol="TEST",
            side=Side.BUY,
            qty=10,
            price=100 - i,
            tif=TimeInForce.GTC,
            ts_submitted=datetime.now(timezone.utc)
        )
        book.add_bid(buy_order)
        
        sell_order = Order(
            id=i + 10,
            cl_ord_id=f"SELL{i}",
            symbol="TEST",
            side=Side.SELL,
            qty=10,
            price=101 + i,
            tif=TimeInForce.GTC,
            ts_submitted=datetime.now(timezone.utc)
        )
        book.add_ask(sell_order)
    
    # Check book state
    assert len(book.bids) == 5
    assert len(book.asks) == 5
    assert book.get_best_bid() == 100
    assert book.get_best_ask() == 101
    assert book.get_mid_price() == 100.5

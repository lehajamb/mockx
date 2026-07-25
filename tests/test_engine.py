"""Tests for the matching engine."""

import pytest
from datetime import datetime, timezone

from src.mockx.models import (
    Order, Side, TimeInForce, OrderStatus, ExecutionParams, OrderBook
)
from src.mockx.engine import MatchingEngine
from src.mockx.latency import LatencyScheduler

@pytest.fixture
def exec_params():
    """Create test execution parameters."""
    return ExecutionParams(
        network_latency_ms=10,
        engine_latency_ms=5,
        partial_fill_prob=0.0,
        reject_prob=0.0,
        rand_seed=42
    )

@pytest.fixture
def engine(exec_params):
    """Create test matching engine."""
    scheduler = LatencyScheduler()
    engine = MatchingEngine(exec_params, scheduler)
    engine.add_symbol("TEST")
    # Set a dummy handler so that execution reports are generated
    engine.on_execution_report = lambda report: None
    return engine


def create_order(side: Side, qty: float, price: float = None) -> Order:
    """Helper to create test orders."""
    return Order(
        id=0,
        cl_ord_id=f"TEST_{datetime.now(timezone.utc).timestamp()}",
        symbol="TEST",
        side=side,
        qty=qty,
        price=price,
        tif=TimeInForce.GTC,
        ts_submitted=datetime.now(timezone.utc)
    )


class TestOrderBook:
    """Test order book functionality."""
    
    def test_add_bid(self):
        """Test adding bid orders."""
        book = OrderBook("TEST")
        order1 = create_order(Side.BUY, 10, 100)
        order2 = create_order(Side.BUY, 5, 101)
        order3 = create_order(Side.BUY, 3, 100)
        
        book.add_bid(order1)
        book.add_bid(order2)
        book.add_bid(order3)
        
        # Check ordering (best bid first)
        assert len(book.bids) == 2
        assert book.bids[0].price == 101
        assert book.bids[1].price == 100
        
        # Check quantities
        assert book.bids[0].qty == 5
        assert book.bids[1].qty == 13  # 10 + 3
        
    def test_add_ask(self):
        """Test adding ask orders."""
        book = OrderBook("TEST")
        order1 = create_order(Side.SELL, 10, 100)
        order2 = create_order(Side.SELL, 5, 99)
        order3 = create_order(Side.SELL, 3, 100)
        
        book.add_ask(order1)
        book.add_ask(order2)
        book.add_ask(order3)
        
        # Check ordering (best ask first)
        assert len(book.asks) == 2
        assert book.asks[0].price == 99
        assert book.asks[1].price == 100
        
        # Check quantities
        assert book.asks[0].qty == 5
        assert book.asks[1].qty == 13  # 10 + 3
        
    def test_best_prices(self):
        """Test best bid/ask price retrieval."""
        book = OrderBook("TEST")
        
        # Empty book
        assert book.get_best_bid() is None
        assert book.get_best_ask() is None
        assert book.get_mid_price() is None
        
        # Add orders
        book.add_bid(create_order(Side.BUY, 10, 99))
        book.add_bid(create_order(Side.BUY, 5, 100))
        book.add_ask(create_order(Side.SELL, 10, 101))
        book.add_ask(create_order(Side.SELL, 5, 102))
        
        assert book.get_best_bid() == 100
        assert book.get_best_ask() == 101
        assert book.get_mid_price() == 100.5


class TestMatchingEngine:
    """Test matching engine functionality."""
    
    def test_limit_order_matching(self, engine):
        """Test basic limit order matching."""
        # Add resting sell order
        sell_order = create_order(Side.SELL, 10, 100)
        sell_id = engine.submit_order(sell_order)
        engine.process_order(sell_id)
        
        # Add aggressive buy order
        buy_order = create_order(Side.BUY, 5, 100)
        buy_id = engine.submit_order(buy_order)
        engine.process_order(buy_id)
        
        # Check orders are matched
        assert engine.orders[sell_id].filled_qty == 5
        assert engine.orders[sell_id].status == OrderStatus.PARTIAL
        assert engine.orders[buy_id].filled_qty == 5
        assert engine.orders[buy_id].status == OrderStatus.FILLED
        
    def test_price_time_priority(self, engine):
        """Test price-time priority matching."""
        # Add multiple sell orders
        sell1 = create_order(Side.SELL, 10, 100)
        sell2 = create_order(Side.SELL, 10, 99)  # Better price
        sell3 = create_order(Side.SELL, 10, 100)  # Same price as sell1
        
        id1 = engine.submit_order(sell1)
        id2 = engine.submit_order(sell2)
        id3 = engine.submit_order(sell3)
        
        engine.process_order(id1)
        engine.process_order(id2)
        engine.process_order(id3)
        
        # Add aggressive buy order
        buy = create_order(Side.BUY, 25, 100)
        buy_id = engine.submit_order(buy)
        engine.process_order(buy_id)
        
        # Check matching order
        assert engine.orders[id2].status == OrderStatus.FILLED  # Best price
        assert engine.orders[id1].status == OrderStatus.FILLED  # First at 100
        assert engine.orders[id3].filled_qty == 5    # Partial fill
    
    def test_market_order(self, engine):
        """Test market order execution and resulting slippage from walking the book."""
        # Add liquidity at two different price levels
        sell_100 = create_order(Side.SELL, 10, 100) # Best price
        sell_101 = create_order(Side.SELL, 10, 101) # Second best price
        engine.process_order(engine.submit_order(sell_100))
        engine.process_order(engine.submit_order(sell_101))
        
        # A market buy large enough to consume the first level and part of the second
        market_buy = create_order(Side.BUY, 15, None)
        buy_id = engine.submit_order(market_buy)
        engine.process_order(buy_id)
        
        buy_order = engine.orders[buy_id]
        
        # The order should be fully filled
        assert buy_order.status == OrderStatus.FILLED
        assert buy_order.filled_qty == 15

        # The average price must be higher than the best price, demonstrating slippage
        # Weighted average: ((10 * 100) + (5 * 101)) / 15 = 100.333...
        expected_avg_price = (10 * 100 + 5 * 101) / 15
        
        # Use pytest.approx for float comparison
        assert buy_order.avg_fill_price == pytest.approx(expected_avg_price)
        assert buy_order.avg_fill_price > 100

    def test_ioc_order(self, engine):
        """Test immediate-or-cancel orders."""
        # Add small liquidity
        sell = create_order(Side.SELL, 5, 100)
        sell_id = engine.submit_order(sell)
        engine.process_order(sell_id)
        
        # IOC buy for more than available
        buy = Order(
            id=0,
            cl_ord_id="IOC_TEST",
            symbol="TEST",
            side=Side.BUY,
            qty=10.0,
            price=100.0,
            tif=TimeInForce.IOC,
            ts_submitted=datetime.now(timezone.utc)
        )
        buy_id = engine.submit_order(buy)
        engine.process_order(buy_id)
        
        # Check partial fill and cancellation
        assert engine.orders[buy_id].filled_qty == 5
        assert engine.orders[buy_id].status == OrderStatus.CANCELLED
        
    def test_rejection(self):
        """Test order rejection."""
        exec_params = ExecutionParams(
            network_latency_ms=10,
            engine_latency_ms=5,
            partial_fill_prob=0.0,
            reject_prob=1.0,  # Always reject
            rand_seed=42
        )
        
        scheduler = LatencyScheduler()
        engine = MatchingEngine(exec_params, scheduler)
        engine.add_symbol("TEST")
        
        order = create_order(Side.BUY, 10, 100)
        order_id = engine.submit_order(order)
        engine.process_order(order_id)
        
        assert engine.orders[order_id].status == OrderStatus.REJECTED
        
    def test_partial_fill(self):
        """Test partial fill simulation."""
        exec_params = ExecutionParams(
            network_latency_ms=10,
            engine_latency_ms=5,
            partial_fill_prob=1.0,  # Always partial fill
            reject_prob=0.0,
            rand_seed=42
        )
        
        scheduler = LatencyScheduler()
        engine = MatchingEngine(exec_params, scheduler)
        engine.add_symbol("TEST")
        # Set a dummy handler so that execution reports are generated
        engine.on_execution_report = lambda report: None
        
        # Add liquidity
        sell = create_order(Side.SELL, 10, 100)
        sell_id = engine.submit_order(sell)
        engine.process_order(sell_id)
        
        # Try to buy all
        buy = create_order(Side.BUY, 10, 100)
        buy_id = engine.submit_order(buy)
        engine.process_order(buy_id)
        
        # Check that a partial fill occurred and the order status is correct
        buy_order = engine.orders[buy_id]
        assert buy_order.status == OrderStatus.PARTIAL
        assert buy_order.filled_qty > 0
        assert buy_order.filled_qty < buy_order.qty
        
    def test_cancel_order(self, engine):
        """Test order cancellation."""
        # Add resting order
        order = create_order(Side.BUY, 10, 99)
        order_id = engine.submit_order(order)
        engine.process_order(order_id)
        
        # Cancel it
        engine.cancel_order(order_id)
        assert engine.orders[order_id].status == OrderStatus.CANCELLED
        
        # Can't cancel filled order
        sell = create_order(Side.SELL, 5, 99)
        sell_id = engine.submit_order(sell)
        engine.process_order(sell_id)
        
        buy = create_order(Side.BUY, 5, 99)
        buy_id = engine.submit_order(buy)
        engine.process_order(buy_id)
        
        engine.cancel_order(buy_id)
        # The status should NOT change from FILLED
        assert engine.orders[buy_id].status == OrderStatus.FILLED

    def test_cancel_order_sends_execution_report(self, engine):
        """Test that cancelling an order generates a correct ExecutionReport."""
        # Add a resting order
        order = create_order(Side.BUY, 10, 99)
        order_id = engine.submit_order(order)
        
        # Process the NEW order report
        engine.process_order(order_id)
        
        # Find the NEW execution report in the queue
        new_reports = [
            e.data for e in engine.scheduler.event_queue
            if e.event_type == 'exec_report' and e.data.exec_type == 'NEW'
        ]
        assert len(new_reports) == 1
        report_ack = new_reports[0]
        assert report_ack.exec_type == "NEW"

        # Clear the queue and cancel the order
        engine.scheduler.clear()
        engine.cancel_order(order_id)

        # Check that a CANCELLED ExecutionReport was scheduled
        assert engine.scheduler.get_pending_count() == 1

        # Verify the cancellation report
        report_cancel = engine.scheduler.event_queue[0].data
        assert report_cancel.order_id == order_id
        assert report_cancel.exec_type == "CANCELLED"
        assert report_cancel.ord_status == OrderStatus.CANCELLED

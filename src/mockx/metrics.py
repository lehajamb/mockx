# metrics.py refactored
"""
metrics.py

This module provides classes to collect and record metrics for the mock exchange.
It includes latency tracking, order metrics, trade metrics, fix metrics, and market data updates.
"""


import time
from .models import (
    DualTimestamp,
    ExecutionReport,
    MarketDataUpdate,
    OrderTimingData,
    LiquidityCategory,
    Side,
    Trade,
    liquidity_category_for_session,
)
from .tsdb_writer import TSDBWriter, Point
import logging
from typing import Dict, Tuple, Optional
from datetime import datetime
import math
logger = logging.getLogger(__name__)

class MetricsCollector:
    """Collects and records metrics for the mock exchange."""

    def __init__(self, exchange_name: str, tsdb_writer: Optional[TSDBWriter] = None):
        self.exchange_name = exchange_name
        self.tsdb_writer = tsdb_writer
        self.metrics_buffer = []

    def set_run_id(self, run_id: Optional[str]):
        """Sets the run_id for the TSDBWriter if available."""
        if self.tsdb_writer:
            logger.info(f"Setting TSDB run_id to: {run_id}")
            self.tsdb_writer.run_id = run_id
            self.metrics_buffer = [] # Clear buffer for new run

    def _create_point(self, measurement: str, tags: Dict, fields: Dict, timestamp_ns: int):
        """Helper to create a Point object and add to buffer."""
        if not self.tsdb_writer:
            return
        
        point = Point(measurement)
        if self.tsdb_writer.run_id:
            tags['run_id'] = self.tsdb_writer.run_id
        
        for key, value in tags.items():
            point.tag(key, str(value))
        for key, value in fields.items():
            point.field(key, float(value))
        point.time(timestamp_ns, write_precision='ns')
        
        self.metrics_buffer.append(point)

    def flush_tsdb(self):
        """Flushes the underlying TSDB writer's buffer."""
        if self.tsdb_writer and self.metrics_buffer:
            logger.info(f"Flushing {len(self.metrics_buffer)} buffered metrics to TSDB.")
            # The InfluxDB client library is designed to handle lists of points
            self.tsdb_writer.write_api.write(bucket=self.tsdb_writer.bucket, record=self.metrics_buffer)
            self.metrics_buffer.clear()
            self.tsdb_writer.flush() # Force send the async batch

    def record_latency(self, metric_name: str, value_ms: float, timestamp_ns: int):
        """Record a value for a specific latency histogram."""        
        if self.tsdb_writer:
            self._create_point(
                "latency", {"exchange": self.exchange_name, "type": metric_name}, {"duration_ms": value_ms}, timestamp_ns
            )

    def record_order(self, report: ExecutionReport):
        """Record order metrics solely from an immutable report snapshot."""
        if self.tsdb_writer:
            tags = {
                "exchange": self.exchange_name,
                "symbol": report.symbol,
                "side": report.side.value,
                "status": report.ord_status.value,
                "tif": report.tif.value,
            }
            # Conditionally add tags that might be None
            if report.session_alias:
                tags["session_alias"] = report.session_alias
            if report.reject_reason:
                tags["reject_reason"] = report.reject_reason

            fields = {
                "order_id": report.order_id,
                "qty": report.order_qty,
                "filled_qty": report.cum_qty,
                "canceled_qty": report.canceled_qty,
                "leaves_qty": report.leaves_qty,
                "avg_fill_price": report.avg_px,
                "ts_submitted": int(report.submitted_at.timestamp() * 1_000_000_000),
            }
            # Conditionally add optional fields
            if report.order_price is not None:
                fields["price"] = report.order_price

            self._create_point(
                "orders",
                tags,
                fields,
                int(report.timestamp.timestamp() * 1_000_000_000)
            )

    def record_trade(self, trade: Trade):
        """Record trade metrics with an optimized tag/field schema."""
        if self.tsdb_writer:
            tags = {
                "exchange": self.exchange_name,
                "symbol": trade.symbol,
                "buyer_session_alias": trade.buyer_session_alias,
                "seller_session_alias": trade.seller_session_alias,
                "aggressor_side": trade.aggressor_side.value,
            }
            
            # Add strategy involvement tags for faster querying
            if (
                liquidity_category_for_session(trade.buyer_session_alias)
                == LiquidityCategory.STRATEGY
            ):
                tags["strategy_side"] = "buy"
                tags["has_strategy"] = "true"
            elif (
                liquidity_category_for_session(trade.seller_session_alias)
                == LiquidityCategory.STRATEGY
            ):
                tags["strategy_side"] = "sell"
                tags["has_strategy"] = "true"
            else:
                tags["strategy_side"] = "none"
                tags["has_strategy"] = "false"
                
            fields = {
                "trade_id": trade.id,
                "price": trade.price,
                "qty": trade.qty,
                "volume": trade.volume,
                "buyer_order_id": trade.buyer_order_id,
                "seller_order_id": trade.seller_order_id,
                "aggressor_order_id": trade.aggressor_order_id,
                "passive_order_id": trade.passive_order_id,
                "level": trade.level,
            }
            self._create_point(
                "trades", 
                tags, 
                fields, 
                int(trade.timestamp.timestamp() * 1_000_000_000)
            )
        
    def record_fix_message(self, msg_type: str, direction: str, timestamp_ns: int):
        """Record a FIX message being sent or received."""
        if self.tsdb_writer:
            self._create_point(
                "fix_messages", {"exchange": self.exchange_name, "msg_type": msg_type, "direction": direction}, {"count": 1}, timestamp_ns
            )

    def record_fix_connection(self, count: int, timestamp_ns: int):
        """Record the number of active FIX connections."""
        if self.tsdb_writer:
            self._create_point(
                "fix_connections", {"exchange": self.exchange_name}, {"count": count}, timestamp_ns
            )
        
    def update_book_metrics(self, update: MarketDataUpdate, price_levels: int = 15, tick_size: float = 1.0):
        """
        Processes a unified MarketDataUpdate to log liquidity heatmap metrics only.
        """
        if not self.tsdb_writer:
            return

        if not update.bid_levels or not update.ask_levels:
            return

        timestamp_ns = int(update.timestamp.timestamp() * 1_000_000_000)
        best_bid = update.bid_levels[0].price
        best_ask = update.ask_levels[0].price
        spread = best_ask - best_bid
        mid_price = float((best_bid + best_ask) / 2)

        # Calculate strategy quotes
        strategy_bid = None
        for level in update.bid_levels:  # Bids are sorted high-to-low
            if any(o.is_strategy_order for o in level.orders):
                strategy_bid = level.price
                break  # The first one found IS the best one
        
        strategy_ask = None
        for level in update.ask_levels:  # Asks are sorted low-to-high
            if any(o.is_strategy_order for o in level.orders):
                strategy_ask = level.price
                break  # The first one found IS the best one
                
        fields = {
            "spread": spread,
            "mid_price": mid_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
        }
        if strategy_bid is not None:
            fields["strategy_bid"] = strategy_bid
            fields["strategy_bid_spread"] = strategy_bid - best_bid
        if strategy_ask is not None:
            fields["strategy_ask"] = strategy_ask
            fields["strategy_ask_spread"] = strategy_ask - best_ask
        self._create_point(
            "book",
            {"exchange": self.exchange_name, "symbol": update.symbol},
            fields,
            timestamp_ns
        )

        all_levels = [("bid", update.bid_levels), ("ask", update.ask_levels)]
        for side, levels in all_levels:
            for i, level in enumerate(levels):
                # Create and buffer book depth point
                tags = {
                    "exchange": self.exchange_name,
                    "symbol": update.symbol,
                    "side": side,
                    "level": i
                }
                fields = {
                    "price": level.price,
                    "mid_price": mid_price,
                    "qty": level.qty,
                    "market_qty": level.market_qty,
                    "strategy_qty": level.strategy_qty
                }
                self._create_point(
                    "book_depth",
                    tags,
                    fields,
                    timestamp_ns
                )

    def update_active_orders(self, update: MarketDataUpdate):
        """Update active order counts."""
        symbol = update.symbol
        timestamp_ns = int(update.timestamp.timestamp() * 1_000_000_000)
        buy_count = sum(len(level.orders) for level in update.bid_levels)
        sell_count = sum(len(level.orders) for level in update.ask_levels)
        if self.tsdb_writer:
            self._create_point(
                "active_orders", {"exchange": self.exchange_name, "symbol": symbol, "side": "buy"}, {"count": buy_count}, timestamp_ns
            )
            self._create_point(
                "active_orders", {"exchange": self.exchange_name, "symbol": symbol, "side": "sell"}, {"count": sell_count}, timestamp_ns
            )

    def update_pending_events(self, count: int, timestamp_ns: int):
        """Update pending event count."""
        if self.tsdb_writer:
            self._create_point(
                "pending_events", {"exchange": self.exchange_name}, {"count": count}, timestamp_ns
            )

class LatencyTracker:
    """Tracks and records latency metrics using both simulated and real-world time."""

    def __init__(self, scheduler, metrics_collector: MetricsCollector):
        self.scheduler = scheduler
        self.metrics = metrics_collector
        self.orders: Dict[int, OrderTimingData] = {}
        
        self._start_real_time = time.perf_counter()
        self._start_sim_time = scheduler.current_time
        self._orders_processed_since_drift_check = 0

    def _create_timestamp(self) -> DualTimestamp:
        """Create a dual timestamp with current simulated and real time."""
        return DualTimestamp(simulated=self.scheduler.current_time, real=time.perf_counter_ns())
    
    def _calculate_and_record_latencies(self, name: str, start_ts: DualTimestamp, end_ts: DualTimestamp):
        """Helper to calculate and record simulated and real latencies."""
        if not (start_ts and end_ts):
            return None, None

        sim_latency_ms = (end_ts.simulated - start_ts.simulated).total_seconds() * 1000
        real_latency_ms = (end_ts.real - start_ts.real) / 1_000_000  # Convert ns to ms

        # Use the end timestamp as the timestamp for the latency event
        # For simulated latency, use the simulation clock
        self.metrics.record_latency(name, sim_latency_ms, int(end_ts.simulated.timestamp() * 1e9))
        # For real latency, use the real-world high-resolution clock
        self.metrics.record_latency(f'real_{name}', real_latency_ms, end_ts.real)
        return sim_latency_ms, real_latency_ms

    def order_submitted(self, order_id: int):
        self.orders[order_id] = OrderTimingData(submitted=self._create_timestamp())

    def order_arrived(self, order_id: int):
        if order_id in self.orders:
            self.orders[order_id].arrived = self._create_timestamp()

    def order_processing_start(self, order_id: int):
        if order_id in self.orders:
            order_data = self.orders[order_id]
            order_data.processing_start = self._create_timestamp()
            self._calculate_and_record_latencies(
                'gateway', order_data.arrived, order_data.processing_start
            )

    def order_processing_end(self, order_id: int):
        if order_id in self.orders:
            order_data = self.orders[order_id]
            order_data.processing_end = self._create_timestamp()
            sim_ms, real_ms = self._calculate_and_record_latencies(
                'engine', order_data.processing_start, order_data.processing_end
            )
            if sim_ms is not None and sim_ms > 0:
                ratio = real_ms / sim_ms
                if ratio > 2.0:
                    logger.warning(
                        f"Order {order_id}: Engine processing ratio is high: {ratio:.2f} "
                        f"({real_ms:.2f}ms real vs {sim_ms:.2f}ms sim)"
                    )
            
    def record_e2e_latency(self, order_id: int):
        """
        Record end-to-end latency when the final response is sent.
        This is now the single point of truth for E2E latency and drift calculation.
        """
        if order_id not in self.orders:
            return
            
        order_data = self.orders[order_id]
        order_data.response_sent = self._create_timestamp()
        
        self._calculate_and_record_latencies(
            'order', order_data.submitted, order_data.response_sent
        )
        
        self._orders_processed_since_drift_check += 1
        if self._orders_processed_since_drift_check >= 100:
            self._update_drift_metrics()
            self._orders_processed_since_drift_check = 0
        
    def cleanup_order(self, order_id: int):
        self.orders.pop(order_id, None)

    def _update_drift_metrics(self):
        """Update system performance and drift metrics."""
        real_elapsed = time.perf_counter() - self._start_real_time
        sim_elapsed = (self.scheduler.current_time - self._start_sim_time).total_seconds()
        drift = sim_elapsed - real_elapsed
        
        if drift < -1.0:
            logger.warning(f"System falling behind real-time by {abs(drift):.2f} seconds")

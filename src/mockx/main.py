"""Main application entry point for the mock exchange."""

import asyncio
import logging
import signal
import sys
import time
import os
from typing import Dict, Optional
import uvicorn

from .models import (
    ExecutionParams,
    ExecutionReport,
    InvalidOrderTransition,
    MarketDataUpdate,
    ProcessOrderCommand,
    TERMINAL_ORDER_STATUSES,
    Trade,
)
from .engine import MatchingEngine
from .latency import LatencyScheduler
from .gateway import FIXGateway
from .api import create_app, ExchangeAPI
from .metrics import MetricsCollector, LatencyTracker
from .latency import LatencyScheduler
from .tsdb_writer import TSDBWriter


logger = logging.getLogger(__name__)


class MockExchange:
    """Main mock exchange application."""
    
    def __init__(self, config: Dict, fix_cfg: str):
        self.config = config
        self.fix_cfg = fix_cfg
        self.running = False
        
        # Initialize components
        self._init_components()
        
    def _init_components(self):
        """Initialize all exchange components."""
        # Create execution parameters
        self.exec_params = ExecutionParams(
            network_latency_ms=self.config["latency"]["network_ms"],
            engine_latency_ms=self.config["latency"]["engine_ms"],
            partial_fill_prob=self.config["execution"]["partial_fill_prob"],
            reject_prob=self.config["execution"]["reject_prob"],
            rand_seed=self.config["execution"]["random_seed"]
        )

        # Extract session delays from config
        session_delays = self.config["latency"].get("session_delays", {})

        # Create scheduler with session delays
        self.scheduler = LatencyScheduler(session_delays=session_delays)

        # Create TSDB writer if configured
        self.tsdb_writer = None
        if self.config.get("tsdb"):
            self.tsdb_writer = TSDBWriter(
                url=os.environ.get("INFLUXDB_URL", self.config["tsdb"]["url"]),
                token=os.environ.get("INFLUXDB_TOKEN", self.config["tsdb"]["token"]),
                org=os.environ.get("INFLUXDB_ORG", self.config["tsdb"]["org"]),
                bucket=os.environ.get("INFLUXDB_BUCKET", self.config["tsdb"]["bucket"]),
            )
        
        # Create metrics collector
        self.metrics = MetricsCollector(self.config["exchange_name"], self.tsdb_writer)
        
        # Create latency tracker with metrics reference
        self.latency_tracker = LatencyTracker(self.scheduler, self.metrics)

        # Create matching engine
        self.engine = MatchingEngine(self.exec_params, self.scheduler)
        
        # Add symbols
        for symbol in self.config["symbols"]:
            self.engine.add_symbol(symbol)
                        
        # If not provided, or file does not exist raise exception
        if not self.fix_cfg or not os.path.isfile(self.fix_cfg):
            raise ValueError(f"FIX config file does not exist: {self.fix_cfg}")
                        
        # Create FIX gateway with optional config file
        self.fix_gateway = FIXGateway(self.fix_cfg, self.engine, self.metrics, self.latency_tracker)
            
        # Create API
        self.app = create_app(self.engine, self.metrics, self.config, self.fix_gateway.application)
        self.api = None  # Will be set when we have the API instance
        
        # Wire up callbacks
        self._setup_callbacks()
        
    def _setup_callbacks(self):
        """Setup callbacks between components."""
        # Register scheduler handlers
        self.scheduler.register_handler("order_arrived", self._handle_order_arrived)
        self.scheduler.register_handler("order_process", self._handle_order_process)
        self.scheduler.register_handler("exec_report", self._handle_exec_report)
        
        # Set engine callbacks
        self.engine.on_execution_report = self._on_execution_report
        self.engine.on_market_data_update = self._on_market_data_update
        self.engine.on_trade = self._on_trade
    
    def _handle_order_arrived(self, command: ProcessOrderCommand):
        """Handle order arrival event from scheduler."""
        self.latency_tracker.order_arrived(command.order_id)

    def _handle_order_process(self, command: ProcessOrderCommand):
        """Handle order processing event from scheduler."""
        order_id = command.order_id

        # Mark processing start
        self.latency_tracker.order_processing_start(order_id)

        self.engine.process_order(command)

        # Mark processing complete
        self.latency_tracker.order_processing_end(order_id)
        
    def _handle_exec_report(self, report: ExecutionReport):
        """Handle execution report event from scheduler."""
        # Clean up completed orders
        if report.ord_status in TERMINAL_ORDER_STATUSES:
            self.latency_tracker.cleanup_order(report.order_id)
            
        # Send report via FIX
        if self.fix_gateway:
            self.fix_gateway.process_execution_report(report)

    def _on_execution_report(self, report: ExecutionReport):
        """Record each order-specific report; trade volume is recorded separately."""
        self.metrics.record_order(report)
            
    def _on_market_data_update(self, update: MarketDataUpdate):
        """Callback when market data is updated."""
        # Broadcast to WebSocket clients
        if self.api:
            asyncio.create_task(self.api.broadcast_market_data(update))
        self.metrics.update_book_metrics(update)
        self.metrics.update_active_orders(update)
            
    def _on_trade(self, trade: Trade):
        """Callback when a trade occurs."""
        self.metrics.record_trade(trade)
        
    async def _run_scheduler(self):
        """Run the scheduler in a loop."""
        while self.running:
            try:
                # If there are events, jump time to the next event
                if self.scheduler.get_pending_count() > 0:
                    next_event_time = self.scheduler.get_next_event_time()
                    if next_event_time > self.scheduler.current_time:
                        self.scheduler.set_time(next_event_time)
                
                # Process all events at the current time
                await self.scheduler.process_events()

                # If the queue is empty, sleep to prevent busy-waiting
                if self.scheduler.get_pending_count() == 0:
                    await asyncio.sleep(0.01)
                
            except InvalidOrderTransition:
                logger.exception("Order lifecycle programming error in scheduler")
                raise
            except Exception as e:
                logger.error(f"Scheduler error: {e}", exc_info=True)
                
    async def _run_api(self):
        """Run the FastAPI server."""
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self.config["admin_api"]["port"],
            log_level="warning"
        )
        server = uvicorn.Server(config)
        
        # Store API instance for WebSocket broadcasting
        for route in self.app.routes:
            if hasattr(route, 'endpoint') and hasattr(route.endpoint, '__self__'):
                if isinstance(route.endpoint.__self__, ExchangeAPI):
                    self.api = route.endpoint.__self__
                    break
                    
        await server.serve()
        
    def run(self):
        """Run the exchange."""
        logger.info(f"Starting {self.config['exchange_name']}...")
        
        # Start FIX gateway
        if self.fix_gateway:
            self.fix_gateway.start()
            
        # Setup signal handlers
        def signal_handler(sig, frame):
            logger.info("Received shutdown signal")
            self.stop()
            sys.exit(0)
            
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Run async components
        self.running = True
        
        try:
            # Create event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)            
            self.fix_gateway.application.loop = loop

            # Create tasks
            tasks = [
                # loop.create_task(self._run_scheduler()),
                loop.create_task(self._run_api())
            ]
            
            # Run until complete
            loop.run_until_complete(asyncio.gather(*tasks))
            
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error(f"Runtime error: {e}")
        finally:
            self.stop()
            
    def stop(self):
        """Stop the exchange."""
        logger.info("Stopping exchange...")
        self.running = False
        
        # Stop FIX gateway
        if self.fix_gateway:
            self.fix_gateway.stop()

        # Flush and close TSDB writer
        if self.tsdb_writer:
            self.tsdb_writer.close()
            
        logger.info("Exchange stopped")

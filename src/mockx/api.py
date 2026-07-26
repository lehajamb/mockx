"""REST API and WebSocket endpoints for the mock exchange."""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.responses import Response
from typing import Dict, List, Optional, Set
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import itertools
from datetime import datetime, timezone
from decimal import Decimal
import time

from .models import OrderBook, MarketDataUpdate
from .engine import MatchingEngine
from .metrics import MetricsCollector
from .gateway import FIXApplication
from pydantic import BaseModel


logger = logging.getLogger(__name__)


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal values."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)

class SessionBody(BaseModel):
    run_id: str

class TimeSyncBody(BaseModel):
    timestamp: datetime

class ExchangeAPI:
    """FastAPI application for the exchange."""
    
    def __init__(self, engine: MatchingEngine, metrics: MetricsCollector, config: Dict, fix_app: FIXApplication):
        self.engine = engine
        self.metrics = metrics
        self.config = config
        self.fix_app = fix_app

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            try:
                yield
            finally:
                await self.metrics.close()

        self.app = FastAPI(
            title="MockX Exchange API",
            version="1.0.0",
            lifespan=lifespan,
        )
        self.templates = Jinja2Templates(directory="templates")
        
        # WebSocket connections
        self.websocket_connections: Dict[str, Set[WebSocket]] = {}
        
        # Setup routes
        self._setup_routes()
        
    def _setup_routes(self):
        """Setup all API routes."""
        
        @self.app.get("/")
        async def root():
            return {"exchange": self.config["exchange_name"], "status": "running"}
            
        @self.app.get("/healthz")
        async def health_check():
            """Liveness check."""
            return {"status": "healthy"}
            
        @self.app.get("/readyz")
        async def readiness_check():
            """Readiness check."""
            # Check if engine has symbols loaded
            if not self.engine.books:
                return JSONResponse(
                    status_code=503,
                    content={"status": "not ready", "reason": "No symbols loaded"}
                )
            return {"status": "ready"}
                        
        @self.app.get("/book/{symbol}")
        async def get_order_book(symbol: str):
            """Get order book snapshot with order details."""
            book = self.engine.get_order_book_snapshot(symbol)
            if not book:
                raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")

            bids = list(book.bids)
            asks = list(book.asks)
            
            # Convert to JSON-serializable format with order details
            return {
                "symbol": symbol,
                "timestamp": self.engine.scheduler.current_time,
                "bids": [
                    {
                        "price": float(level.price), 
                        "qty": float(level.qty), 
                    }
                    for level in bids
                ],
                "asks": [
                    {
                        "price": float(level.price), 
                        "qty": float(level.qty), 
                    }
                    for level in asks
                ],
                "best_bid": float(book.get_best_bid()) if book.get_best_bid() else None,
                "best_ask": float(book.get_best_ask()) if book.get_best_ask() else None,
                "mid_price": float(book.get_mid_price()) if book.get_mid_price() else None
            }            
        
        @self.app.get("/symbols")
        async def get_symbols():
            """Get list of available symbols."""
            return {"symbols": list(self.engine.books.keys())}
            
        @self.app.get("/stats")
        async def get_stats():
            """Get a stable JSON-safe process metrics snapshot."""
            return self.metrics.snapshot().to_dict(
                run_id=self.metrics.run_id,
            )

        @self.app.get("/metrics")
        async def get_prometheus_metrics():
            """Expose this application instance's Prometheus registry."""
            try:
                payload = self.metrics.prometheus_payload()
            except RuntimeError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return Response(
                content=payload,
                media_type=CONTENT_TYPE_LATEST,
            )
            
        @self.app.get("/ui/{symbol}", response_class=HTMLResponse)
        async def book_ui(symbol: str):
            """Order book UI."""
            book = self.engine.get_order_book_snapshot(symbol)
            if not book:
                raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")
                
            # Prepare data for template
            context = {
                "request": {"url": {"path": f"/ui/{symbol}"}},  # Mock request object
                "symbol": symbol,
                "bids": [
                    {"price": float(level.price), "qty": float(level.qty), "orders": len(level.orders)}
                    for level in itertools.islice(book.bids, 10)
                ],
                "asks": [
                    {"price": float(level.price), "qty": float(level.qty), "orders": len(level.orders)}
                    for level in itertools.islice(book.asks, 10)
                ],
                "best_bid": float(book.get_best_bid()) if book.get_best_bid() else None,
                "best_ask": float(book.get_best_ask()) if book.get_best_ask() else None,
                "mid_price": float(book.get_mid_price()) if book.get_mid_price() else None,
                "ws_port": self.config["admin_api"]["port"]
            }
            
            return self.templates.TemplateResponse("book.html", context)
            
        @self.app.websocket("/ws/{symbol}")
        async def websocket_endpoint(websocket: WebSocket, symbol: str):
            """WebSocket endpoint for real-time market data."""
            await websocket.accept()
            
            # Add to connection pool
            if symbol not in self.websocket_connections:
                self.websocket_connections[symbol] = set()
            self.websocket_connections[symbol].add(websocket)
            
            try:
                # Send initial snapshot
                book = self.engine.get_order_book_snapshot(symbol)
                if book:
                    snapshot = {
                        "type": "snapshot",
                        "symbol": symbol,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "bids": [
                            {"price": float(level.price), "qty": float(level.qty)}
                            for level in itertools.islice(book.bids, 10)
                        ],
                        "asks": [
                            {"price": float(level.price), "qty": float(level.qty)}
                            for level in itertools.islice(book.asks, 10)
                        ]
                    }
                    await websocket.send_json(snapshot)
                    
                # Keep connection alive
                while True:
                    # Heartbeat
                    await asyncio.sleep(30)
                    await websocket.send_json({"type": "heartbeat"})
                    
            except WebSocketDisconnect:
                # Remove from connection pool
                self.websocket_connections[symbol].discard(websocket)
                if not self.websocket_connections[symbol]:
                    del self.websocket_connections[symbol]
                    
        @self.app.post("/admin/reload-config")
        async def reload_config():
            """Reload configuration (placeholder)."""
            # In a real implementation, this would reload config from file
            return {"status": "Configuration reload not implemented"}

        @self.app.post("/api/v1/session/start")
        async def start_session(session: SessionBody):
            """
            Starts a new session for a backtest run.
            This will first reset the entire exchange state to ensure a clean slate.
            """
            if not await self.metrics.set_run_id_async(session.run_id):
                raise HTTPException(
                    status_code=503,
                    detail="Buffered metrics could not be flushed",
                )

            # Reset engine-owned state only after the previous run is flushed.
            logger.info("--- RESETTING EXCHANGE STATE FOR NEW SESSION ---")
            self.engine.reset()
            self.fix_app.reset()
            self.fix_app.latency_tracker.reset()
            self.metrics.start_new_engine_generation()
            self.metrics.refresh_engine_gauges()
            
            logger.info(f"Session started with run_id: {session.run_id}")
            return {"status": "session_started_and_reset", "run_id": session.run_id}

        @self.app.post("/api/v1/session/end")
        async def end_session():
            """
            Ends the current session and flushes all buffered data.
            """
            if not await self.metrics.flush():
                raise HTTPException(
                    status_code=503,
                    detail="Buffered metrics could not be flushed",
                )
            self.metrics.set_run_id(None)
            
            logger.info("Session ended and data flushed.")
            return {"status": "session_ended_and_flushed"}

        @self.app.post("/admin/advance-time")
        async def advance_time(body: TimeSyncBody):
            """Advances the scheduler's clock to a specific time and processes events."""
            logger.info(f"scheduler current time: {self.engine.scheduler.current_time}")
            target_time = body.timestamp
            if target_time < self.engine.scheduler.current_time:
                return JSONResponse(
                    status_code=400,
                    content={"status": "error", "reason": "Cannot move time backwards"}
                )
            
            # # This is the key: run the scheduler until it reaches the target time
            # # --- SIMPLIFIED LOGIC ---
            # # 1. Set the clock to the target time.
            # self.engine.scheduler.set_time(target_time)
            # logger.info(f"scheduler time set to {self.engine.scheduler.current_time}")

            # # 2. Run until the event queue is empty. This will process all orders
            # #    sent by the backtester, including their latent responses.
            # await self.engine.scheduler.run_until_idle()
            # self.engine.scheduler.run_until()

            await self.engine.scheduler.run_until(target_time)
            logger.info(f"scheduler current time: {self.engine.scheduler.current_time}")
            return {"status": "time_advanced", "current_time": self.engine.scheduler.current_time}

    async def broadcast_market_data(self, update: MarketDataUpdate):
        """Broadcast market data update to WebSocket clients."""
        if update.symbol not in self.websocket_connections:
            return
            
        # Prepare update message
        message = {
            "type": "update",
            "symbol": update.symbol,
            "timestamp": update.timestamp.isoformat(),
            "bids": [
                {"price": level.price, "qty": level.qty}
                for level in update.bid_levels[:10]
            ],
            "asks": [
                {"price": level.price, "qty": level.qty}
                for level in update.ask_levels[:10]
            ]
        }
        
        if update.trades:
            message["trades"] = [
                {
                    "price": float(trade.price),
                    "qty": float(trade.qty),
                    "timestamp": trade.timestamp.isoformat()
                }
                for trade in update.trades
            ]
            
        # Send to all connected clients
        disconnected = []
        for websocket in self.websocket_connections[update.symbol]:
            try:
                await websocket.send_json(message)
            except Exception:
                disconnected.append(websocket)
                
        # Clean up disconnected clients
        for websocket in disconnected:
            self.websocket_connections[update.symbol].discard(websocket)


def create_app(engine: MatchingEngine, metrics: MetricsCollector, config: Dict, fix_app: FIXApplication) -> FastAPI:
    """Create FastAPI application."""
    api = ExchangeAPI(engine, metrics, config, fix_app)
    return api.app

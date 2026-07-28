"""Typed REST API and reliable WebSocket delivery for MockX."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from starlette.responses import Response

from .engine import MatchingEngine
from .gateway import FIXApplication
from .latency import SchedulerFailedError
from .lifecycle import (
    ExchangeLifecycle,
    LifecycleError,
    LifecycleState,
)
from .metrics import MetricsCollector
from .models import MarketDataSnapshot, SchedulerState
from .websocket_delivery import WebSocketConnection, WebSocketRegistry


logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class RootResponse(BaseModel):
    exchange: str
    status: str


class HealthResponse(BaseModel):
    status: Literal["healthy"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded", "not_ready"]
    components: Dict[str, str]


class SymbolsResponse(BaseModel):
    symbols: list[str]


class PriceLevelResponse(BaseModel):
    price: float = Field(description="Price for this level")
    qty: float = Field(description="Total remaining quantity at this price")
    strategy_qty: float = Field(
        description="Quantity attributed to the strategy session category"
    )
    market_qty: float = Field(
        description="Quantity attributed to all other session categories"
    )
    order_count: int = Field(
        description="Number of live FIFO orders at this price"
    )


class TradeResponse(BaseModel):
    trade_id: str
    price: float
    qty: float
    aggressor_side: Literal["BUY", "SELL"]
    timestamp: datetime


class BookSnapshotResponse(BaseModel):
    symbol: str
    sequence: int = Field(
        description="Global per-exchange market-data sequence"
    )
    timestamp: datetime = Field(description="Timezone-aware simulated UTC")
    bids: list[PriceLevelResponse]
    asks: list[PriceLevelResponse]


class MarketDataMessage(BookSnapshotResponse):
    type: Literal["market_data"] = "market_data"
    trades: list[TradeResponse] = Field(default_factory=list)


class HeartbeatMessage(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    symbol: str
    timestamp: datetime
    last_sequence: int


class WebSocketErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


class OrdersStats(BaseModel):
    submitted: int
    accepted: int
    rejected: int
    canceled: int
    filled: int
    partial_fill_events: int
    cancel_requests: int
    cancel_rejects: int
    active: int
    resting: int


class TradesStats(BaseModel):
    count: int
    quantity: float
    notional: float
    canceled_quantity: float


class CancellationStats(BaseModel):
    accepted: int
    unknown_order: int
    already_terminal: int
    wrong_owner: int
    duplicate_request: int
    stale_run_generation: int


class ReportsStats(BaseModel):
    created: int
    delivery_failures: int


class SessionsStats(BaseModel):
    connected: int


class FixDeliveryStats(BaseModel):
    enqueued: int
    queue_depth: int
    pending: int
    due: int
    retry_pending: int
    delivered: int
    permanently_failed: int
    abandoned: int
    sessions_with_backlog: int
    attempts: int
    failures: int
    oldest_pending_age_seconds: Optional[float]


class BookStats(BaseModel):
    levels: int


class LatencyStats(BaseModel):
    average_real_matching_duration_us: Optional[float] = Field(
        description="Mean wall-clock matching duration in microseconds"
    )
    average_real_report_serialization_duration_us: Optional[float] = Field(
        description="Mean wall-clock FIX serialization duration in microseconds"
    )
    average_sim_inbound_latency_ms: Optional[float] = Field(
        description="Mean configured simulated inbound latency in milliseconds"
    )
    average_sim_engine_latency_ms: Optional[float] = Field(
        description="Mean configured simulated engine latency in milliseconds"
    )
    average_sim_outbound_latency_ms: Optional[float] = Field(
        description="Mean configured simulated outbound latency in milliseconds"
    )
    average_sim_end_to_end_latency_ms: Optional[float] = Field(
        description="Mean simulated submit-to-delivery latency in milliseconds"
    )
    average_sim_cancel_inbound_latency_ms: Optional[float] = Field(
        description="Mean simulated cancel receive-to-arrival latency"
    )
    average_sim_cancel_engine_latency_ms: Optional[float] = Field(
        description="Mean simulated cancel arrival-to-report latency"
    )
    average_sim_cancel_outbound_latency_ms: Optional[float] = Field(
        description="Mean simulated cancel report-delivery latency"
    )
    average_sim_cancel_end_to_end_latency_ms: Optional[float] = Field(
        description="Mean simulated cancel receive-to-delivery latency"
    )
    invalid_measurements: int


class PersistenceStats(BaseModel):
    failures: int
    buffered_records: int


SchedulerMetadataValue = str | int | float | bool | None


class SchedulerFailureResponse(BaseModel):
    event_id: str
    event_type: str
    scheduled_timestamp: datetime
    scheduler_sequence: int
    failure_timestamp: datetime
    exception_type: str
    exception_message: str
    retry_attempted: bool
    status: Literal[
        "HANDLER_EXCEPTION",
        "HANDLER_NOT_REGISTERED",
        "EVENT_PROCESSING_LIMIT_EXCEEDED",
    ]
    event_metadata: Dict[str, SchedulerMetadataValue]


class SchedulerStats(BaseModel):
    state: Literal["RUNNING", "FAILED", "STOPPED"]
    pending_events: int
    failure_count: int
    max_events_per_advance: int
    dead_letter: Optional[SchedulerFailureResponse]


class StatsResponse(BaseModel):
    generated_at: datetime
    run_id: Optional[str]
    run_generation: Optional[int]
    orders: OrdersStats
    trades: TradesStats
    cancellations: CancellationStats
    reports: ReportsStats
    sessions: SessionsStats
    fix_delivery: FixDeliveryStats
    book: BookStats
    latency: LatencyStats
    persistence: PersistenceStats
    scheduler: SchedulerStats


class SessionBody(BaseModel):
    """Start one explicitly named simulation session."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    run_id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("run_id", "session_id"),
    )


class SessionStartResponse(BaseModel):
    status: Literal["session_started"]
    session_id: str
    run_generation: int
    starting_time: datetime


class SessionEndResponse(BaseModel):
    status: Literal["session_ended", "no_active_session"]
    session_id: Optional[str]
    run_generation: Optional[int]
    final_stats: Optional[StatsResponse]


class AdvanceTimeRequest(BaseModel):
    """Advance to an absolute timestamp or by a positive millisecond delta."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    target_time: Optional[datetime] = Field(
        default=None,
        validation_alias=AliasChoices("target_time", "timestamp"),
        description="Timezone-aware absolute simulated timestamp",
    )
    milliseconds: Optional[int] = Field(
        default=None,
        gt=0,
        le=86_400_000,
        description="Positive relative advancement in milliseconds",
    )

    @field_validator("target_time")
    @classmethod
    def normalize_target_time(
        cls,
        value: Optional[datetime],
    ) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("target_time must include a timezone offset")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def exactly_one_target(self) -> "AdvanceTimeRequest":
        if (self.target_time is None) == (self.milliseconds is None):
            raise ValueError(
                "Provide exactly one of target_time or milliseconds"
            )
        return self


# Compatibility name used by older callers.
TimeSyncBody = AdvanceTimeRequest


class AdvanceTimeResponse(BaseModel):
    previous_time: datetime
    current_time: datetime
    events_processed: int
    pending_events: int


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _book_payload(snapshot: MarketDataSnapshot) -> dict[str, Any]:
    return {
        "symbol": snapshot.symbol,
        "sequence": snapshot.sequence,
        "timestamp": snapshot.timestamp,
        "bids": [
            {
                "price": level.price,
                "qty": level.qty,
                "strategy_qty": level.strategy_qty,
                "market_qty": level.market_qty,
                "order_count": level.order_count,
            }
            for level in snapshot.bids
        ],
        "asks": [
            {
                "price": level.price,
                "qty": level.qty,
                "strategy_qty": level.strategy_qty,
                "market_qty": level.market_qty,
                "order_count": level.order_count,
            }
            for level in snapshot.asks
        ],
    }


def market_data_message(
    snapshot: MarketDataSnapshot,
) -> MarketDataMessage:
    """Build the testable public WebSocket envelope from one snapshot."""
    return MarketDataMessage(
        type="market_data",
        **_book_payload(snapshot),
        trades=[
            {
                "trade_id": trade.trade_id,
                "price": trade.price,
                "qty": trade.qty,
                "aggressor_side": trade.aggressor_side.value,
                "timestamp": trade.timestamp,
            }
            for trade in snapshot.trades
        ],
    )


class ExchangeAPI:
    """One explicitly owned FastAPI service for one exchange instance."""

    def __init__(
        self,
        engine: MatchingEngine,
        metrics: MetricsCollector,
        config: Dict[str, Any],
        fix_app: FIXApplication,
        *,
        lifecycle_manager: Optional[Any] = None,
        session_lifecycle: Optional[ExchangeLifecycle] = None,
    ) -> None:
        self.engine = engine
        self.scheduler = engine.scheduler
        self.metrics = metrics
        self.config = config
        self.fix_app = fix_app
        self.lifecycle_manager = lifecycle_manager
        self.lifecycle = session_lifecycle

        websocket_config = config.get("websocket", {})

        def _positive_seconds(name: str, default: float) -> float:
            value = websocket_config.get(name, default)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"websocket.{name} must be positive")
            return float(value)

        self.heartbeat_interval_seconds = _positive_seconds(
            "heartbeat_interval_seconds",
            10.0,
        )
        queue_capacity = websocket_config.get("outbound_queue_capacity", 8)
        if (
            not isinstance(queue_capacity, int)
            or isinstance(queue_capacity, bool)
            or queue_capacity <= 0
        ):
            raise ValueError(
                "websocket.outbound_queue_capacity must be a positive integer"
            )
        self.websocket_queue_capacity = queue_capacity
        self.websocket_send_timeout_seconds = _positive_seconds(
            "send_timeout_seconds",
            5.0,
        )
        heartbeat_stale_seconds = _positive_seconds(
            "heartbeat_stale_seconds",
            30.0,
        )
        market_data_stale_seconds = _positive_seconds(
            "market_data_stale_seconds",
            30.0,
        )
        fallback_poll_seconds = _positive_seconds(
            "fallback_poll_seconds",
            2.0,
        )
        reconnect_initial_seconds = _positive_seconds(
            "reconnect_initial_seconds",
            1.0,
        )
        reconnect_max_seconds = _positive_seconds(
            "reconnect_max_seconds",
            30.0,
        )
        staleness_check_seconds = _positive_seconds(
            "staleness_check_seconds",
            1.0,
        )
        if heartbeat_stale_seconds <= self.heartbeat_interval_seconds:
            raise ValueError(
                "websocket.heartbeat_stale_seconds must exceed "
                "heartbeat cadence"
            )
        if market_data_stale_seconds <= self.heartbeat_interval_seconds:
            raise ValueError(
                "websocket.market_data_stale_seconds must exceed "
                "heartbeat cadence"
            )
        if reconnect_max_seconds < reconnect_initial_seconds:
            raise ValueError(
                "websocket.reconnect_max_seconds must be at least "
                "reconnect_initial_seconds"
            )
        self.registry = WebSocketRegistry(
            metrics,
            queue_capacity=self.websocket_queue_capacity,
            send_timeout_seconds=self.websocket_send_timeout_seconds,
        )
        self.websocket_browser_timing = {
            "heartbeatIntervalMs": round(
                self.heartbeat_interval_seconds * 1000
            ),
            "heartbeatStaleMs": round(heartbeat_stale_seconds * 1000),
            "marketDataStaleMs": round(market_data_stale_seconds * 1000),
            "fallbackPollMs": round(fallback_poll_seconds * 1000),
            "reconnectInitialMs": round(reconnect_initial_seconds * 1000),
            "reconnectMaxMs": round(reconnect_max_seconds * 1000),
            "stalenessCheckMs": round(staleness_check_seconds * 1000),
        }

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            app.state.lifecycle_started = True
            app.state.component_status["api"] = "starting"
            try:
                if self.lifecycle_manager is not None:
                    await self.lifecycle_manager.start_components()
                else:
                    self.fix_app.loop = asyncio.get_running_loop()
                    app.state.component_status.update(
                        {
                            "engine": "ready",
                            "scheduler": "waiting_external",
                            "api": "ready",
                            "fix_gateway": "disabled",
                            "metrics": "ready",
                            "persistence": (
                                "ready"
                                if self.metrics.persistence is not None
                                else "disabled"
                            ),
                        }
                    )
                    app.state.accepting_admin = True
                    app.state.ready = True
                yield
            finally:
                app.state.ready = False
                app.state.accepting_admin = False
                app.state.component_status["api"] = "stopping"
                if self.lifecycle_manager is not None:
                    await self.lifecycle_manager.stop_components()
                else:
                    await self.registry.close_all()
                    try:
                        if not await self.metrics.close():
                            logger.error("Metrics close reported a failure")
                    except Exception:
                        logger.exception("Metrics close failed during shutdown")
                    self.fix_app.loop = None
                app.state.component_status["api"] = "stopped"
                app.state.lifecycle_started = False

        self.app = FastAPI(
            title="MockX Exchange API",
            description=(
                "Deterministic simulation control and immutable market data. "
                "Administrative routes are intended for trusted development "
                "networks."
            ),
            version="2.0.0",
            lifespan=lifespan,
        )
        self.app.state.exchange_api = self
        self.app.state.engine = self.engine
        self.app.state.scheduler = self.scheduler
        self.app.state.metrics = self.metrics
        self.app.state.fix_app = self.fix_app
        self.app.state.session_registry = self.fix_app.session_registry
        self.app.state.ready = False
        self.app.state.accepting_admin = False
        self.app.state.lifecycle_started = False
        self.app.state.component_status = {
            "engine": "not_ready",
            "scheduler": "not_ready",
            "api": "not_ready",
            "fix_gateway": "not_ready",
            "metrics": "not_ready",
            "persistence": (
                "not_ready"
                if self.metrics.persistence is not None
                else "disabled"
            ),
        }

        origins = config.get("cors_origins", [])
        if origins:
            self.app.add_middleware(
                CORSMiddleware,
                allow_origins=list(origins),
                allow_credentials=False,
                allow_methods=["GET", "POST"],
                allow_headers=["Content-Type"],
            )

        template_dir = Path(__file__).resolve().parents[2] / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))
        if self.lifecycle is not None:
            self.lifecycle.bind_api(
                close_websockets=self.close_websockets,
                stats_builder=self._stats_response,
            )
        self._setup_routes()

    def _normalized_symbol(self, symbol: str) -> str:
        return symbol.strip().upper()

    def _snapshot_or_404(self, symbol: str) -> MarketDataSnapshot:
        normalized = self._normalized_symbol(symbol)
        snapshot = self.engine.get_market_data_snapshot(normalized)
        if snapshot is None:
            raise _api_error(
                404,
                "UNKNOWN_SYMBOL",
                f"Unknown symbol: {normalized}",
            )
        return snapshot

    def _readiness(self) -> tuple[int, ReadinessResponse]:
        components = dict(self.app.state.component_status)
        if self.lifecycle is not None:
            components["lifecycle"] = self.lifecycle.state.value.lower()
        if self.scheduler.state == SchedulerState.FAILED:
            components["scheduler"] = "failed"
        elif self.scheduler.state == SchedulerState.STOPPED:
            components["scheduler"] = "stopped"
        else:
            components["scheduler"] = (
                "waiting_external"
                if self.config.get("scheduler", {}).get(
                    "mode",
                    "external",
                )
                == "external"
                else "ready"
            )
        persistence_failures = (
            self.metrics.snapshot().metric_sink_failures
        )
        if persistence_failures:
            components["persistence"] = "degraded"
        if not getattr(self.lifecycle_manager, "fix_enabled", False):
            components["fix_delivery"] = "disabled"
        else:
            delivery = self.fix_app.delivery_queue.snapshot()
            components["fix_delivery"] = (
                "degraded"
                if (
                    delivery.due
                    or delivery.retry_pending
                    or delivery.permanently_failed
                )
                else "ready"
            )

        critical_names = ["engine", "scheduler", "api", "metrics"]
        if getattr(self.lifecycle_manager, "fix_enabled", False):
            critical_names.append("fix_gateway")
        ready_values = {"ready", "waiting_external"}
        critical_ready = all(
            components.get(name) in ready_values
            for name in critical_names
        )
        lifecycle_ready = (
            self.lifecycle is None
            or self.lifecycle.state
            in {LifecycleState.STOPPED, LifecycleState.ACTIVE}
        )
        if not self.app.state.ready or not critical_ready or not lifecycle_ready:
            return 503, ReadinessResponse(
                status="not_ready",
                components=components,
            )
        if "degraded" in components.values():
            return 200, ReadinessResponse(
                status="degraded",
                components=components,
            )
        return 200, ReadinessResponse(
            status="ready",
            components=components,
        )

    def _stats_response(self) -> StatsResponse:
        payload = self.metrics.snapshot().to_dict(
            run_id=self.metrics.run_id
        )
        payload["run_generation"] = (
            self.lifecycle.run_generation
            if self.lifecycle is not None and self.lifecycle.run_id is not None
            else None
        )
        payload["scheduler"] = self.scheduler.diagnostics()
        delivery = self.fix_app.delivery_queue.snapshot()
        payload["fix_delivery"] = {
            "enqueued": delivery.enqueued,
            "queue_depth": delivery.queue_depth,
            "pending": delivery.pending,
            "due": delivery.due,
            "retry_pending": delivery.retry_pending,
            "delivered": delivery.delivered,
            "permanently_failed": delivery.permanently_failed,
            "abandoned": delivery.abandoned,
            "sessions_with_backlog": delivery.sessions_with_backlog,
            "attempts": delivery.attempts,
            "failures": delivery.failures,
            "oldest_pending_age_seconds": (
                delivery.oldest_pending_age_seconds
            ),
        }
        return StatsResponse.model_validate(payload)

    @staticmethod
    def _raise_lifecycle_error(exc: LifecycleError) -> None:
        raise _api_error(exc.status_code, exc.code, exc.message) from exc

    def _require_stable_simulation_read(self) -> None:
        if self.lifecycle is None:
            return
        try:
            self.lifecycle.raise_if_transition_or_failed()
        except LifecycleError as exc:
            self._raise_lifecycle_error(exc)

    def _require_admin(self) -> None:
        if not self.app.state.accepting_admin:
            raise _api_error(
                503,
                "ADMIN_UNAVAILABLE",
                "Administrative actions are unavailable during startup or shutdown",
            )

    def _setup_routes(self) -> None:
        @self.app.get("/", response_model=RootResponse)
        async def root() -> RootResponse:
            return RootResponse(
                exchange=self.config["exchange_name"],
                status="running",
            )

        @self.app.get("/healthz", response_model=HealthResponse)
        async def health_check() -> HealthResponse:
            return HealthResponse(status="healthy")

        @self.app.get(
            "/readyz",
            response_model=ReadinessResponse,
            responses={503: {"model": ReadinessResponse}},
        )
        async def readiness_check():
            status_code, body = self._readiness()
            if status_code == 200:
                return body
            return JSONResponse(
                status_code=status_code,
                content=body.model_dump(mode="json"),
            )

        @self.app.get(
            "/book/{symbol}",
            response_model=BookSnapshotResponse,
            responses={404: {"model": ErrorResponse}},
        )
        async def get_order_book(symbol: str) -> BookSnapshotResponse:
            self._require_stable_simulation_read()
            return BookSnapshotResponse.model_validate(
                _book_payload(self._snapshot_or_404(symbol))
            )

        @self.app.get("/symbols", response_model=SymbolsResponse)
        async def get_symbols() -> SymbolsResponse:
            self._require_stable_simulation_read()
            return SymbolsResponse(symbols=list(self.engine.books))

        @self.app.get("/stats", response_model=StatsResponse)
        async def get_stats() -> StatsResponse:
            self._require_stable_simulation_read()
            return self._stats_response()

        @self.app.get("/metrics")
        async def get_prometheus_metrics() -> Response:
            try:
                payload = self.metrics.prometheus_payload()
            except RuntimeError as exc:
                raise _api_error(
                    404,
                    "METRICS_DISABLED",
                    str(exc),
                ) from exc
            return Response(
                content=payload,
                media_type=CONTENT_TYPE_LATEST,
            )

        @self.app.get("/ui/{symbol}", response_class=HTMLResponse)
        async def book_ui(request: Request, symbol: str):
            self._require_stable_simulation_read()
            snapshot = self._snapshot_or_404(symbol)
            best_bid = snapshot.bids[0].price if snapshot.bids else None
            best_ask = snapshot.asks[0].price if snapshot.asks else None
            mid_price = (
                (best_bid + best_ask) / 2
                if best_bid is not None and best_ask is not None
                else None
            )
            return self.templates.TemplateResponse(
                request=request,
                name="book.html",
                context={
                    "symbol": snapshot.symbol,
                    "bids": [
                        {
                            "price": level.price,
                            "qty": level.qty,
                            "orders": level.order_count,
                        }
                        for level in snapshot.bids[:10]
                    ],
                    "asks": [
                        {
                            "price": level.price,
                            "qty": level.qty,
                            "orders": level.order_count,
                        }
                        for level in snapshot.asks[:10]
                    ],
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "mid_price": mid_price,
                    "websocket_timing": self.websocket_browser_timing,
                },
            )

        @self.app.websocket("/ws/{symbol}")
        async def websocket_endpoint(
            websocket: WebSocket,
            symbol: str,
        ) -> None:
            normalized = self._normalized_symbol(symbol)
            if normalized not in self.engine.books:
                await websocket.accept()
                await websocket.send_json(
                    WebSocketErrorMessage(
                        code="UNKNOWN_SYMBOL",
                        message=f"Unknown symbol: {normalized}",
                    ).model_dump(mode="json")
                )
                await websocket.close(code=1008)
                return

            await websocket.accept()
            connection: Optional[WebSocketConnection] = None
            try:
                async def _register_and_snapshot():
                    snapshot = self.engine.get_market_data_snapshot(normalized)
                    if snapshot is None:
                        raise RuntimeError(
                            f"Symbol vanished after validation: {normalized}"
                        )
                    return await self.registry.register(
                        normalized,
                        websocket,
                        market_data_message(snapshot).model_dump(mode="json"),
                        initial_sequence=snapshot.sequence,
                    )

                if self.lifecycle is not None:
                    try:
                        connection = await self.lifecycle.websocket_snapshot(
                            _register_and_snapshot
                        )
                    except LifecycleError as exc:
                        await websocket.send_json(
                            WebSocketErrorMessage(
                                code=exc.code,
                                message=exc.message,
                            ).model_dump(mode="json")
                        )
                        await websocket.close(code=1013)
                        return
                else:
                    connection = await _register_and_snapshot()

                while True:
                    assert connection.sender_task is not None
                    receive_task = asyncio.create_task(websocket.receive_text())
                    done, _ = await asyncio.wait(
                        (receive_task, connection.sender_task),
                        timeout=self.heartbeat_interval_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if connection.sender_task in done:
                        if receive_task.done():
                            try:
                                await receive_task
                            except (
                                WebSocketDisconnect,
                                RuntimeError,
                                ConnectionError,
                                OSError,
                            ):
                                pass
                        else:
                            receive_task.cancel()
                            try:
                                await receive_task
                            except asyncio.CancelledError:
                                pass
                        break
                    if receive_task not in done:
                        receive_task.cancel()
                        try:
                            await receive_task
                        except asyncio.CancelledError:
                            pass
                        heartbeat = HeartbeatMessage(
                            symbol=normalized,
                            timestamp=self.scheduler.current_time,
                            last_sequence=self.engine.market_data_sequence,
                        )
                        self.registry.enqueue_heartbeat(
                            connection,
                            heartbeat.model_dump(mode="json"),
                        )
                        continue
                    try:
                        await receive_task
                    except (
                        WebSocketDisconnect,
                        RuntimeError,
                        ConnectionError,
                        OSError,
                    ):
                        break
            finally:
                if connection is not None:
                    await self.registry.disconnect(connection)

        if self.config.get("admin_api", {}).get("enabled", True):
            self._setup_admin_routes()

    def _setup_admin_routes(self) -> None:
        @self.app.post(
            "/api/v1/session/start",
            response_model=SessionStartResponse,
            responses={
                409: {"model": ErrorResponse},
                503: {"model": ErrorResponse},
            },
        )
        async def start_session(
            session: SessionBody,
        ) -> SessionStartResponse:
            self._require_admin()
            assert self.lifecycle is not None
            try:
                self.lifecycle.raise_if_transition_or_failed()
                result = await self.lifecycle.start_session(session.run_id)
            except LifecycleError as exc:
                self._raise_lifecycle_error(exc)
            return SessionStartResponse(
                status="session_started",
                session_id=result.run_id,
                run_generation=result.run_generation,
                starting_time=result.starting_time,
            )

        @self.app.post(
            "/api/v1/session/end",
            response_model=SessionEndResponse,
            responses={503: {"model": ErrorResponse}},
        )
        async def end_session() -> SessionEndResponse:
            self._require_admin()
            assert self.lifecycle is not None
            try:
                self.lifecycle.raise_if_transition_or_failed()
                result = await self.lifecycle.end_session()
            except LifecycleError as exc:
                self._raise_lifecycle_error(exc)
            return SessionEndResponse(
                status=(
                    "session_ended"
                    if result.run_id is not None
                    else "no_active_session"
                ),
                session_id=result.run_id,
                run_generation=result.run_generation,
                final_stats=result.final_stats,
            )

        @self.app.post(
            "/admin/advance-time",
            response_model=AdvanceTimeResponse,
            responses={
                409: {"model": ErrorResponse},
                503: {"model": ErrorResponse},
            },
        )
        async def advance_time(
            body: AdvanceTimeRequest,
        ) -> AdvanceTimeResponse:
            self._require_admin()
            assert self.lifecycle is not None
            try:
                self.lifecycle.raise_if_transition_or_failed()
            except LifecycleError as exc:
                self._raise_lifecycle_error(exc)
            scheduler_config = self.config.get("scheduler", {})
            if scheduler_config.get("mode", "external") != "external":
                raise _api_error(
                    409,
                    "CLOCK_MODE_CONFLICT",
                    "Manual time advancement requires external scheduler mode",
                )

            async def _advance():
                self.scheduler.ensure_running()
                previous_time = self.scheduler.current_time
                if body.target_time is not None:
                    target_time = body.target_time
                else:
                    max_advance = int(
                        scheduler_config.get(
                            "max_advance_ms",
                            86_400_000,
                        )
                    )
                    milliseconds = int(body.milliseconds or 0)
                    if milliseconds > max_advance:
                        raise _api_error(
                            422,
                            "ADVANCE_TOO_LARGE",
                            (
                                "milliseconds exceeds configured limit "
                                f"{max_advance}"
                            ),
                        )
                    target_time = previous_time + timedelta(
                        milliseconds=milliseconds
                    )

                if target_time < previous_time:
                    raise _api_error(
                        409,
                        "TIME_REVERSAL",
                        "Target time is before current simulated time",
                    )
                events_processed = await self.scheduler.run_until(target_time)
                return previous_time, events_processed

            try:
                previous_time, events_processed = (
                    await self.lifecycle.execute_administrative_advance(
                        _advance
                    )
                )
            except LifecycleError as exc:
                self._raise_lifecycle_error(exc)
            except SchedulerFailedError as exc:
                raise _api_error(
                    503,
                    "SCHEDULER_FAILED",
                    (
                        "Scheduler is failed; end the current simulation "
                        "session and start a new session to reset it"
                    ),
                ) from exc
            return AdvanceTimeResponse(
                previous_time=previous_time,
                current_time=self.scheduler.current_time,
                events_processed=events_processed,
                pending_events=self.scheduler.get_pending_count(),
            )

    async def broadcast_market_data(
        self,
        snapshot: MarketDataSnapshot,
    ) -> None:
        """Deliver one immutable payload once to each current subscriber."""
        async def _broadcast() -> None:
            message = market_data_message(snapshot).model_dump(mode="json")
            await self.registry.enqueue_market_data(
                snapshot.symbol,
                message,
                sequence=snapshot.sequence,
            )

        if self.lifecycle is not None:
            await self.lifecycle.broadcast_if_current(
                snapshot.run_generation,
                _broadcast,
            )
        else:
            await _broadcast()

    async def close_websockets(self) -> None:
        await self.registry.close_all()


def create_app(
    engine: MatchingEngine,
    metrics: MetricsCollector,
    config: Dict[str, Any],
    fix_app: FIXApplication,
) -> FastAPI:
    """Compatibility factory for already initialized in-process components.

    Production ownership uses ``MockExchange.api`` and the ASGI lifespan.
    Historical callers of this factory supplied already-live components and
    used transports that do not run lifespan events, so this compatibility
    path marks only that standalone API facade ready.
    """
    lifecycle = fix_app.lifecycle
    if lifecycle is None:
        lifecycle = ExchangeLifecycle(
            engine,
            metrics,
            fix_app.latency_tracker,
            fix_app,
        )
        fix_app.lifecycle = lifecycle
    api = ExchangeAPI(
        engine,
        metrics,
        config,
        fix_app,
        session_lifecycle=lifecycle,
    )
    api.app.state.component_status.update(
        {
            "engine": "ready",
            "scheduler": "waiting_external",
            "api": "ready",
            "fix_gateway": "disabled",
            "metrics": "ready",
            "persistence": (
                "ready"
                if metrics.persistence is not None
                else "disabled"
            ),
        }
    )
    api.app.state.lifecycle_started = True
    api.app.state.accepting_admin = True
    api.app.state.ready = True
    return api.app

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
from fastapi.exceptions import RequestValidationError
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

from .configuration import ExchangeConfig
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
from .numeric import format_decimal_ratio
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
    core_ready: bool
    traffic_ready: bool
    persistence_required: bool
    components: Dict[str, str]


class SymbolsResponse(BaseModel):
    symbols: list[str]


class PriceLevelResponse(BaseModel):
    price: str = Field(description="Canonical decimal price")
    qty: str = Field(description="Canonical decimal remaining quantity")
    strategy_qty: str = Field(
        description="Quantity attributed to the strategy session category"
    )
    market_qty: str = Field(
        description="Quantity attributed to all other session categories"
    )
    order_count: int = Field(description="Number of live FIFO orders at this price")


class TradeResponse(BaseModel):
    trade_id: str
    price: str
    qty: str
    aggressor_side: Literal["BUY", "SELL"]
    timestamp: datetime


class BookSnapshotResponse(BaseModel):
    symbol: str
    sequence: int = Field(description="Global per-exchange market-data sequence")
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
    replace_requests: int
    replace_rejects: int
    active: int
    resting: int


class TradesStats(BaseModel):
    count: int
    quantity: str
    notional: str
    canceled_quantity: str


class CancellationStats(BaseModel):
    accepted: int
    unknown_order: int
    already_terminal: int
    wrong_owner: int
    duplicate_request: int
    stale_run_generation: int
    exchange_unavailable: int


class ReplacementStats(BaseModel):
    accepted: int
    unknown_order: int
    already_terminal: int
    wrong_owner: int
    duplicate_request: int
    stale_run_generation: int
    stale_order_reference: int
    invalid_request: int
    exchange_unavailable: int


class ReportsStats(BaseModel):
    created: int
    delivery_failures: int


class SessionsStats(BaseModel):
    connected: int


class FixDeliveryStats(BaseModel):
    scope: Literal["process_lifetime"] = "process_lifetime"
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
    retries: int
    failures: int
    retained_delivered_records: int
    oldest_pending_age_seconds: Optional[float]


class BookStats(BaseModel):
    levels: int


class LatencyStats(BaseModel):
    average_match_loop_duration_us: Optional[float] = Field(
        description="Mean monotonic match-loop duration in microseconds"
    )
    average_book_commit_duration_us: Optional[float] = Field(
        description="Mean authoritative book-mutation duration in microseconds"
    )
    average_engine_command_duration_us: Optional[float] = Field(
        description="Mean complete engine-command duration in microseconds"
    )
    average_report_creation_duration_us: Optional[float] = Field(
        description="Mean immutable report-construction duration in microseconds"
    )
    average_serialization_duration_us: Optional[float] = Field(
        description="Mean outbound FIX construction duration in microseconds"
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
    average_sim_replace_inbound_latency_ms: Optional[float] = Field(
        description="Mean simulated replacement receive-to-arrival latency"
    )
    average_sim_replace_engine_latency_ms: Optional[float] = Field(
        description="Mean simulated replacement arrival-to-report latency"
    )
    average_sim_replace_outbound_latency_ms: Optional[float] = Field(
        description="Mean simulated replacement report-delivery latency"
    )
    average_sim_replace_end_to_end_latency_ms: Optional[float] = Field(
        description="Mean simulated replacement receive-to-delivery latency"
    )
    invalid_measurements: int


class PersistenceRunOutcomes(BaseModel):
    accepted_records: int
    flush_required_records: int
    dropped_records: int
    rejected_closed_records: int
    rejected_full_records: int
    failed_records: int
    failures: int
    integrity_complete: bool


class RunFailureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: Literal["METRICS_INCOMPLETE"]
    message: str
    run_id: str
    run_generation: int
    failed_at: datetime
    reason: str
    metric_event_sequence: Optional[int]


class PersistenceStats(BaseModel):
    scope: Literal["operational"] = "operational"
    required: bool
    state: Literal[
        "DISABLED",
        "HEALTHY",
        "RETRYING",
        "DEGRADED",
        "UNAVAILABLE",
        "CLOSED",
    ]
    last_successful_write_time: Optional[datetime]
    last_failure_time: Optional[datetime]
    consecutive_failures: int
    lifetime_failures: int
    buffered_records: int
    buffered_bytes: int
    oldest_buffered_record_age_seconds: Optional[float]
    accepted_records: int
    flush_required_records: int
    dropped_records: int
    rejected_closed_records: int
    rejected_full_records: int
    failed_records: int
    worker_running: bool
    close_outcome: Optional[str]
    unflushed_on_close: int
    current_run_outcomes: PersistenceRunOutcomes


class RunPersistenceStats(PersistenceRunOutcomes):
    scope: Literal["current_run"] = "current_run"


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
    scope: Literal["current_run"] = "current_run"
    state: Literal["RUNNING", "FAILED", "STOPPED"]
    pending_events: int
    failure_count: int
    max_events_per_advance: int
    dead_letter: Optional[SchedulerFailureResponse]
    dead_letter_count: int = 0
    event_processing_limit_failures: int = 0


class WebSocketStats(BaseModel):
    scope: Literal["process_lifetime"] = "process_lifetime"
    active_clients: int
    queue_depth: int
    snapshots_conflated: int
    snapshots_dropped: int
    slow_client_disconnects: int
    send_failures: int
    send_successes: int


class ITCHStats(BaseModel):
    scope: Literal["process_lifetime"] = "process_lifetime"
    enabled: bool
    state: Literal["disabled", "idle", "ready", "degraded", "stopped"]
    active_session: Optional[str]
    next_sequence_number: int
    sessions_started: int
    sessions_ended: int
    messages_sent: int
    packets_sent: int
    heartbeats_sent: int
    failures: int
    last_error: Optional[str]


class GlimpseStats(BaseModel):
    scope: Literal["process_lifetime"] = "process_lifetime"
    enabled: bool
    state: Literal["disabled", "idle", "ready", "degraded", "stopped"]
    active_connections: int
    cached_sessions: int
    cached_bytes: int
    connections_accepted: int
    fresh_sessions: int
    resumed_sessions: int
    completed_sessions: int
    rejected_sessions: int
    sequenced_messages: int
    sequenced_bytes: int
    heartbeats_sent: int
    timeouts: int
    failures: int
    last_error: Optional[str]


class RunStatsResponse(BaseModel):
    generated_at: datetime
    scope: Literal["current_run"] = "current_run"
    run_active: bool
    run_id: Optional[str]
    run_generation: Optional[int]
    run_failure: Optional[RunFailureResponse]
    orders: OrdersStats
    trades: TradesStats
    cancellations: CancellationStats
    replacements: ReplacementStats
    reports: ReportsStats
    book: BookStats
    latency: LatencyStats
    persistence: RunPersistenceStats
    scheduler: SchedulerStats


class StatsResponse(BaseModel):
    generated_at: datetime
    scope: Literal["current_run"] = "current_run"
    run_active: bool
    run_id: Optional[str]
    run_generation: Optional[int]
    run_failure: Optional[RunFailureResponse]
    orders: OrdersStats
    trades: TradesStats
    cancellations: CancellationStats
    replacements: ReplacementStats
    reports: ReportsStats
    sessions: SessionsStats
    fix_delivery: FixDeliveryStats
    websocket: WebSocketStats
    itch: ITCHStats
    glimpse: GlimpseStats
    book: BookStats
    latency: LatencyStats
    persistence: PersistenceStats
    scheduler: SchedulerStats
    process_lifetime: Dict[str, object]


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
    final_stats: Optional[RunStatsResponse]


class SessionFailedResponse(BaseModel):
    status: Literal["session_failed"] = "session_failed"
    detail: ErrorDetail
    session_id: str
    run_generation: int
    final_stats: RunStatsResponse


class RunAdvanceFailedResponse(ErrorResponse):
    run_failure: RunFailureResponse
    current_time: datetime
    events_processed: int
    pending_events: int


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
            raise ValueError("Provide exactly one of target_time or milliseconds")
        return self


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
    instrument = snapshot.instrument
    return {
        "symbol": snapshot.symbol,
        "sequence": snapshot.sequence,
        "timestamp": snapshot.timestamp,
        "bids": [
            {
                "price": instrument.format_price(level.price_ticks),
                "qty": instrument.format_quantity(level.level_qty_lots),
                "strategy_qty": instrument.format_quantity(level.strategy_qty_lots),
                "market_qty": instrument.format_quantity(level.market_qty_lots),
                "order_count": level.order_count,
            }
            for level in snapshot.bids
        ],
        "asks": [
            {
                "price": instrument.format_price(level.price_ticks),
                "qty": instrument.format_quantity(level.level_qty_lots),
                "strategy_qty": instrument.format_quantity(level.strategy_qty_lots),
                "market_qty": instrument.format_quantity(level.market_qty_lots),
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
                "price": snapshot.instrument.format_price(trade.price_ticks),
                "qty": snapshot.instrument.format_quantity(trade.qty_lots),
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
        config: ExchangeConfig,
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

        websocket_config = config.websocket
        self.heartbeat_interval_seconds = websocket_config.heartbeat_interval_seconds
        self.websocket_queue_capacity = websocket_config.outbound_queue_capacity
        self.websocket_send_timeout_seconds = websocket_config.send_timeout_seconds
        heartbeat_stale_seconds = websocket_config.heartbeat_stale_seconds
        market_data_stale_seconds = websocket_config.market_data_stale_seconds
        fallback_poll_seconds = websocket_config.fallback_poll_seconds
        reconnect_initial_seconds = websocket_config.reconnect_initial_seconds
        reconnect_max_seconds = websocket_config.reconnect_max_seconds
        staleness_check_seconds = websocket_config.staleness_check_seconds
        self.registry = WebSocketRegistry(
            metrics,
            queue_capacity=self.websocket_queue_capacity,
            send_timeout_seconds=self.websocket_send_timeout_seconds,
        )
        self.websocket_browser_timing = {
            "heartbeatIntervalMs": round(self.heartbeat_interval_seconds * 1000),
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

        @self.app.exception_handler(RequestValidationError)
        async def request_validation_error_handler(
            request: Request,
            exc: RequestValidationError,
        ) -> JSONResponse:
            del request
            issues = sorted(
                (
                    ".".join(str(part) for part in error["loc"]),
                    error["msg"],
                )
                for error in exc.errors()
            )
            message = "; ".join(
                f"{path}: {description}" for path, description in issues
            )
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "REQUEST_VALIDATION_ERROR",
                        "message": message,
                    }
                },
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
        itch_feed = getattr(lifecycle_manager, "itch_feed", None)
        glimpse_service = getattr(lifecycle_manager, "glimpse_service", None)
        self.app.state.component_status = {
            "engine": "not_ready",
            "scheduler": "not_ready",
            "api": "not_ready",
            "fix_gateway": "not_ready",
            "metrics": "not_ready",
            "persistence": (
                "not_ready" if self.metrics.persistence is not None else "disabled"
            ),
            "itch": (itch_feed.state.value if itch_feed is not None else "disabled"),
            "glimpse": (
                glimpse_service.state.value
                if glimpse_service is not None
                else "disabled"
            ),
        }

        origins = config.cors_origins
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
                stats_builder=self._final_run_stats_response,
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
                if self.config.scheduler.mode == "external"
                else "ready"
            )
        persistence_health = self.metrics.persistence_health_snapshot()
        components["persistence"] = persistence_health.state.value.lower()
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
        core_ready = all(
            components.get(name) in ready_values for name in critical_names
        )
        lifecycle_ready = self.lifecycle is None or self.lifecycle.state in {
            LifecycleState.STOPPED,
            LifecycleState.ACTIVE,
        }
        core_ready = bool(self.app.state.ready and core_ready and lifecycle_ready)
        persistence_required = bool(
            self.config.questdb is not None and self.config.questdb.enabled
        )
        persistence_ready = (
            not persistence_required or persistence_health.state.value == "HEALTHY"
        )
        traffic_ready = core_ready and persistence_ready
        if not traffic_ready:
            return 503, ReadinessResponse(
                status="not_ready",
                core_ready=core_ready,
                traffic_ready=False,
                persistence_required=persistence_required,
                components=components,
            )
        if "degraded" in components.values():
            return 200, ReadinessResponse(
                status="degraded",
                core_ready=True,
                traffic_ready=True,
                persistence_required=persistence_required,
                components=components,
            )
        return 200, ReadinessResponse(
            status="ready",
            core_ready=True,
            traffic_ready=True,
            persistence_required=persistence_required,
            components=components,
        )

    def _scheduler_stats_payload(self) -> Dict[str, object]:
        payload = self.scheduler.diagnostics()
        payload["scope"] = "current_run"
        payload.update(self.metrics.current_run_scheduler_snapshot())
        return payload

    def _run_stats_payload(self, *, run_active: bool) -> Dict[str, object]:
        payload = self.metrics.snapshot().to_dict(run_id=self.metrics.run_id)
        payload["scope"] = "current_run"
        payload["run_active"] = run_active
        payload["run_failure"] = (
            self.lifecycle.run_failure if self.lifecycle is not None else None
        )
        payload["run_generation"] = (
            self.lifecycle.run_generation
            if self.lifecycle is not None and self.lifecycle.run_id is not None
            else None
        )
        payload["scheduler"] = self._scheduler_stats_payload()
        payload["persistence"] = {
            "scope": "current_run",
            **self.metrics.run_persistence_snapshot(),
        }
        return payload

    def _final_run_stats_response(self) -> RunStatsResponse:
        return RunStatsResponse.model_validate(
            self._run_stats_payload(run_active=False)
        )

    def _stats_response(self) -> StatsResponse:
        payload = self._run_stats_payload(
            run_active=(
                self.lifecycle.state == LifecycleState.ACTIVE
                if self.lifecycle is not None else self.metrics.run_id is not None
            )
        )
        health = self.metrics.persistence_health_snapshot()
        payload["persistence"] = {
            "scope": "operational",
            "required": bool(
                self.config.questdb is not None and self.config.questdb.enabled
            ),
            "state": health.state.value,
            "last_successful_write_time": health.last_successful_write_time,
            "last_failure_time": health.last_failure_time,
            "consecutive_failures": health.consecutive_failures,
            "lifetime_failures": health.lifetime_failures,
            "buffered_records": health.buffered_records,
            "buffered_bytes": health.buffered_bytes,
            "oldest_buffered_record_age_seconds": (
                health.oldest_buffered_record_age_seconds
            ),
            "accepted_records": health.accepted_records,
            "flush_required_records": health.flush_required_records,
            "dropped_records": health.dropped_records,
            "rejected_closed_records": health.rejected_closed_records,
            "rejected_full_records": health.rejected_full_records,
            "failed_records": health.failed_records,
            "worker_running": health.worker_running,
            "close_outcome": health.close_outcome,
            "unflushed_on_close": health.unflushed_on_close,
            "current_run_outcomes": self.metrics.run_persistence_snapshot(),
        }
        payload["process_lifetime"] = self.metrics.process_lifetime_snapshot()
        payload["websocket"] = {
            "scope": "process_lifetime",
            **self.metrics.websocket_metrics_snapshot(),
        }
        itch_feed = getattr(self.lifecycle_manager, "itch_feed", None)
        if itch_feed is None:
            payload["itch"] = {
                "scope": "process_lifetime",
                "enabled": False,
                "state": "disabled",
                "active_session": None,
                "next_sequence_number": 1,
                "sessions_started": 0,
                "sessions_ended": 0,
                "messages_sent": 0,
                "packets_sent": 0,
                "heartbeats_sent": 0,
                "failures": 0,
                "last_error": None,
            }
        else:
            payload["itch"] = {
                "scope": "process_lifetime",
                **itch_feed.diagnostics(),
            }
        glimpse_service = getattr(self.lifecycle_manager, "glimpse_service", None)
        if glimpse_service is None:
            payload["glimpse"] = {
                "scope": "process_lifetime",
                "enabled": False,
                "state": "disabled",
                "active_connections": 0,
                "cached_sessions": 0,
                "cached_bytes": 0,
                "connections_accepted": 0,
                "fresh_sessions": 0,
                "resumed_sessions": 0,
                "completed_sessions": 0,
                "rejected_sessions": 0,
                "sequenced_messages": 0,
                "sequenced_bytes": 0,
                "heartbeats_sent": 0,
                "timeouts": 0,
                "failures": 0,
                "last_error": None,
            }
        else:
            payload["glimpse"] = {
                "scope": "process_lifetime",
                **glimpse_service.diagnostics(),
            }
        delivery = self.fix_app.delivery_queue.snapshot()
        payload["fix_delivery"] = {
            "scope": "process_lifetime",
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
            "retries": delivery.retries,
            "failures": delivery.failures,
            "retained_delivered_records": (delivery.retained_delivered_records),
            "oldest_pending_age_seconds": (delivery.oldest_pending_age_seconds),
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
                exchange=self.config.exchange_name,
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
            instrument = snapshot.instrument
            best_bid = (
                instrument.format_price(snapshot.bids[0].price_ticks)
                if snapshot.bids
                else None
            )
            best_ask = (
                instrument.format_price(snapshot.asks[0].price_ticks)
                if snapshot.asks
                else None
            )
            mid_price = None
            if snapshot.bids and snapshot.asks:
                # Half a tick needs at most one extra decimal place; keep the
                # configured display padding when that extra place is zero.
                mid_price = format_decimal_ratio(
                    instrument.price_decimal(
                        snapshot.bids[0].price_ticks + snapshot.asks[0].price_ticks
                    ),
                    2,
                    instrument.price_display_precision + 1,
                )
                if mid_price.endswith("0"):
                    mid_price = mid_price[:-1].removesuffix(".")
            initial_book = BookSnapshotResponse.model_validate(
                _book_payload(snapshot)
            ).model_dump(mode="json")
            return self.templates.TemplateResponse(
                request=request,
                name="book.html",
                context={
                    "symbol": snapshot.symbol,
                    "bids": [
                        {
                            "price": instrument.format_price(level.price_ticks),
                            "qty": instrument.format_quantity(level.level_qty_lots),
                            "orders": level.order_count,
                        }
                        for level in snapshot.bids[:20]
                    ],
                    "asks": [
                        {
                            "price": instrument.format_price(level.price_ticks),
                            "qty": instrument.format_quantity(level.level_qty_lots),
                            "orders": level.order_count,
                        }
                        for level in snapshot.asks[:20]
                    ],
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "mid_price": mid_price,
                    "initial_book": initial_book,
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

        if self.config.admin_api.enabled:
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
            responses={503: {"model": ErrorResponse | SessionFailedResponse}},
        )
        async def end_session() -> SessionEndResponse:
            self._require_admin()
            assert self.lifecycle is not None
            try:
                self.lifecycle.raise_if_transition_or_failed()
                result = await self.lifecycle.end_session()
            except LifecycleError as exc:
                self._raise_lifecycle_error(exc)
            if result.final_stats is not None and result.final_stats.run_failure is not None:
                failure = result.final_stats.run_failure
                response = SessionFailedResponse(
                    detail=ErrorDetail(code=failure.code, message=failure.message),
                    session_id=result.run_id,
                    run_generation=result.run_generation,
                    final_stats=result.final_stats,
                )
                return JSONResponse(status_code=503, content=response.model_dump(mode="json"))
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
                503: {"model": ErrorResponse | RunAdvanceFailedResponse},
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
            if self.config.scheduler.mode != "external":
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
                    max_advance = self.config.scheduler.max_advance_ms
                    milliseconds = int(body.milliseconds or 0)
                    if milliseconds > max_advance:
                        raise _api_error(
                            422,
                            "ADVANCE_TOO_LARGE",
                            ("milliseconds exceeds configured limit " f"{max_advance}"),
                        )
                    target_time = previous_time + timedelta(milliseconds=milliseconds)

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
                    await self.lifecycle.execute_administrative_advance(_advance)
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
            if self.lifecycle.run_failure is not None:
                failure = self.lifecycle.run_failure
                response = RunAdvanceFailedResponse(
                    detail=ErrorDetail(code=failure.code, message=failure.message),
                    run_failure=RunFailureResponse.model_validate(failure),
                    current_time=self.scheduler.current_time,
                    events_processed=events_processed,
                    pending_events=self.scheduler.get_pending_count(),
                )
                return JSONResponse(status_code=503, content=response.model_dump(mode="json"))
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
    config: ExchangeConfig,
    fix_app: FIXApplication,
) -> FastAPI:
    """Create an in-process API facade around initialized components."""
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
            "persistence": ("ready" if metrics.persistence is not None else "disabled"),
        }
    )
    api.app.state.lifecycle_started = True
    api.app.state.accepting_admin = True
    api.app.state.ready = True
    return api.app

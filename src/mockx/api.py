"""Typed REST lifecycle, diagnostics, and observability for MockX."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, Literal, Optional

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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

    code: str
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
    scenario_label: Optional[str]
    run_generation: Optional[int]
    run_failure: Optional[RunFailureResponse]
    orders: OrdersStats
    trades: TradesStats
    cancellations: CancellationStats
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
    scenario_label: Optional[str]
    run_generation: Optional[int]
    run_failure: Optional[RunFailureResponse]
    orders: OrdersStats
    trades: TradesStats
    cancellations: CancellationStats
    reports: ReportsStats
    sessions: SessionsStats
    fix_delivery: FixDeliveryStats
    itch: ITCHStats
    glimpse: GlimpseStats
    book: BookStats
    latency: LatencyStats
    persistence: PersistenceStats
    scheduler: SchedulerStats
    process_lifetime: Dict[str, object]


class SessionBody(BaseModel):
    """Start a fresh UUID run with a caller-selected scenario label."""

    model_config = ConfigDict(extra="forbid")
    scenario_label: str = Field(min_length=1, max_length=128)


class SessionStartResponse(BaseModel):
    status: Literal["session_started"]
    session_id: str
    scenario_label: str
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

        if self.lifecycle is not None:
            self.lifecycle.bind_api(
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
        if self.lifecycle_manager is None:
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

        critical_names = ["engine", "scheduler", "api", "metrics", "glimpse"]
        if self.lifecycle is not None and self.lifecycle.state == LifecycleState.ACTIVE:
            critical_names.append("itch")
        if self.lifecycle_manager is not None:
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
        persistence_required = True
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
        payload["scenario_label"] = (
            self.lifecycle.scenario_label if self.lifecycle else None
        )
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
                if self.lifecycle is not None
                else self.metrics.run_id is not None
            )
        )
        health = self.metrics.persistence_health_snapshot()
        payload["persistence"] = {
            "scope": "operational",
            "required": True,
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

        if self.config.admin_api.enabled:
            self._setup_admin_routes()

    def _setup_admin_routes(self) -> None:
        @self.app.post("/api/v1/checkpoints")
        async def checkpoint():
            self._require_admin()
            assert self.lifecycle is not None
            try:
                result = await self.lifecycle.capture_checkpoint()
            except LifecycleError as exc:
                self._raise_lifecycle_error(exc)
            return {
                "status": "checkpoint_completed",
                "run_id": self.lifecycle.run_id,
                "checkpoint": result,
            }

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
                result = await self.lifecycle.start_session(session.scenario_label)
            except LifecycleError as exc:
                self._raise_lifecycle_error(exc)
            return SessionStartResponse(
                status="session_started",
                session_id=result.run_id,
                scenario_label=result.scenario_label,
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
            if (
                result.final_stats is not None
                and result.final_stats.run_failure is not None
            ):
                failure = result.final_stats.run_failure
                response = SessionFailedResponse(
                    detail=ErrorDetail(code=failure.code, message=failure.message),
                    session_id=result.run_id,
                    run_generation=result.run_generation,
                    final_stats=result.final_stats,
                )
                return JSONResponse(
                    status_code=503, content=response.model_dump(mode="json")
                )
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
                return JSONResponse(
                    status_code=503, content=response.model_dump(mode="json")
                )
            return AdvanceTimeResponse(
                previous_time=previous_time,
                current_time=self.scheduler.current_time,
                events_processed=events_processed,
                pending_events=self.scheduler.get_pending_count(),
            )


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

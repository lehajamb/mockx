"""Application ownership and structured lifecycle for the mock exchange."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import os
from typing import Optional

import uvicorn

from .api import ExchangeAPI
from .configuration import ExchangeConfig
from .engine import MatchingEngine
from .gateway import FIXApplication, FIXGateway, SessionRegistry
from .latency import (
    LatencyScheduler,
    PerfCounterClock,
    SchedulerFailedError,
)
from .instruments import InstrumentCatalog
from .lifecycle import ExchangeLifecycle, LifecycleState
from .metrics import (
    BufferedMetricsWriter,
    LatencyTracker,
    MetricsCollector,
)
from .models import (
    CancelOrderCommand,
    ExecutionParams,
    ExecutionReport,
    MarketDataSnapshot,
    ProcessOrderCommand,
    SchedulerState,
    Trade,
)
from .questdb_writer import QuestDBMetricsSink
from .validation import OrderNormalizer


logger = logging.getLogger(__name__)


class MockExchange:
    """Own every component and expose exactly one retained API service."""

    def __init__(
        self,
        config: ExchangeConfig,
        fix_cfg: Optional[str] = None,
    ) -> None:
        self.config = config
        self.fix_cfg = fix_cfg
        self.running = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server: Optional[uvicorn.Server] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._started = False
        self._shutdown_complete = False
        self._callbacks_registered = False

        self.fix_enabled = self.config.fix.enabled
        self.scheduler_mode = self.config.scheduler.mode

        self._init_components()

    def _init_components(self) -> None:
        latency_config = self.config.latency
        self.exec_params = ExecutionParams(
            network_latency_ms=latency_config.total_network_ms,
            engine_latency_ms=latency_config.engine_ms,
            reject_prob=self.config.execution.reject_prob,
            rand_seed=self.config.execution.random_seed,
            inbound_network_latency_ms=latency_config.inbound_ms,
            outbound_network_latency_ms=latency_config.outbound_ms,
        )

        self.scheduler = LatencyScheduler(
            session_delays=dict(latency_config.session_delays),
            max_events_per_advance=(
                self.config.scheduler.max_events_per_advance
            ),
        )
        self.monotonic_clock = PerfCounterClock()
        self.instruments = InstrumentCatalog.from_config(self.config)
        self.order_normalizer = OrderNormalizer(self.instruments)

        self.questdb_writer = None
        self.metrics_persistence = None
        questdb_config = self.config.questdb
        if questdb_config is not None and questdb_config.enabled:
            self.questdb_writer = QuestDBMetricsSink(
                url=questdb_config.url or "",
                health_url=questdb_config.health_url or "",
                username=questdb_config.username,
                password=questdb_config.password,
                request_timeout_ms=questdb_config.request_timeout_ms,
            )
            self.metrics_persistence = BufferedMetricsWriter(
                self.questdb_writer,
                flush_batch_size=questdb_config.flush_batch_size,
                max_buffer_records=questdb_config.max_buffer_records,
                max_buffer_bytes=questdb_config.max_buffer_bytes,
                flush_interval_ms=questdb_config.flush_interval_ms,
                retry_backoff_ms=questdb_config.retry_backoff_ms,
                max_retry_delay_ms=questdb_config.max_retry_delay_ms,
                overflow_retry_attempts=(
                    questdb_config.overflow_retry_attempts
                ),
                shutdown_timeout_ms=questdb_config.shutdown_timeout_ms,
                overflow_policy=questdb_config.overflow_policy,
            )

        self.metrics = MetricsCollector(
            self.config.exchange_name,
            instruments=self.instruments,
            persistence=self.metrics_persistence,
            prometheus_enabled=self.config.prometheus.enabled,
        )
        self.latency_tracker = LatencyTracker(
            self.scheduler,
            self.metrics,
            monotonic_clock=self.monotonic_clock,
        )
        self.engine = MatchingEngine(
            self.exec_params,
            self.scheduler,
            self.instruments,
            monotonic_clock=self.monotonic_clock,
        )
        self.metrics.bind_engine(self.engine)

        self.session_registry = SessionRegistry()
        self.fix_app = FIXApplication(
            self.engine,
            self.metrics,
            self.latency_tracker,
            session_registry=self.session_registry,
            monotonic_clock=self.monotonic_clock,
            order_normalizer=self.order_normalizer,
            session_participants=self.config.fix.session_participants,
            delivery_retry_interval_ms=(
                self.config.fix.delivery_retry_interval_ms
            ),
            delivery_max_attempts_per_cycle=(
                self.config.fix.delivery_max_attempts_per_cycle
            ),
            delivery_terminal_retention_records=(
                self.config.fix.delivery_terminal_retention_records
            ),
        )
        self.lifecycle = ExchangeLifecycle(
            self.engine,
            self.metrics,
            self.latency_tracker,
            self.fix_app,
        )
        self.fix_app.lifecycle = self.lifecycle
        self.fix_gateway: Optional[FIXGateway] = None
        if self.fix_enabled:
            if not self.fix_cfg or not os.path.isfile(self.fix_cfg):
                raise ValueError(
                    f"FIX config file does not exist: {self.fix_cfg}"
                )
            self.fix_gateway = FIXGateway(
                self.fix_cfg,
                self.engine,
                self.metrics,
                self.latency_tracker,
                application=self.fix_app,
                session_participants=self.config.fix.session_participants,
            )

        # Ownership is explicit: no route endpoint introspection is involved.
        self.api = ExchangeAPI(
            self.engine,
            self.metrics,
            self.config,
            self.fix_app,
            order_normalizer=self.order_normalizer,
            lifecycle_manager=self,
            session_lifecycle=self.lifecycle,
        )
        self.app = self.api.app
        self.app.state.exchange = self
        self.app.state.fix_gateway = self.fix_gateway

        self._setup_callbacks()

    def _setup_callbacks(self) -> None:
        self.scheduler.register_handler(
            "new_order_received",
            self.fix_app.process_new_order_envelope,
        )
        self.scheduler.register_handler(
            "cancel_received",
            self.fix_app.process_cancel_envelope,
        )
        self.scheduler.register_handler(
            "order_arrived",
            self._handle_order_arrived,
        )
        self.scheduler.register_handler(
            "order_process",
            self._handle_order_process,
        )
        self.scheduler.register_handler(
            "fix_delivery_due",
            self.fix_app.process_delivery_due,
        )
        self.scheduler.register_handler(
            "cancel_arrived_at_exchange",
            self._handle_cancel_arrived,
        )
        self.scheduler.register_handler(
            "cancel_engine_process",
            self._handle_cancel_process,
        )

        self.engine.on_execution_report = self._on_execution_report
        self.engine.on_market_data_update = self._on_market_data_update
        self.engine.on_trade = self._on_trade
        self.engine.on_self_trade_prevention = (
            self.metrics.record_self_trade_prevention
        )
        self.engine.on_order_submitted = self._on_order_submitted
        self.engine.on_cancel_request = self.metrics.record_cancel_request
        self.engine.on_cancel_reject = self.metrics.record_cancel_reject
        self.engine.on_cancel_outcome = self.metrics.record_cancel_outcome
        self.engine.on_cpu_duration = self.latency_tracker.record_cpu_duration
        self.scheduler.on_queue_depth_change = (
            self.metrics.update_scheduler_pending
        )
        self.scheduler.on_failure = self.metrics.record_scheduler_failure
        self.scheduler.on_state_change = self.metrics.update_scheduler_state
        self.metrics.update_scheduler_pending(self.scheduler.get_pending_count())
        self.metrics.update_scheduler_state(self.scheduler.state)
        self._callbacks_registered = True

    async def start_components(self) -> None:
        """Start components in one documented, readiness-gated order."""
        async with self.lifecycle.lock:
            if self._started:
                return
            if self._shutdown_complete:
                raise RuntimeError(
                    "A stopped MockExchange instance cannot be restarted"
                )

            state = self.app.state
            state.ready = False
            state.accepting_admin = False
            state.component_status["api"] = "starting"
            state.component_status["engine"] = (
                "ready" if self.engine.books else "failed"
            )
            state.component_status["scheduler"] = "starting"
            state.component_status["metrics"] = "ready"
            if self.metrics.persistence is not None:
                await self.metrics.persistence.start()
                state.component_status["persistence"] = (
                    self.metrics.persistence.health_snapshot()
                    .state.value.lower()
                )
            else:
                state.component_status["persistence"] = "disabled"

            self.loop = asyncio.get_running_loop()
            # This handoff is deliberately before QuickFIX accepts traffic.
            self.fix_app.loop = self.loop
            state.component_status["scheduler"] = (
                "waiting_external"
                if self.scheduler_mode == "external"
                else "ready"
            )

            if not self._callbacks_registered:
                raise RuntimeError(
                    "Engine callbacks must be registered before startup"
                )

            if self.fix_enabled:
                state.component_status["fix_gateway"] = "starting"
                try:
                    assert self.fix_gateway is not None
                    self.fix_gateway.start()
                except Exception:
                    state.component_status["fix_gateway"] = "failed"
                    raise
                state.component_status["fix_gateway"] = "ready"
            else:
                state.component_status["fix_gateway"] = "disabled"

            self.running = True
            if self.scheduler_mode == "realtime":
                self._scheduler_task = self.loop.create_task(
                    self._run_scheduler(),
                    name=f"{self.config.exchange_name}-scheduler",
                )

            state.component_status["api"] = "ready"
            state.accepting_admin = True
            state.ready = True
            self._started = True
            logger.info("%s is ready", self.config.exchange_name)

    async def stop_components(self) -> None:
        """Stop ingestion first, flush last, and continue after close errors."""
        async with self.lifecycle.lock:
            if self._shutdown_complete:
                return

            state = self.app.state
            if self.lifecycle.state == LifecycleState.ACTIVE:
                self.lifecycle._publish(LifecycleState.ENDING)
            # Readiness and administrative ingestion are disabled first.
            state.ready = False
            state.accepting_admin = False
            state.component_status["api"] = "stopping"

            if self.fix_gateway is not None:
                try:
                    self.fix_gateway.stop()
                except Exception:
                    logger.exception("FIX gateway stop failed")
                finally:
                    state.component_status["fix_gateway"] = "stopped"

            self.running = False
            if self._scheduler_task is not None:
                self._scheduler_task.cancel()
                try:
                    await self._scheduler_task
                except asyncio.CancelledError:
                    pass
                finally:
                    self._scheduler_task = None
            self.scheduler.stop()
            state.component_status["scheduler"] = "stopped"

            try:
                await self.api.close_websockets()
            except Exception:
                logger.exception("WebSocket shutdown failed")

            try:
                metrics_closed = await self.metrics.close()
                if not metrics_closed.successful:
                    logger.error(
                        "Metrics persistence did not close cleanly",
                        extra={
                            "persistence_close_outcome": (
                                metrics_closed.outcome.value
                            ),
                            "unflushed_records": (
                                metrics_closed.unflushed_records
                            ),
                        },
                    )
                    state.component_status["persistence"] = "closed"
            except Exception:
                logger.exception("Metrics shutdown failed")
                state.component_status["persistence"] = "degraded"
            finally:
                state.component_status["metrics"] = "stopped"

            self.fix_app.loop = None
            self.loop = None
            state.component_status["engine"] = "stopped"
            state.component_status["api"] = "stopped"
            self._started = False
            self._shutdown_complete = True
            if self.lifecycle.state != LifecycleState.FAILED:
                self.lifecycle.run_id = None
                self.lifecycle._publish(LifecycleState.STOPPED)
            logger.info("%s stopped", self.config.exchange_name)

    def _on_order_submitted(self, order_id: int, command) -> None:
        self.metrics.record_order_submitted(order_id, command)
        run_generation = self.engine.orders[order_id].run_generation
        self.latency_tracker.order_submitted(
            order_id,
            session_id=command.session_id,
            sim_submitted_at=command.submitted_at,
            run_generation=run_generation,
        )

    def _handle_order_arrived(self, command: ProcessOrderCommand) -> None:
        self.latency_tracker.order_arrived(
            command.order_id,
            self.engine.identifier_scope.generation,
        )

    def _handle_order_process(self, command: ProcessOrderCommand) -> None:
        order_id = command.order_id
        run_generation = self.engine.identifier_scope.generation
        self.latency_tracker.order_processing_start(
            order_id,
            run_generation,
        )
        self.engine.process_order(command)
        self.latency_tracker.order_processing_end(
            order_id,
            run_generation,
        )

    def _on_execution_report(self, report: ExecutionReport) -> None:
        self.metrics.record_order(report)
        if report.cancel_ingress_sequence is None:
            self.latency_tracker.report_created(report)
        self.fix_app.enqueue_report(report)

    def _handle_cancel_arrived(self, command: CancelOrderCommand) -> None:
        self.latency_tracker.cancel_arrived(command)

    def _handle_cancel_process(self, command: CancelOrderCommand) -> None:
        report = self.engine.cancel_order(command)
        self.latency_tracker.cancel_report_created(command, report)
        if not isinstance(report, ExecutionReport):
            self.fix_app.enqueue_report(report)

    @staticmethod
    def _log_broadcast_result(future) -> None:
        try:
            future.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Market-data broadcast failed")

    def _on_market_data_update(
        self,
        snapshot: MarketDataSnapshot,
    ) -> None:
        """Record metrics and hand the immutable snapshot to the API loop."""
        self.metrics.update_book_metrics(snapshot)
        self.metrics.update_active_orders(snapshot)

        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            task = loop.create_task(
                self.api.broadcast_market_data(snapshot)
            )
            task.add_done_callback(self._log_broadcast_result)
        elif loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self.api.broadcast_market_data(snapshot),
                loop,
            )
            future.add_done_callback(self._log_broadcast_result)

    def _on_trade(self, trade: Trade) -> None:
        self.metrics.record_trade(trade)

    async def _run_scheduler(self) -> None:
        """Advance simulated time periodically only in explicit realtime mode."""
        interval_ms = self.config.scheduler.interval_ms
        while self.running:
            if self.scheduler.state == SchedulerState.FAILED:
                await asyncio.sleep(interval_ms / 1000)
                continue
            if self.scheduler.state == SchedulerState.STOPPED:
                return
            try:
                await asyncio.sleep(interval_ms / 1000)
                await self.lifecycle.advance_realtime_by(
                    timedelta(milliseconds=interval_ms)
                )
            except asyncio.CancelledError:
                raise
            except SchedulerFailedError:
                self.app.state.component_status["scheduler"] = "failed"
                logger.exception(
                    "Realtime scheduler entered fail-stop state"
                )

    async def _run_api(self) -> None:
        admin_config = self.config.admin_api
        config = uvicorn.Config(
            self.app,
            host=admin_config.host,
            port=admin_config.port,
            log_level="warning",
        )
        self.server = uvicorn.Server(config)
        await self.server.serve()

    def run(self) -> None:
        """Run Uvicorn; its lifespan owns FIX, scheduler, and persistence."""
        logger.info("Starting %s", self.config.exchange_name)
        asyncio.run(self._run_api())

    async def aclose(self) -> bool:
        """Close an in-process exchange without requiring an ASGI server."""
        if self._started:
            await self.stop_components()
            return self.app.state.component_status["persistence"] != "degraded"
        return await self.metrics.close()

    def stop(self) -> None:
        """Request server shutdown; the ASGI lifespan performs teardown."""
        if self.server is not None:
            self.server.should_exit = True

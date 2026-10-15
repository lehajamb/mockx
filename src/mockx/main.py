"""Application ownership and structured lifecycle for the mock exchange."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import inspect
import os
from collections.abc import Mapping
from types import MappingProxyType
from typing import Optional

import uvicorn

from .api import ExchangeAPI
from .effects import CommandEffect, CommandEffects
from .journal import ExchangeJournal, JournalAdmissionError
from .configuration import ExchangeConfig
from .engine import MatchingEngine
from .gateway import FIXApplication, FIXGateway, SessionRegistry
from .latency import (
    LatencyScheduler,
    PerfCounterClock,
    SchedulerFailedError,
    datetime_to_ns,
)
from .instruments import InstrumentCatalog
from .glimpse_encoder import GlimpseEncoder
from .glimpse_service import GlimpseService
from .itch_delivery import ITCHFeedPublisher
from .lifecycle import ExchangeLifecycle, LifecycleState
from .metrics import (
    BufferedMetricsWriter,
    LatencyTracker,
    MetricsCollector,
    PersistenceBackpressureTimeout,
)
from .models import (
    CancelOrderCommand,
    ExecutionParams,
    ExecutionReport,
    CancelReject,
    ReplaceOrderCommand,
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
        *,
        persistence_sink=None,
        fix_gateway_factory=None,
    ) -> None:
        self.config = config
        self.fix_cfg = fix_cfg or "configs/mockx1.cfg"
        if not os.path.isfile(self.fix_cfg):
            raise ValueError(f"FIX config file does not exist: {self.fix_cfg}")
        self._persistence_sink = persistence_sink
        self._fix_gateway_factory = fix_gateway_factory or FIXGateway
        self.running = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server: Optional[uvicorn.Server] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._started = False
        self._shutdown_complete = False
        self._callbacks_registered = False
        self._captured_effects: Optional[list[CommandEffect]] = None

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
            max_events_per_advance=self.config.scheduler.max_events_per_advance,
            processing_batch_size=self.config.scheduler.processing_batch_size,
        )
        self.monotonic_clock = PerfCounterClock()
        self.instruments = InstrumentCatalog.from_config(self.config)
        self.order_normalizer = OrderNormalizer(
            self.instruments,
            itch_compatible=True,
        )

        questdb_config = self.config.questdb
        self.questdb_writer = self._persistence_sink or QuestDBMetricsSink(
            url=questdb_config.url,
            health_url=questdb_config.health_url,
            username=questdb_config.username,
            password=questdb_config.password,
            request_timeout_ms=questdb_config.request_timeout_ms,
        )
        self.metrics_persistence = BufferedMetricsWriter(
            self.questdb_writer,
            flush_batch_size=questdb_config.flush_batch_size,
            flush_batch_bytes=questdb_config.flush_batch_bytes,
            max_buffer_records=questdb_config.max_buffer_records,
            max_buffer_bytes=questdb_config.max_buffer_bytes,
            flush_interval_ms=questdb_config.flush_interval_ms,
            retry_backoff_ms=questdb_config.retry_backoff_ms,
            max_retry_delay_ms=questdb_config.max_retry_delay_ms,
            backpressure_timeout_ms=questdb_config.backpressure_timeout_ms,
            shutdown_timeout_ms=questdb_config.shutdown_timeout_ms,
        )
        self.journal = ExchangeJournal(
            self.metrics_persistence, self.config.exchange_name
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

        self.itch_feed = ITCHFeedPublisher(
            self.instruments,
            self.scheduler,
            self.config.itch,
            self.config.moldudp64,
            self.metrics,
            on_state_change=self._on_itch_state,
        )
        self.metrics.update_itch_state("idle", 1)
        self.metrics_persistence.before_wait = self.itch_feed.flush

        self.session_registry = SessionRegistry()
        self.fix_app = FIXApplication(
            self.engine,
            self.metrics,
            self.latency_tracker,
            session_registry=self.session_registry,
            monotonic_clock=self.monotonic_clock,
            order_normalizer=self.order_normalizer,
            session_participants=self.config.fix.session_participants,
            max_orders_per_list=self.config.fix.max_orders_per_list,
            delivery_retry_interval_ms=(self.config.fix.delivery_retry_interval_ms),
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
            self.itch_feed,
            journal=self.journal,
        )
        self.glimpse_service = GlimpseService(
            self.config.soupbintcp,
            GlimpseEncoder(
                self.itch_feed.encoder,
                market_category=self.config.itch.market_category.encode("ascii"),
                financial_status=self.config.itch.financial_status.encode("ascii"),
                round_lot_size=self.config.itch.round_lot_size,
            ),
            self.lifecycle.capture_glimpse_snapshot,
            self.metrics,
            on_state_change=self._on_glimpse_state,
        )
        self.lifecycle.bind_glimpse_service(self.glimpse_service)
        self.metrics.update_glimpse_state("idle")
        self.fix_app.lifecycle = self.lifecycle
        self.lifecycle.command_executor = self._execute_ingress_command
        self._enqueue_report = self.fix_app.enqueue_report
        self.fix_app.enqueue_report = self._queue_report
        self.fix_app.on_report_delivered = self._on_report_delivered
        self.fix_gateway = self._fix_gateway_factory(
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
            lifecycle_manager=self,
            session_lifecycle=self.lifecycle,
        )
        self.app = self.api.app
        self.app.state.exchange = self
        self.app.state.fix_gateway = self.fix_gateway

        self._setup_callbacks()

    def _setup_callbacks(self) -> None:
        handlers = {
            "new_order_received": self.fix_app.process_new_order_envelope,
            "cancel_received": self.fix_app.process_cancel_envelope,
            "replace_received": self.fix_app.process_replace_envelope,
            "order_arrived": self._handle_order_arrived,
            "order_process": self._handle_order_process,
            "cancel_arrived_at_exchange": self._handle_cancel_arrived,
            "cancel_engine_process": self._handle_cancel_process,
            "replace_arrived_at_exchange": self._handle_replace_arrived,
            "replace_engine_process": self._handle_replace_process,
            "fix_delivery_due": self.fix_app.process_delivery_due,
        }
        for event_type, handler in handlers.items():

            async def execute(data, event_type=event_type, handler=handler):
                return await self._execute_committed_command(event_type, handler, data)

            self.scheduler.register_handler(event_type, execute)
        self.scheduler.register_handler(
            self.itch_feed.HEARTBEAT_EVENT, self.itch_feed.handle_heartbeat
        )
        self.scheduler.on_processing_boundary = self._processing_boundary
        self.engine.on_execution_report = self._on_execution_report
        self.engine.on_trade = self._on_trade
        self.engine.on_self_trade_prevention = self._on_self_trade_prevention
        self.engine.on_displayed_order_event = lambda event: self._capture(
            "book_change", event
        )
        self.engine.on_order_submitted = self._on_order_submitted
        self.engine.on_cancel_request = self._on_cancel_request
        self.engine.on_cancel_reject = self.metrics.record_cancel_reject
        self.engine.on_cancel_outcome = self._on_cancel_outcome
        self.engine.on_replace_request = lambda command: self._capture(
            "replace_request", command
        )
        self.engine.on_replace_reject = self.metrics.record_cancel_reject
        self.engine.on_cpu_duration = self.latency_tracker.record_cpu_duration
        self.scheduler.on_queue_depth_change = self.metrics.update_scheduler_pending
        self.scheduler.on_failure = self._on_scheduler_failure
        self.scheduler.on_state_change = self.metrics.update_scheduler_state
        self.metrics.update_scheduler_pending(self.scheduler.get_pending_count())
        self.metrics.update_scheduler_state(self.scheduler.state)
        self._callbacks_registered = True

    def _capture(self, kind: str, payload: object) -> None:
        if self._captured_effects is not None:
            if isinstance(payload, dict):
                payload = MappingProxyType(dict(payload))
            self._captured_effects.append(CommandEffect(kind, payload))

    def _queue_report(self, report) -> None:
        if self._captured_effects is None:
            self._enqueue_report(report)
        else:
            self._capture("report", report)

    async def _execute_committed_command(self, event_type, handler, data):
        # Delivery remains available after a run fault so truthful reports settle.
        delivery = event_type == "fix_delivery_due"
        if not delivery and self.lifecycle.check_persistence_failure():
            self.fix_app.reject_failed_command(
                event_type, data, self.lifecycle.failure_message
            )
            return
        self.lifecycle._command_depth += 1
        effects: list[CommandEffect] = []
        self._captured_effects = effects
        try:
            result = handler(data)
            if inspect.isawaitable(result):
                result = await result
        except BaseException:
            # Mutation may have occurred. No completed marker or rollback is invented.
            self._captured_effects = None
            raise
        else:
            self._captured_effects = None

            async def settle():
                def effect_run(effect):
                    return (
                        effect.payload.get("run_id")
                        if isinstance(effect.payload, Mapping)
                        else getattr(effect.payload, "run_id", None)
                    )

                scopes = {}
                if delivery:
                    for effect in effects:
                        scopes.setdefault(
                            effect_run(effect) or self.journal.run_id, []
                        ).append(effect)
                else:
                    scopes[self.journal.run_id] = effects
                envelopes = tuple(
                    CommandEffects(
                        event_type,
                        self.scheduler.current_time,
                        tuple(group),
                        ingress_sequence=getattr(data, "ingress_sequence", None),
                        order_id=getattr(data, "order_id", None),
                        run_id=run_id,
                    )
                    for run_id, group in scopes.items()
                    if run_id is not None
                )
                try:
                    for envelope in envelopes:
                        sequence = await self.journal.admit_command(envelope)
                        if (
                            envelope.run_id is None
                            or envelope.run_id == self.metrics.run_id
                        ):
                            self.metrics.note_journal_sequence(
                                sequence, datetime_to_ns(envelope.timestamp)
                            )
                except asyncio.CancelledError:
                    # A dependency cancellation after matching is a run fault,
                    # and must still publish the command's truthful reports.
                    reason = "Completed command journal admission was cancelled"
                    self.metrics.mark_integrity_failure(reason)
                    self.lifecycle.request_failure(
                        "JOURNAL_ADMISSION_CANCELLED", reason
                    )
                except Exception as exc:
                    self.metrics.mark_integrity_failure(str(exc))
                    self.lifecycle.request_failure("JOURNAL_ADMISSION_FAILED", str(exc))
                # Admission failure cannot turn an executed fill into a rejection.
                for effect in effects:
                    if effect.kind == "report":
                        self._enqueue_report(effect.payload)
                self.itch_feed.publish_batch(
                    tuple(e.payload for e in effects if e.kind == "book_change")
                )
                await self._wait_persistence_pressure()
                return result

            task = asyncio.create_task(settle(), name=f"command-commit:{event_type}")
            return await self.lifecycle._complete_transition(
                task, operation="command", run_id=self.lifecycle.run_id
            )
        finally:
            self._captured_effects = None
            self.lifecycle._command_depth -= 1
            self.lifecycle.check_persistence_failure()

    async def _execute_ingress_command(self, action):
        return await self._execute_committed_command(
            "fix_ingress", lambda _: action(), None
        )

    async def _processing_boundary(self) -> None:
        self.itch_feed.flush()
        self.metrics.refresh_engine_gauges()
        await self._wait_persistence_pressure()
        self.lifecycle.check_persistence_failure()

    async def _wait_persistence_pressure(self) -> None:
        if self.lifecycle.state == LifecycleState.ACTIVE:
            try:
                await self.metrics_persistence.wait_for_pressure_relief()
            except PersistenceBackpressureTimeout as exc:
                self.lifecycle.request_failure(
                    "PERSISTENCE_BACKPRESSURE_TIMEOUT", str(exc)
                )
            except asyncio.CancelledError:
                self.lifecycle.request_failure(
                    "PERSISTENCE_PRESSURE_CANCELLED",
                    "Admitted backlog pressure wait was cancelled",
                )

    def _on_scheduler_failure(self, failure) -> None:
        self.metrics.record_scheduler_failure(failure)
        self.lifecycle.request_failure(
            "ENGINE_FAILED", "Engine command failed; start a fresh run."
        )

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
            state.component_status["itch"] = (
                self.itch_feed.state.value if self.itch_feed is not None else "disabled"
            )
            state.component_status["glimpse"] = (
                self.glimpse_service.state.value
                if self.glimpse_service is not None
                else "disabled"
            )
            if self.metrics.persistence is not None:
                await self.metrics.persistence.start()
                state.component_status["persistence"] = (
                    self.metrics.persistence.health_snapshot().state.value.lower()
                )
            else:
                state.component_status["persistence"] = "disabled"

            self.loop = asyncio.get_running_loop()
            # This handoff is deliberately before QuickFIX accepts traffic.
            self.fix_app.loop = self.loop
            state.component_status["scheduler"] = (
                "waiting_external" if self.scheduler_mode == "external" else "ready"
            )

            if not self._callbacks_registered:
                raise RuntimeError("Engine callbacks must be registered before startup")

            if self.glimpse_service is not None:
                if not await self.glimpse_service.start():
                    raise RuntimeError("Required GLIMPSE listener startup failed")

            state.component_status["fix_gateway"] = "starting"
            try:
                self.fix_gateway.start()
            except Exception:
                state.component_status["fix_gateway"] = "failed"
                raise
            state.component_status["fix_gateway"] = "ready"

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

            if self.glimpse_service is not None:
                try:
                    await self.glimpse_service.stop()
                except Exception:
                    logger.exception("GLIMPSE shutdown failed")
                    state.component_status["glimpse"] = "degraded"

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
            if self.itch_feed is not None:
                try:
                    self.itch_feed.shutdown(self.scheduler.current_time)
                except Exception:
                    logger.exception("ITCH shutdown failed")
                    state.component_status["itch"] = "degraded"

            self.scheduler.stop()
            state.component_status["scheduler"] = "stopped"

            if self.journal.run_id is not None:
                try:
                    await self.journal.end_run(
                        self.scheduler.current_time,
                        failed=True,
                        reason=(
                            self.lifecycle.failure_message
                            if self.lifecycle.run_failure
                            else "Service stopped before normal run completion"
                        ),
                    )
                except Exception:
                    logger.exception("Run shutdown journal acknowledgment failed")
                self.journal.detach_run()

            try:
                metrics_closed = await self.metrics.close()
                if not metrics_closed.successful:
                    logger.error(
                        "Metrics persistence did not close cleanly",
                        extra={
                            "persistence_close_outcome": (metrics_closed.outcome.value),
                            "unflushed_records": (metrics_closed.unflushed_records),
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

    def _on_itch_state(self, state: str) -> None:
        api = getattr(self, "api", None)
        if api is not None:
            api.app.state.component_status["itch"] = state
        if state == "degraded":
            service = getattr(self, "glimpse_service", None)
            feed = getattr(self, "itch_feed", None)
            if (
                service is not None
                and feed is not None
                and feed.active_generation is not None
            ):
                service.revoke_run(feed.active_generation)
            lifecycle = getattr(self, "lifecycle", None)
            if lifecycle is not None:
                lifecycle.request_failure(
                    "ITCH_PUBLISHER_FAILED", "Required ITCH publisher failed"
                )

    def _on_glimpse_state(self, state: str) -> None:
        api = getattr(self, "api", None)
        if api is not None:
            api.app.state.component_status["glimpse"] = state
        if state == "degraded":
            self.lifecycle.request_failure(
                "GLIMPSE_LISTENER_FAILED", "Required GLIMPSE listener failed"
            )

    def _on_order_submitted(self, order_id: int, command) -> None:
        self._capture(
            "order_submitted",
            {
                "order_id": order_id,
                **{
                    name: getattr(command, name, None)
                    for name in (
                        "cl_ord_id",
                        "symbol",
                        "side",
                        "order_type",
                        "tif",
                        "price_ticks",
                        "order_qty_lots",
                        "session_id",
                        "session_alias",
                        "participant_id",
                        "list_id",
                        "submitted_at",
                    )
                },
            },
        )
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
        self.fix_app.enqueue_report(report)
        self.metrics.record_order(report)
        if (
            report.cancel_ingress_sequence is None
            and report.replace_ingress_sequence is None
        ):
            self.latency_tracker.report_created(report)

    def _handle_cancel_arrived(self, command: CancelOrderCommand) -> None:
        self.latency_tracker.cancel_arrived(command)

    def _handle_cancel_process(self, command: CancelOrderCommand) -> None:
        report = self.engine.cancel_order(command)
        self.latency_tracker.cancel_report_created(command, report)
        if not isinstance(report, ExecutionReport):
            self.fix_app.enqueue_report(report)

    def _on_trade(self, trade: Trade) -> None:
        self._capture("trade", trade)
        self.metrics.record_trade(trade)

    def _on_self_trade_prevention(self, event) -> None:
        self._capture("stp", event)
        self.metrics.record_self_trade_prevention(event)

    def _on_cancel_request(self, command) -> None:
        self._capture("cancel_request", command)
        self.metrics.record_cancel_request(command)

    def _on_cancel_outcome(self, *args) -> None:
        self._capture(
            "request_outcome",
            {
                "reason_code": args[1].value,
                "ingress_sequence": args[0].ingress_sequence,
            },
        )
        self.metrics.record_cancel_outcome(*args)

    def _handle_replace_arrived(self, command: ReplaceOrderCommand) -> None:
        self.latency_tracker.replace_arrived(command)

    def _handle_replace_process(self, command: ReplaceOrderCommand) -> None:
        report = self.engine.replace_order(command)
        self.latency_tracker.replace_report_created(command, report)
        if not isinstance(report, ExecutionReport):
            self.fix_app.enqueue_report(report)

    def _on_report_delivered(self, report, timestamp) -> None:
        self._capture(
            "timing",
            {
                "stage": "report_delivered",
                "timestamp": timestamp,
                "order_id": report.order_id,
                "run_id": report.run_id,
                "report_id": getattr(
                    report, "exec_id", getattr(report, "reject_id", None)
                ),
            },
        )

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
                logger.exception("Realtime scheduler entered fail-stop state")

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
        if not self.server.started:
            raise RuntimeError("Required exchange component startup failed")

    def run(self) -> None:
        """Run Uvicorn; its lifespan owns FIX, scheduler, and persistence."""
        logger.info("Starting %s", self.config.exchange_name)
        asyncio.run(self._run_api())

    async def aclose(self) -> bool:
        """Close an in-process exchange without requiring an ASGI server."""
        if self._started:
            await self.stop_components()
            return self.app.state.component_status["persistence"] != "degraded"
        return (await self.metrics.close()).successful

    def stop(self) -> None:
        """Request server shutdown; the ASGI lifespan performs teardown."""
        if self.server is not None:
            self.server.should_exit = True

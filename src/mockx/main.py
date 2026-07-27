"""Application ownership and structured lifecycle for the mock exchange."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import os
from typing import Any, Dict, Optional

import uvicorn

from .api import ExchangeAPI
from .engine import MatchingEngine
from .gateway import FIXApplication, FIXGateway, SessionRegistry
from .latency import (
    LatencyScheduler,
    PerfCounterClock,
    SchedulerFailedError,
)
from .metrics import (
    BufferedMetricsWriter,
    LatencyTracker,
    MetricsCollector,
)
from .models import (
    ExecutionParams,
    ExecutionReport,
    MarketDataSnapshot,
    ProcessOrderCommand,
    SchedulerState,
    TERMINAL_ORDER_STATUSES,
    Trade,
)
from .tsdb_writer import InfluxDBMetricsSink


logger = logging.getLogger(__name__)


class MockExchange:
    """Own every component and expose exactly one retained API service."""

    def __init__(
        self,
        config: Dict[str, Any],
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
        self._lifecycle_lock = asyncio.Lock()
        self._callbacks_registered = False

        fix_config = self.config.get("fix", {})
        self.fix_enabled = bool(
            fix_config.get("enabled", bool(self.fix_cfg))
        )
        self.scheduler_mode = self.config.get("scheduler", {}).get(
            "mode",
            "external",
        )
        if self.scheduler_mode not in {"external", "realtime"}:
            raise ValueError(
                "scheduler.mode must be 'external' or 'realtime'"
            )

        self._init_components()

    def _tsdb_config(self) -> Optional[dict[str, Any]]:
        configured = dict(self.config.get("tsdb") or {})
        environment_values = {
            "url": os.environ.get("INFLUXDB_URL"),
            "token": os.environ.get("INFLUXDB_TOKEN"),
            "org": os.environ.get("INFLUXDB_ORG"),
            "bucket": os.environ.get("INFLUXDB_BUCKET"),
        }
        if any(environment_values.values()):
            for key, value in environment_values.items():
                if value is not None:
                    configured[key] = value
        if not configured or not configured.get("enabled", True):
            return None
        missing = [
            key
            for key in ("url", "token", "org", "bucket")
            if not configured.get(key)
        ]
        if missing:
            raise ValueError(
                "Missing TSDB configuration fields: "
                + ", ".join(f"tsdb.{key}" for key in missing)
            )
        return configured

    def _init_components(self) -> None:
        latency_config = self.config["latency"]
        network_ms = latency_config.get(
            "network_ms",
            latency_config.get("inbound_ms", 0)
            + latency_config.get("outbound_ms", 0),
        )
        self.exec_params = ExecutionParams(
            network_latency_ms=network_ms,
            engine_latency_ms=latency_config["engine_ms"],
            reject_prob=self.config["execution"]["reject_prob"],
            rand_seed=self.config["execution"]["random_seed"],
            inbound_network_latency_ms=latency_config.get("inbound_ms"),
            outbound_network_latency_ms=latency_config.get("outbound_ms"),
        )

        session_delays = latency_config.get("session_delays", {})
        scheduler_config = self.config.get("scheduler", {})
        self.scheduler = LatencyScheduler(
            session_delays=session_delays,
            max_events_per_advance=scheduler_config.get(
                "max_events_per_advance",
                100_000,
            ),
        )
        self.monotonic_clock = PerfCounterClock()

        self.tsdb_writer = None
        self.metrics_persistence = None
        tsdb_config = self._tsdb_config()
        if tsdb_config is not None:
            self.tsdb_writer = InfluxDBMetricsSink(
                url=tsdb_config["url"],
                token=tsdb_config["token"],
                org=tsdb_config["org"],
                bucket=tsdb_config["bucket"],
            )
            self.metrics_persistence = BufferedMetricsWriter(
                self.tsdb_writer,
                batch_size=int(tsdb_config.get("batch_size", 1000)),
            )

        self.metrics = MetricsCollector(
            self.config["exchange_name"],
            persistence=self.metrics_persistence,
            prometheus_enabled=self.config.get("prometheus", {}).get(
                "enabled",
                True,
            ),
        )
        self.latency_tracker = LatencyTracker(
            self.scheduler,
            self.metrics,
            monotonic_clock=self.monotonic_clock,
        )
        self.engine = MatchingEngine(
            self.exec_params,
            self.scheduler,
            monotonic_clock=self.monotonic_clock,
        )
        self.metrics.bind_engine(self.engine)
        for symbol in self.config["symbols"]:
            self.engine.add_symbol(symbol)

        self.session_registry = SessionRegistry()
        self.fix_app = FIXApplication(
            self.engine,
            self.metrics,
            self.latency_tracker,
            session_registry=self.session_registry,
            monotonic_clock=self.monotonic_clock,
        )
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
            )

        # Ownership is explicit: no route endpoint introspection is involved.
        self.api = ExchangeAPI(
            self.engine,
            self.metrics,
            self.config,
            self.fix_app,
            lifecycle_manager=self,
        )
        self.app = self.api.app
        self.app.state.exchange = self
        self.app.state.fix_gateway = self.fix_gateway

        self._setup_callbacks()

    def _setup_callbacks(self) -> None:
        self.scheduler.register_handler(
            "order_arrived",
            self._handle_order_arrived,
        )
        self.scheduler.register_handler(
            "order_process",
            self._handle_order_process,
        )
        self.scheduler.register_handler(
            "exec_report",
            self._handle_exec_report,
        )

        self.engine.on_execution_report = self._on_execution_report
        self.engine.on_market_data_update = self._on_market_data_update
        self.engine.on_trade = self._on_trade
        self.engine.on_order_submitted = self._on_order_submitted
        self.engine.on_cancel_request = self.metrics.record_cancel_request
        self.engine.on_cancel_reject = self.metrics.record_cancel_reject
        self.engine.on_matching_duration = (
            self.latency_tracker.record_real_matching_duration
        )
        self._callbacks_registered = True

    async def start_components(self) -> None:
        """Start components in one documented, readiness-gated order."""
        async with self._lifecycle_lock:
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
            state.component_status["persistence"] = (
                "ready"
                if self.metrics.persistence is not None
                else "disabled"
            )

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
                    name=f"{self.config['exchange_name']}-scheduler",
                )

            state.component_status["api"] = "ready"
            state.accepting_admin = True
            state.ready = True
            self._started = True
            logger.info("%s is ready", self.config["exchange_name"])

    async def stop_components(self) -> None:
        """Stop ingestion first, flush last, and continue after close errors."""
        async with self._lifecycle_lock:
            if self._shutdown_complete:
                return

            state = self.app.state
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
                if not metrics_closed:
                    logger.error(
                        "Metrics persistence did not flush cleanly"
                    )
                    state.component_status["persistence"] = "degraded"
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
            logger.info("%s stopped", self.config["exchange_name"])

    def _on_order_submitted(self, order_id: int, command) -> None:
        self.metrics.record_order_submitted(order_id, command)
        self.latency_tracker.order_submitted(
            order_id,
            session_id=command.session_id,
            sim_submitted_at=command.submitted_at,
        )

    def _handle_order_arrived(self, command: ProcessOrderCommand) -> None:
        self.latency_tracker.order_arrived(command.order_id)

    def _handle_order_process(self, command: ProcessOrderCommand) -> None:
        order_id = command.order_id
        self.latency_tracker.order_processing_start(order_id)
        self.engine.process_order(command)
        self.latency_tracker.order_processing_end(order_id)

    def _handle_exec_report(self, report: ExecutionReport) -> None:
        if self.fix_gateway is not None:
            self.fix_gateway.process_execution_report(report)
        if (
            report.ord_status in TERMINAL_ORDER_STATUSES
            and self.latency_tracker.complete_order(report)
        ):
            self.latency_tracker.cleanup_order(report.order_id)

    def _on_execution_report(self, report: ExecutionReport) -> None:
        self.metrics.record_order(report)
        self.latency_tracker.report_created(report)

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
        scheduler_config = self.config.get("scheduler", {})
        interval_ms = int(scheduler_config.get("interval_ms", 10))
        if interval_ms <= 0:
            raise ValueError("scheduler.interval_ms must be positive")
        while self.running:
            if self.scheduler.state == SchedulerState.FAILED:
                await asyncio.sleep(interval_ms / 1000)
                continue
            if self.scheduler.state == SchedulerState.STOPPED:
                return
            try:
                await asyncio.sleep(interval_ms / 1000)
                target = self.scheduler.current_time + timedelta(
                    milliseconds=interval_ms
                )
                await self.scheduler.run_until(target)
            except asyncio.CancelledError:
                raise
            except SchedulerFailedError:
                self.app.state.component_status["scheduler"] = "failed"
                logger.exception(
                    "Realtime scheduler entered fail-stop state"
                )

    async def _run_api(self) -> None:
        admin_config = self.config.get("admin_api", {})
        config = uvicorn.Config(
            self.app,
            host=admin_config.get("host", "127.0.0.1"),
            port=int(admin_config["port"]),
            log_level="warning",
        )
        self.server = uvicorn.Server(config)
        await self.server.serve()

    def run(self) -> None:
        """Run Uvicorn; its lifespan owns FIX, scheduler, and persistence."""
        logger.info("Starting %s", self.config["exchange_name"])
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

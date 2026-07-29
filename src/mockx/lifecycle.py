"""Atomic simulation-session lifecycle coordination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import inspect
import logging
from typing import Any, Awaitable, Callable, Optional

from .identifiers import RunIdentifierScope
from .models import SchedulerState


logger = logging.getLogger(__name__)


class LifecycleState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    ENDING = "ENDING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class IngressToken:
    state: LifecycleState
    generation: int


@dataclass(frozen=True, slots=True)
class SessionStartResult:
    run_id: str
    run_generation: int
    starting_time: datetime


@dataclass(frozen=True, slots=True)
class SessionEndResult:
    run_id: Optional[str]
    run_generation: Optional[int]
    final_stats: Optional[Any]


class LifecycleError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class LifecycleIngressRejected(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExchangeLifecycle:
    """Sole serialization point for run transitions and simulation work."""

    def __init__(
        self,
        engine: Any,
        metrics: Any,
        latency_tracker: Any,
        fix_app: Any,
    ) -> None:
        self.engine = engine
        self.scheduler = engine.scheduler
        self.metrics = metrics
        self.latency_tracker = latency_tracker
        self.fix_app = fix_app
        self.lock = asyncio.Lock()
        self.state = LifecycleState.STOPPED
        self.run_id: Optional[str] = None
        self.run_generation = 0
        self._published_token = IngressToken(self.state, self.run_generation)
        self._close_websockets: Optional[Callable[[], Awaitable[None]]] = None
        self._stats_builder: Optional[Callable[[], Any]] = None

    @property
    def token(self) -> IngressToken:
        """Return the atomically published state tuple for QuickFIX threads."""
        return self._published_token

    def bind_api(
        self,
        *,
        close_websockets: Callable[[], Awaitable[None]],
        stats_builder: Callable[[], Any],
    ) -> None:
        self._close_websockets = close_websockets
        self._stats_builder = stats_builder

    def _publish(self, state: LifecycleState) -> None:
        self.state = state
        self._published_token = IngressToken(state, self.run_generation)

    def raise_if_transition_or_failed(self) -> None:
        state = self.state
        if state in {LifecycleState.STARTING, LifecycleState.ENDING}:
            raise LifecycleError(
                503,
                "LIFECYCLE_TRANSITION_IN_PROGRESS",
                "An exchange lifecycle transition is in progress",
            )
        if state == LifecycleState.FAILED:
            raise LifecycleError(
                503,
                "LIFECYCLE_FAILED",
                "The exchange lifecycle failed and requires a service restart",
            )

    def ingress_rejection(self, token: IngressToken) -> Optional[str]:
        if token.state in {LifecycleState.STARTING, LifecycleState.ENDING}:
            return "LIFECYCLE_TRANSITION_IN_PROGRESS"
        if token.state == LifecycleState.FAILED:
            return "LIFECYCLE_FAILED"
        if token.state != LifecycleState.ACTIVE:
            return "SESSION_NOT_ACTIVE"
        if self.scheduler.state == SchedulerState.FAILED:
            return "SCHEDULER_FAILED"
        return None

    async def execute_ingress(
        self,
        token: IngressToken,
        action: Callable[[], Any],
    ) -> Any:
        async with self.lock:
            current = self.token
            if (
                token.state != LifecycleState.ACTIVE
                or current.state != LifecycleState.ACTIVE
                or token.generation != current.generation
            ):
                code = self.ingress_rejection(current) or "LIFECYCLE_CHANGED"
                raise LifecycleIngressRejected(code)
            code = self.ingress_rejection(current)
            if code is not None:
                raise LifecycleIngressRejected(code)
            result = action()
            if inspect.isawaitable(result):
                return await result
            return result

    async def start_session(self, run_id: str) -> SessionStartResult:
        async with self.lock:
            self.raise_if_transition_or_failed()
            if self.state == LifecycleState.ACTIVE:
                raise LifecycleError(
                    409,
                    "SESSION_ALREADY_ACTIVE",
                    "A simulation session is already active",
                )

            self._publish(LifecycleState.STARTING)
            try:
                flush_result = await self.metrics.flush()
                if not flush_result.successful:
                    self._publish(LifecycleState.STOPPED)
                    raise LifecycleError(
                        503,
                        "METRICS_FLUSH_FAILED",
                        "Buffered metrics could not be flushed",
                    )
                if self._close_websockets is not None:
                    await self._close_websockets()
            except LifecycleError:
                raise
            except Exception as exc:
                self._publish(LifecycleState.STOPPED)
                logger.exception("Session start failed before destructive reset")
                raise LifecycleError(
                    503,
                    "SESSION_START_FAILED",
                    "Simulation session start failed before reset",
                ) from exc

            try:
                self.scheduler.reset()
                self.engine.reset_run_state()
                self.latency_tracker.reset(
                    preserve_order_keys=(
                        self.fix_app.delivery_queue.pending_order_keys()
                    ),
                    preserve_cancel_keys=(
                        self.fix_app.delivery_queue.pending_cancel_keys()
                    ),
                )
                self.metrics.reset_run_metrics()
                self.metrics.refresh_engine_gauges()
                self.engine.reseed_rng()

                generation = self.run_generation + 1
                identifiers = RunIdentifierScope(generation)
                self.engine.install_identifier_scope(identifiers)
                self.fix_app.reset_run_state(identifiers)
                self.metrics.start_run(run_id)
                for symbol in self.engine.books:
                    snapshot = self.engine.get_market_data_snapshot(symbol)
                    if snapshot is None:
                        raise RuntimeError(
                            f"Missing initial book snapshot for {symbol}"
                        )
                    self.metrics.update_book_metrics(snapshot)
                self.fix_app.reschedule_deliveries_after_reset()

                self.run_id = run_id
                self.run_generation = generation
                self._publish(LifecycleState.ACTIVE)
                logger.info(
                    "Simulation session started",
                    extra={
                        "simulation_session_id": run_id,
                        "run_generation": generation,
                    },
                )
                return SessionStartResult(
                    run_id=run_id,
                    run_generation=generation,
                    starting_time=self.scheduler.current_time,
                )
            except Exception as exc:
                self.metrics.end_run()
                self.run_id = None
                self._publish(LifecycleState.FAILED)
                logger.exception("Session start failed after destructive reset")
                raise LifecycleError(
                    503,
                    "SESSION_START_FAILED",
                    "Simulation session start failed; service restart required",
                ) from exc

    async def end_session(self) -> SessionEndResult:
        async with self.lock:
            self.raise_if_transition_or_failed()
            if self.state == LifecycleState.STOPPED:
                return SessionEndResult(None, None, None)

            run_id = self.run_id
            generation = self.run_generation
            self._publish(LifecycleState.ENDING)
            try:
                flush_result = await self.metrics.flush()
                if not flush_result.successful:
                    self._publish(LifecycleState.ACTIVE)
                    raise LifecycleError(
                        503,
                        "METRICS_FLUSH_FAILED",
                        "Buffered metrics could not be flushed",
                    )
                if self._stats_builder is None:
                    raise RuntimeError("Lifecycle statistics builder is not bound")
                final_stats = self._stats_builder()
            except LifecycleError:
                raise
            except Exception as exc:
                self._publish(LifecycleState.ACTIVE)
                logger.exception("Session end failed before the run was closed")
                raise LifecycleError(
                    503,
                    "SESSION_END_FAILED",
                    "Simulation session end failed; the run remains active",
                ) from exc

            self.metrics.end_run()
            self.run_id = None
            self._publish(LifecycleState.STOPPED)
            logger.info(
                "Simulation session ended",
                extra={
                    "simulation_session_id": run_id,
                    "run_generation": generation,
                },
            )
            return SessionEndResult(run_id, generation, final_stats)

    async def advance_to(self, target_time: datetime) -> int:
        return await self.execute_administrative_advance(
            lambda: self.scheduler.run_until(target_time)
        )

    async def execute_administrative_advance(
        self,
        action: Callable[[], Any],
    ) -> Any:
        """Run the complete administrative advancement under the lifecycle lock."""
        observed = self.token
        self.raise_if_transition_or_failed()
        barrier = await self.fix_app.await_ingress_enqueued(
            generation=observed.generation,
        )
        async with self.lock:
            self.raise_if_transition_or_failed()
            if observed != self.token:
                raise LifecycleError(
                    503,
                    "LIFECYCLE_TRANSITION_IN_PROGRESS",
                    "The advancement request crossed a lifecycle transition",
                )
            if self.state != LifecycleState.ACTIVE:
                raise LifecycleError(
                    409,
                    "SESSION_NOT_ACTIVE",
                    "Administrative time advancement requires an active session",
                )
            self.fix_app.drain_ingress_locked(
                barrier.run_generation,
                barrier.through_sequence,
            )
            result = action()
            if inspect.isawaitable(result):
                return await result
            return result

    async def advance_realtime(self, target_time: datetime) -> int:
        observed = self.token
        if observed.state != LifecycleState.ACTIVE:
            return 0
        barrier = await self.fix_app.await_ingress_enqueued(
            generation=observed.generation,
        )
        async with self.lock:
            if self.token != observed:
                return 0
            self.fix_app.drain_ingress_locked(
                barrier.run_generation,
                barrier.through_sequence,
            )
            return await self.scheduler.run_until(target_time)

    async def advance_realtime_by(self, delta: timedelta) -> int:
        """Compute and commit one realtime tick under the lifecycle lock."""
        observed = self.token
        if observed.state != LifecycleState.ACTIVE:
            return 0
        barrier = await self.fix_app.await_ingress_enqueued(
            generation=observed.generation,
        )
        async with self.lock:
            if self.token != observed:
                return 0
            self.fix_app.drain_ingress_locked(
                barrier.run_generation,
                barrier.through_sequence,
            )
            target_time = self.scheduler.current_time + delta
            return await self.scheduler.run_until(target_time)

    async def websocket_snapshot(
        self,
        action: Callable[[], Awaitable[Any]],
    ) -> Any:
        observed = self.token
        self.raise_if_transition_or_failed()
        async with self.lock:
            self.raise_if_transition_or_failed()
            if observed != self.token:
                raise LifecycleError(
                    503,
                    "LIFECYCLE_TRANSITION_IN_PROGRESS",
                    "The WebSocket registration crossed a lifecycle transition",
                )
            return await action()

    async def broadcast_if_current(
        self,
        generation: int,
        action: Callable[[], Awaitable[None]],
    ) -> bool:
        async with self.lock:
            if (
                self.state != LifecycleState.ACTIVE
                or generation != self.run_generation
            ):
                return False
            await action()
            return True

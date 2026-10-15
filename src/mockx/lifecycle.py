"""Atomic simulation-session lifecycle coordination."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from uuid import uuid4
import inspect
import logging
from typing import Any, Awaitable, Callable, Optional, TypeVar

from .effects import BookCheckpoint, BookOrderState
from .journal import JournalFinalizationError
from .latency import datetime_to_ns
from .glimpse_encoder import GlimpseOrderSnapshot, GlimpseSnapshot
from .identifiers import RunIdentifierScope
from .models import SchedulerState


logger = logging.getLogger(__name__)
_TransitionResult = TypeVar("_TransitionResult")


class LifecycleState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    RUN_FAILED = "RUN_FAILED"
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
    scenario_label: str


@dataclass(frozen=True, slots=True)
class SessionEndResult:
    run_id: Optional[str]
    run_generation: Optional[int]
    final_stats: Optional[Any]


@dataclass(frozen=True, slots=True)
class RunFailure:
    code: str
    message: str
    run_id: str
    run_generation: int
    failed_at: datetime
    reason: str
    metric_event_sequence: Optional[int]


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
        itch_feed: Any = None,
        *,
        journal: Any = None,
    ) -> None:
        self.engine = engine
        self.scheduler = engine.scheduler
        self.metrics = metrics
        self.latency_tracker = latency_tracker
        self.fix_app = fix_app
        self.itch_feed = itch_feed
        self.journal = journal
        self.command_executor: Optional[Callable] = None
        self.scenario_label: Optional[str] = None
        self._pending_failure: Optional[tuple[str, str]] = None
        self.lock = asyncio.Lock()
        self.state = LifecycleState.STOPPED
        self.run_id: Optional[str] = None
        self.run_generation = 0
        self._published_token = IngressToken(self.state, self.run_generation)
        self._stats_builder: Optional[Callable[[], Any]] = None
        self.glimpse_service: Optional[Any] = None
        self.run_failure: Optional[RunFailure] = None
        self._command_depth = 0
        self.metrics.on_integrity_failure = self._request_persistence_check

    @asynccontextmanager
    async def _serialized(self):
        async with self.lock:
            self.check_persistence_failure()
            try:
                yield
            finally:
                self.check_persistence_failure()

    @property
    def failure_message(self) -> str:
        failure = self.run_failure
        return f"{failure.code}: {failure.message}" if failure else "RUN_INCOMPLETE"

    def request_failure(self, code: str, reason: str) -> None:
        if self._pending_failure is None and self.run_failure is None:
            self._pending_failure = (code, reason)
        self.check_persistence_failure()

    def _request_persistence_check(self) -> None:
        """Marshal writer/QuickFIX notifications onto the lifecycle owner loop."""
        loop = self.fix_app.loop
        if loop is None or loop.is_closed():
            return

        def apply_failure() -> None:
            # An in-flight lifecycle operation checks again before releasing
            # its lock. Otherwise this synchronous owner-loop callback is the
            # serialization boundary; it cannot interrupt an engine command.
            if not self.lock.locked():
                self.check_persistence_failure()

        loop.call_soon_threadsafe(apply_failure)

    def check_persistence_failure(
        self, *, on_rejected: Optional[Callable[[], Any]] = None
    ) -> bool:
        """Fail at an owner-loop command boundary, never inside matching."""
        if self.state == LifecycleState.RUN_FAILED:
            if on_rejected is not None:
                on_rejected()
            return True
        failure = self.metrics.persistence_integrity_failure
        if (
            self._command_depth
            or (failure is None and self._pending_failure is None)
            or self.run_id is None
            or self.state
            not in {
                LifecycleState.ACTIVE,
                LifecycleState.STARTING,
                LifecycleState.ENDING,
            }
        ):
            return False
        if self.run_failure is not None:
            self._publish(LifecycleState.RUN_FAILED)
            if on_rejected is not None:
                on_rejected()
            return True
        code, reason = self._pending_failure or ("JOURNAL_INCOMPLETE", failure.reason)
        self.run_failure = RunFailure(
            code=code,
            message="The run is incomplete; start a fresh run.",
            run_id=self.run_id,
            run_generation=self.run_generation,
            failed_at=self.scheduler.current_time,
            reason=reason,
            metric_event_sequence=(
                failure.metric_event_sequence if failure is not None else None
            ),
        )
        self._pending_failure = None
        if self.glimpse_service is not None:
            self.glimpse_service.revoke_run(self.run_generation)
        self._publish(LifecycleState.RUN_FAILED)
        if on_rejected is not None:
            on_rejected()
        self.fix_app.fail_pending_commands(
            f"{self.run_failure.code}: {self.run_failure.message}"
        )
        logger.error(
            self.run_failure.message,
            extra={"run_id": self.run_id, "failure_reason": reason},
        )
        return True

    def execute_command(
        self,
        action: Callable[[], Any],
        *,
        on_rejected: Callable[[], Any],
    ) -> Any:
        """Finish the entire synchronous handler before honoring metric loss."""
        if self.check_persistence_failure(on_rejected=on_rejected):
            return None
        self._command_depth += 1
        try:
            return action()
        finally:
            self._command_depth -= 1
            self.check_persistence_failure()

    @property
    def token(self) -> IngressToken:
        """Return the atomically published state tuple for QuickFIX threads."""
        return self._published_token

    def bind_api(
        self,
        *,
        stats_builder: Callable[[], Any],
    ) -> None:
        self._stats_builder = stats_builder

    def bind_glimpse_service(self, service: Any) -> None:
        self.glimpse_service = service

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
        if token.state == LifecycleState.RUN_FAILED or (
            token.state == LifecycleState.ACTIVE and not self.metrics.integrity_complete
        ):
            return self.run_failure.code if self.run_failure else "JOURNAL_INCOMPLETE"
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
        async with self._serialized():
            self.check_persistence_failure()
            current = self.token
            if (
                token.state != LifecycleState.ACTIVE
                or current.state != LifecycleState.ACTIVE
                or token.generation != current.generation
            ):
                code = self.ingress_rejection(current) or "LIFECYCLE_CHANGED"
                raise LifecycleIngressRejected(code)
            current_rejection = self.ingress_rejection(current)
            if current_rejection is not None:
                raise LifecycleIngressRejected(current_rejection)
            self._command_depth += 1
            try:
                result = (
                    self.command_executor(action)
                    if self.command_executor is not None
                    else action()
                )
                if inspect.isawaitable(result):
                    result = await result
            finally:
                self._command_depth -= 1
                self.check_persistence_failure()
            return result

    async def _complete_transition(
        self,
        task: asyncio.Task[_TransitionResult],
        *,
        operation: str,
        run_id: Optional[str],
    ) -> _TransitionResult:
        """Retain the caller's lock until its accepted transition settles."""
        caller = asyncio.current_task()
        cancellation: Optional[asyncio.CancelledError] = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                # A shield also raises when its child is internally canceled.
                # Never retry a settled child, or this becomes a busy loop.
                if not task.cancelled() or (caller is not None and caller.cancelling()):
                    if cancellation is None:
                        cancellation = exc
                if task.done():
                    break
            except Exception:
                # Retrieve the settled exception below, including when the
                # caller has already canceled and cannot receive that failure.
                break

        if cancellation is not None:
            try:
                task.result()
            except (Exception, asyncio.CancelledError):
                logger.exception(
                    "Session %s failed after caller cancellation",
                    operation,
                    extra={
                        "operation": operation,
                        "simulation_session_id": run_id,
                        "run_generation": self.run_generation,
                    },
                )
            raise cancellation
        return task.result()

    def _fail_start_after_reset(self) -> None:
        """Revoke an incomplete run without stopping process-owned listeners."""
        try:
            if self.glimpse_service is not None:
                self.glimpse_service.revoke_all()
        except Exception:
            logger.exception("GLIMPSE revocation failed after session start failure")
        try:
            if self.itch_feed is not None:
                self.itch_feed.end_run(self.scheduler.current_time)
        except Exception:
            logger.exception("ITCH cleanup failed after session start failure")
        finally:
            self.metrics.end_run()
            self.run_id = None
            self._publish(LifecycleState.FAILED)

    async def start_session(self, scenario_label: str) -> SessionStartResult:
        async with self._serialized():
            self.raise_if_transition_or_failed()
            if self.state == LifecycleState.RUN_FAILED:
                raise LifecycleError(
                    409,
                    "RUN_REQUIRES_END",
                    "End the failed run before starting a new run",
                )
            if self.state == LifecycleState.ACTIVE:
                raise LifecycleError(
                    409,
                    "SESSION_ALREADY_ACTIVE",
                    "A simulation session is already active",
                )
            if self.itch_feed is not None:
                try:
                    self.itch_feed.validate_generation(self.run_generation + 1)
                except ValueError as exc:
                    raise LifecycleError(
                        409,
                        "ITCH_GENERATION_EXHAUSTED",
                        "The next run generation cannot form an ITCH session",
                    ) from exc

            run_id = str(uuid4())
            self._publish(LifecycleState.STARTING)
            task = asyncio.create_task(
                self._start_session(run_id, scenario_label),
                name=f"session-start:{run_id}",
            )
            try:
                return await self._complete_transition(
                    task, operation="start", run_id=run_id
                )
            except asyncio.CancelledError:
                # A child canceled before its first step cannot run its own
                # phase cleanup. No reset has occurred in this case.
                if self.state == LifecycleState.STARTING:
                    self._publish(LifecycleState.STOPPED)
                raise

    async def _start_session(
        self, run_id: str, scenario_label: str
    ) -> SessionStartResult:
        """Finish an accepted start; the caller owns the lifecycle lock."""
        try:
            flush_result = await self.metrics.flush()
            if not flush_result.successful:
                self._publish(LifecycleState.STOPPED)
                raise LifecycleError(
                    503,
                    "METRICS_FLUSH_FAILED",
                    "Buffered metrics could not be flushed",
                )
        except asyncio.CancelledError:
            self._publish(LifecycleState.STOPPED)
            raise
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
            if self.glimpse_service is not None:
                self.glimpse_service.revoke_all()
            self.scheduler.reset()
            self.engine.reset_run_state()
            self.latency_tracker.reset(
                preserve_order_keys=(self.fix_app.delivery_queue.pending_order_keys()),
                preserve_cancel_keys=(
                    self.fix_app.delivery_queue.pending_cancel_keys()
                ),
                preserve_replace_keys=self.fix_app.delivery_queue.pending_replace_keys(),
            )
            self.metrics.reset_run_metrics()
            self.metrics.refresh_engine_gauges()
            self.engine.reseed_rng()

            generation = self.run_generation + 1
            identifiers = RunIdentifierScope(generation)
            self.engine.install_identifier_scope(identifiers)
            self.fix_app.reset_run_state(identifiers)
            self.metrics.start_run(run_id)
            self.engine.run_id = run_id
            self.scenario_label = scenario_label
            self.run_id = run_id
            self.run_generation = generation
            self.run_failure = None
            self._pending_failure = None
            self.fix_app.reschedule_deliveries_after_reset()
            if self.journal is not None:
                await self.journal.start_run(
                    run_id,
                    scenario_label,
                    tuple(
                        self.engine.instruments.require(s)
                        for s in self.engine.instruments.symbols
                    ),
                    self.scheduler.current_time,
                    {"run_generation": generation},
                )
            if self.check_persistence_failure():
                raise LifecycleError(
                    503, self.run_failure.code, self.run_failure.message
                )

            if self.itch_feed is not None:
                if not await self.itch_feed.start_run(
                    generation, self.scheduler.current_time
                ):
                    self.request_failure(
                        "ITCH_PUBLISHER_FAILED",
                        "Required ITCH publisher could not start",
                    )
                    raise LifecycleError(
                        503, self.run_failure.code, self.run_failure.message
                    )
            if self.glimpse_service is not None:
                if not await self.glimpse_service.start():
                    self.request_failure(
                        "GLIMPSE_LISTENER_FAILED",
                        "Required GLIMPSE listener could not start",
                    )
                    raise LifecycleError(
                        503, self.run_failure.code, self.run_failure.message
                    )
                self.glimpse_service.begin_run(generation)

            if self.check_persistence_failure():
                raise LifecycleError(
                    503, self.run_failure.code, self.run_failure.message
                )
            if self.journal is not None:
                await self.journal.checkpoint(self._capture_book(), reason="start")
                flushed = await self.metrics.flush()
                if not flushed.successful:
                    raise RuntimeError("Initial checkpoint was not acknowledged")
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
                scenario_label=scenario_label,
            )
        except asyncio.CancelledError:
            self._fail_start_after_reset()
            raise
        except Exception as exc:
            if (
                isinstance(exc, LifecycleError)
                and self.state == LifecycleState.RUN_FAILED
            ):
                raise
            self._fail_start_after_reset()
            logger.exception("Session start failed after destructive reset")
            raise LifecycleError(
                503,
                "SESSION_START_FAILED",
                "Simulation session start failed; service restart required",
            ) from exc

    async def end_session(self) -> SessionEndResult:
        async with self._serialized():
            self.raise_if_transition_or_failed()
            self.check_persistence_failure()
            if self.state == LifecycleState.STOPPED:
                return SessionEndResult(None, None, None)

            run_id = self.run_id
            generation = self.run_generation
            restore_state = self.state
            self._publish(LifecycleState.ENDING)
            task = asyncio.create_task(
                self._end_session(run_id, generation, restore_state),
                name=f"session-end:{run_id}",
            )
            try:
                return await self._complete_transition(
                    task, operation="end", run_id=run_id
                )
            except asyncio.CancelledError:
                # Cover cancellation before the child's first step as well as
                # the phase cleanup performed by a child that already ran.
                if self.state == LifecycleState.ENDING:
                    if self.check_persistence_failure():
                        restore_state = LifecycleState.RUN_FAILED
                    self._publish(restore_state)
                raise

    async def _end_session(
        self,
        run_id: Optional[str],
        generation: int,
        restore_state: LifecycleState,
    ) -> SessionEndResult:
        """Finish an accepted end; the caller owns the lifecycle lock."""
        closing_phase = False
        try:
            self.check_persistence_failure()
            if self.itch_feed is not None:
                self.itch_feed.flush()
            if self.journal is not None:
                if (
                    self.run_failure is None
                    and self.scheduler.state != SchedulerState.FAILED
                ):
                    await self.journal.checkpoint(self._capture_book(), reason="end")
            flush_result = await self.metrics.flush()
            if not flush_result.successful:
                self._publish(restore_state)
                raise LifecycleError(
                    503,
                    "JOURNAL_FLUSH_FAILED",
                    "Journal acknowledgment failed; the run remains open",
                )
            closing_phase = True
            if self.glimpse_service is not None and generation is not None:
                self.glimpse_service.revoke_run(generation)
            if self.itch_feed is not None:
                self.itch_feed.end_run(self.scheduler.current_time)
            self.check_persistence_failure()
            if self.journal is not None:
                await self.journal.end_run(
                    self.scheduler.current_time,
                    failed=self.run_failure is not None,
                    reason=self.failure_message if self.run_failure else None,
                )
            if self._stats_builder is None:
                raise RuntimeError("Lifecycle statistics builder is not bound")
            final_stats = self._stats_builder()
        except asyncio.CancelledError:
            if closing_phase:
                self.request_failure(
                    "SESSION_END_CANCELLED",
                    "Run finalization was cancelled after native feed closure",
                )
            if self.check_persistence_failure():
                restore_state = LifecycleState.RUN_FAILED
            self._publish(restore_state)
            raise
        except LifecycleError:
            raise
        except Exception as exc:
            if isinstance(exc, JournalFinalizationError):
                self.request_failure("JOURNAL_END_UNCONFIRMED", str(exc))
            elif closing_phase:
                self.request_failure(
                    "SESSION_END_FAILED",
                    "Run finalization failed after native feed closure",
                )
            if self.check_persistence_failure():
                restore_state = LifecycleState.RUN_FAILED
            self._publish(restore_state)
            logger.exception("Session end failed before the run was closed")
            raise LifecycleError(
                503,
                "SESSION_END_FAILED",
                "Simulation session end failed; the run remains open",
            ) from exc

        self.metrics.end_run()
        if self.journal is not None:
            self.journal.detach_run()
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

    def _capture_book(self) -> BookCheckpoint:
        """Copy FIFO state once, independently of wire encoding or persistence."""
        if self.run_id is None:
            raise LifecycleError(
                409, "SESSION_NOT_ACTIVE", "A snapshot requires an active run"
            )
        if self.itch_feed is not None:
            self.itch_feed.flush()
        orders = []
        for symbol in self.engine.books:
            book = self.engine.books[symbol]
            book.assert_invariants()
            for levels in (book.bids, book.asks):
                for level in levels:
                    for order in level.orders:
                        if (
                            order.run_generation != self.run_generation
                            or order.price_ticks is None
                        ):
                            raise RuntimeError(
                                f"Invalid resting order {order.id} in {symbol}"
                            )
                        orders.append(
                            BookOrderState(
                                order_id=order.displayed_order_ref,
                                logical_order_id=order.id,
                                symbol=symbol,
                                side=order.side,
                                price_ticks=order.price_ticks,
                                qty_lots=order.leaves_qty_lots,
                                liquidity_category=order.liquidity_category.value,
                            )
                        )
        return BookCheckpoint(
            self.run_id,
            self.scheduler.current_time,
            self.itch_feed.sequence_number if self.itch_feed else 1,
            tuple(orders),
        )

    async def capture_checkpoint(self):
        async with self._serialized():
            if (
                self.state != LifecycleState.ACTIVE
                or self.scheduler.state != SchedulerState.RUNNING
            ):
                raise LifecycleError(
                    409, "RUN_NOT_COHERENT", "Checkpoint requires a coherent active run"
                )
            if self.journal is None:
                raise LifecycleError(
                    503, "JOURNAL_UNAVAILABLE", "Journal is unavailable"
                )
            snapshot = self._capture_book()

            async def settle():
                try:
                    result = await self.journal.checkpoint(snapshot)
                    self.metrics.note_journal_sequence(
                        result, datetime_to_ns(snapshot.timestamp)
                    )
                    flushed = await self.metrics.flush()
                    if not flushed.successful:
                        raise RuntimeError("Checkpoint acknowledgment failed")
                    return result
                except Exception as exc:
                    self.metrics.mark_integrity_failure(str(exc))
                    self.request_failure("JOURNAL_INCOMPLETE", str(exc))
                    raise LifecycleError(
                        503,
                        "CHECKPOINT_FAILED",
                        "Checkpoint admission or acknowledgment failed",
                    ) from exc

            task = asyncio.create_task(settle(), name=f"checkpoint:{self.run_id}")
            return await self._complete_transition(
                task, operation="checkpoint", run_id=self.run_id
            )

    async def capture_glimpse_snapshot(self) -> GlimpseSnapshot:
        observed = self.token
        async with self._serialized():
            feed = self.itch_feed
            if (
                self.state != LifecycleState.ACTIVE
                or observed != self.token
                or self.scheduler.state != SchedulerState.RUNNING
                or feed is None
                or feed.state.value != "ready"
                or feed.active_generation != self.run_generation
            ):
                from .glimpse_service import GlimpseSessionUnavailable

                raise GlimpseSessionUnavailable("A coherent active feed is required")
            snapshot = self._capture_book()
            return GlimpseSnapshot(
                self.run_generation,
                snapshot.timestamp,
                snapshot.next_itch_sequence,
                tuple(
                    GlimpseOrderSnapshot(
                        o.order_id, o.symbol, o.side, o.qty_lots, o.price_ticks
                    )
                    for o in snapshot.orders
                ),
            )

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
        async with self._serialized():
            self.raise_if_transition_or_failed()
            if observed != self.token:
                raise LifecycleError(
                    503,
                    "LIFECYCLE_TRANSITION_IN_PROGRESS",
                    "The advancement request crossed a lifecycle transition",
                )
            if self.state not in {LifecycleState.ACTIVE, LifecycleState.RUN_FAILED}:
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
                result = await result
            self.check_persistence_failure()
            return result

    async def advance_realtime(self, target_time: datetime) -> int:
        observed = self.token
        if observed.state not in {LifecycleState.ACTIVE, LifecycleState.RUN_FAILED}:
            return 0
        barrier = await self.fix_app.await_ingress_enqueued(
            generation=observed.generation,
        )
        async with self._serialized():
            if self.token != observed:
                return 0
            self.fix_app.drain_ingress_locked(
                barrier.run_generation,
                barrier.through_sequence,
            )
            result = await self.scheduler.run_until(target_time)
            self.check_persistence_failure()
            return result

    async def advance_realtime_by(self, delta: timedelta) -> int:
        """Compute and commit one realtime tick under the lifecycle lock."""
        observed = self.token
        if observed.state not in {LifecycleState.ACTIVE, LifecycleState.RUN_FAILED}:
            return 0
        barrier = await self.fix_app.await_ingress_enqueued(
            generation=observed.generation,
        )
        async with self._serialized():
            if self.token != observed:
                return 0
            self.fix_app.drain_ingress_locked(
                barrier.run_generation,
                barrier.through_sequence,
            )
            target_time = self.scheduler.current_time + delta
            result = await self.scheduler.run_until(target_time)
            self.check_persistence_failure()
            return result

"""Atomic simulation-session lifecycle coordination."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import inspect
import logging
from typing import Any, Awaitable, Callable, Optional, TypeVar

from .engine import DuplicateClientOrderID, InitialBookState, SeededOrder
from .glimpse_encoder import GlimpseOrderSnapshot, GlimpseSnapshot
from .identifiers import RunIdentifierScope
from .latency import UNIX_EPOCH
from .models import OrderBookInvariantError, SchedulerState
from .validation import OrderValidationError


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
    seeded_orders: tuple[SeededOrder, ...] = ()


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
    ) -> None:
        self.engine = engine
        self.scheduler = engine.scheduler
        self.metrics = metrics
        self.latency_tracker = latency_tracker
        self.fix_app = fix_app
        self.itch_feed = itch_feed
        self.lock = asyncio.Lock()
        self.state = LifecycleState.STOPPED
        self.run_id: Optional[str] = None
        self.run_generation = 0
        self._published_token = IngressToken(self.state, self.run_generation)
        self._close_websockets: Optional[Callable[[], Awaitable[None]]] = None
        self._stats_builder: Optional[Callable[[], Any]] = None
        self.glimpse_service: Optional[Any] = None
        self.run_failure: Optional[RunFailure] = None
        self._command_depth = 0
        self.metrics.on_integrity_failure = self._request_persistence_check
        self._drain_broadcasts: Optional[Callable[[], Awaitable[None]]] = None
        self._has_pending_broadcasts: Optional[Callable[[], bool]] = None

    def bind_market_data_delivery(
        self,
        drain: Callable[[], Awaitable[None]],
        pending: Callable[[], bool],
    ) -> None:
        self._drain_broadcasts = drain
        self._has_pending_broadcasts = pending

    @asynccontextmanager
    async def _serialized(self, *, drain_broadcasts: bool = False):
        while True:
            # Broadcast tasks acquire this same lock. Await them before taking
            # it, then recheck so an intervening command cannot lose its output.
            if drain_broadcasts and self._drain_broadcasts is not None:
                await self._drain_broadcasts()
            async with self.lock:
                if (
                    drain_broadcasts
                    and self._has_pending_broadcasts is not None
                    and self._has_pending_broadcasts()
                ):
                    continue
                try:
                    yield
                finally:
                    self.check_persistence_failure()
                return

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
            or failure is None
            or self.run_id is None
            or self.state not in {
                LifecycleState.ACTIVE, LifecycleState.STARTING, LifecycleState.ENDING
            }
        ):
            return False
        if self.run_failure is not None:
            # ENDING temporarily changes lifecycle state, never the first fault.
            self._publish(LifecycleState.RUN_FAILED)
            if on_rejected is not None:
                on_rejected()
            return True
        self.run_failure = RunFailure(
            code="METRICS_INCOMPLETE",
            message="Metrics were lost; this simulation must be rerun.",
            run_id=self.run_id,
            run_generation=self.run_generation,
            failed_at=self.scheduler.current_time,
            reason=failure.reason,
            metric_event_sequence=failure.metric_event_sequence,
        )
        self._publish(LifecycleState.RUN_FAILED)
        if on_rejected is not None:
            on_rejected()
        self.fix_app.fail_pending_commands(
            f"{self.run_failure.code}: {self.run_failure.message}"
        )
        logger.error(
            self.run_failure.message,
            extra={"run_id": self.run_id, "persistence_failure_reason": failure.reason},
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
        close_websockets: Callable[[], Awaitable[None]],
        stats_builder: Callable[[], Any],
    ) -> None:
        self._close_websockets = close_websockets
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
            return "METRICS_INCOMPLETE"
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
                result = action()
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

    async def start_session(
        self, run_id: str, snapshot: tuple[dict[str, str], ...] = (),
    ) -> SessionStartResult:
        async with self._serialized():
            self.raise_if_transition_or_failed()
            if self.state == LifecycleState.RUN_FAILED:
                raise LifecycleError(
                    409, "RUN_REQUIRES_END", "End the failed run before starting a new run"
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

            try:
                commands = self.fix_app.normalize_initial_snapshot(
                    snapshot, submitted_at=UNIX_EPOCH,
                )
                initial = self.engine.prepare_initial_book(
                    commands, RunIdentifierScope(self.run_generation + 1),
                )
            except DuplicateClientOrderID as exc:
                raise LifecycleError(409, "DUPLICATE_CL_ORD_ID", str(exc)) from exc
            except OrderValidationError as exc:
                raise LifecycleError(422, exc.code.value, exc.message) from exc
            except OrderBookInvariantError as exc:
                raise LifecycleError(422, "INVALID_SNAPSHOT", str(exc)) from exc

            self._publish(LifecycleState.STARTING)
            task = asyncio.create_task(
                self._start_session(run_id, initial), name=f"session-start:{run_id}"
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
        self, run_id: str, initial: InitialBookState,
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
            if self._close_websockets is not None:
                await self._close_websockets()
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
            self.fix_app.claim_initial_snapshot(initial)
        except DuplicateClientOrderID as exc:
            self._publish(LifecycleState.STOPPED)
            raise LifecycleError(409, "DUPLICATE_CL_ORD_ID", str(exc)) from exc

        try:
            if self.glimpse_service is not None:
                self.glimpse_service.revoke_all()
            previous_sim_time = self.scheduler.current_time
            self.scheduler.reset()
            self.engine.reset_run_state()
            self.latency_tracker.reset(
                previous_sim_time=previous_sim_time,
                preserve_order_keys=(
                    self.fix_app.delivery_queue.pending_order_keys()
                ),
                preserve_cancel_keys=(
                    self.fix_app.delivery_queue.pending_cancel_keys()
                ),
                preserve_replace_keys=(
                    self.fix_app.delivery_queue.pending_replace_keys()
                ),
            )
            self.metrics.reset_run_metrics()
            self.metrics.refresh_engine_gauges()
            self.engine.reseed_rng()

            generation = self.run_generation + 1
            identifiers = initial.identifiers
            self.engine.install_identifier_scope(identifiers)
            self.fix_app.reset_run_state(identifiers)
            self.engine.install_initial_book(initial)
            for order in initial.orders.values():
                self.latency_tracker.snapshot_order_initialized(
                    order.id, order.session_id, generation,
                )
            self.metrics.start_run(run_id)
            self.run_id = run_id
            self.run_generation = generation
            self.run_failure = None
            self.fix_app.reschedule_deliveries_after_reset()
            seeded_symbols = {order.symbol for order in initial.orders.values()}
            for symbol in self.engine.books:
                if symbol in seeded_symbols:
                    # The committed feed publication below also records this snapshot.
                    continue
                snapshot = self.engine.get_market_data_snapshot(symbol)
                if snapshot is None:
                    raise RuntimeError(
                        f"Missing initial book snapshot for {symbol}"
                    )
                self.metrics.update_book_metrics(snapshot)
            if self.check_persistence_failure():
                raise LifecycleError(
                    503, self.run_failure.code, self.run_failure.message
                )

            if self.itch_feed is not None:
                await self.itch_feed.start_run(
                    generation,
                    self.scheduler.current_time,
                )
            self.engine.publish_initial_book(initial)
            if self.glimpse_service is not None:
                await self.glimpse_service.start()
                self.glimpse_service.begin_run(generation)

            if self.check_persistence_failure():
                raise LifecycleError(
                    503, self.run_failure.code, self.run_failure.message
                )
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
                seeded_orders=initial.seeded_orders,
            )
        except asyncio.CancelledError:
            self._fail_start_after_reset()
            raise
        except Exception as exc:
            if isinstance(exc, LifecycleError) and self.state == LifecycleState.RUN_FAILED:
                raise
            self._fail_start_after_reset()
            logger.exception("Session start failed after destructive reset")
            raise LifecycleError(
                503,
                "SESSION_START_FAILED",
                "Simulation session start failed; service restart required",
            ) from exc

    async def end_session(self) -> SessionEndResult:
        async with self._serialized(drain_broadcasts=True):
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
        try:
            failure_before_flush = self.run_failure
            flush_result = await self.metrics.flush()
            self.check_persistence_failure()
            if self.state == LifecycleState.RUN_FAILED:
                restore_state = LifecycleState.RUN_FAILED
            if (
                flush_result.successful
                and failure_before_flush is None
                and self.run_failure is not None
            ):
                # Settling commands after a newly observed fault may have
                # admitted rejection metrics after the first drain ended.
                flush_result = await self.metrics.flush()
            if not flush_result.successful:
                self._publish(restore_state)
                raise LifecycleError(
                    503,
                    "METRICS_FLUSH_FAILED",
                    "Buffered metrics could not be flushed",
                )
            if self.glimpse_service is not None and generation is not None:
                self.glimpse_service.revoke_run(generation)
            if self.itch_feed is not None:
                self.itch_feed.end_run(self.scheduler.current_time)
            if self._stats_builder is None:
                raise RuntimeError("Lifecycle statistics builder is not bound")
            final_stats = self._stats_builder()
        except asyncio.CancelledError:
            if self.check_persistence_failure():
                restore_state = LifecycleState.RUN_FAILED
            self._publish(restore_state)
            raise
        except LifecycleError:
            raise
        except Exception as exc:
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

    async def capture_glimpse_snapshot(self) -> GlimpseSnapshot:
        """Capture one book/ITCH cut without retaining live engine objects."""

        observed = self.token
        self.raise_if_transition_or_failed()
        async with self._serialized():
            self.raise_if_transition_or_failed()
            if observed != self.token:
                raise LifecycleError(
                    503,
                    "LIFECYCLE_TRANSITION_IN_PROGRESS",
                    "The GLIMPSE request crossed a lifecycle transition",
                )
            if self.state not in {LifecycleState.ACTIVE, LifecycleState.RUN_FAILED}:
                raise LifecycleError(
                    409,
                    "SESSION_NOT_ACTIVE",
                    "GLIMPSE snapshots require an active simulation session",
                )
            if self.scheduler.state == SchedulerState.FAILED:
                raise LifecycleError(
                    503,
                    "SCHEDULER_FAILED",
                    "GLIMPSE snapshots are unavailable while the scheduler is failed",
                )
            feed = self.itch_feed
            if (
                feed is None
                or feed.state.value != "ready"
                or feed.active_generation != self.run_generation
            ):
                raise LifecycleError(
                    503,
                    "ITCH_UNAVAILABLE",
                    "GLIMPSE snapshots require the current coherent ITCH session",
                )

            orders: list[GlimpseOrderSnapshot] = []
            for symbol in feed.encoder.symbols:
                book = self.engine.books[symbol]
                book.assert_invariants()
                for levels in (book.bids, book.asks):
                    for level in levels:
                        for order in level.orders:
                            if order.run_generation != self.run_generation:
                                raise RuntimeError(
                                    f"Stale generation order {order.id} in {symbol}"
                                )
                            if order.price_ticks is None:
                                raise RuntimeError(
                                    f"Resting order {order.id} has no limit price"
                                )
                            orders.append(
                                GlimpseOrderSnapshot(
                                    order_ref_num=order.displayed_order_ref,
                                    symbol=symbol,
                                    side=order.side,
                                    qty_lots=order.leaves_qty_lots,
                                    price_ticks=order.price_ticks,
                                )
                            )
            return GlimpseSnapshot(
                run_generation=self.run_generation,
                timestamp=self.scheduler.current_time,
                itch_sequence=feed.sequence_number,
                orders=tuple(orders),
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

    async def websocket_snapshot(
        self,
        action: Callable[[], Awaitable[Any]],
    ) -> Any:
        observed = self.token
        self.raise_if_transition_or_failed()
        async with self._serialized():
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
        async with self._serialized():
            if (
                self.state not in {LifecycleState.ACTIVE, LifecycleState.RUN_FAILED}
                or generation != self.run_generation
            ):
                return False
            await action()
            return True

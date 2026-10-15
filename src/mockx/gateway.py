"""FIX protocol gateway for the mock exchange."""

try:
    import quickfix as fix
except ModuleNotFoundError:  # Pure report mapping and non-FIX tests remain importable.
    fix = None
import asyncio
from dataclasses import replace
from concurrent.futures import Future
from datetime import timedelta
import logging
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Union
import threading

from .identifiers import RunIdentifierScope
from .delivery import (
    FixDeliverySerializationError,
    OutboundFixDeliveryQueue,
)
from .lifecycle import (
    ExchangeLifecycle,
    IngressToken,
    LifecycleIngressRejected,
    LifecycleState,
)
from .models import (
    CancelReject,
    CancelRejectReason,
    CancelOrderCommand,
    ClientOrderKey,
    ReplaceOrderCommand,
    ExecutionReport,
    ExecutionType,
    FixDeliveryDueCommand,
    FixCommandType,
    FixIngressEnvelope,
    FixOrderListIngressEnvelope,
    IngressBarrier,
    NewOrderCommand,
    OrderType,
    OrderStatus,
    ParsedFixCommand,
    ParsedOrderListCommand,
    NewOrderListMember,
    ProcessOrderCommand,
    SessionIdentity,
    Side,
    TimeInForce,
    create_cancel_reject,
)
from .fix_fields import cancel_reject_fields, execution_report_fields
from .metrics import (
    MetricsCollector,
    LatencyTracker,
    SERIALIZATION_DURATION,
)
from .engine import MatchingEngine
from .latency import MonotonicClock, PerfCounterClock, monotonic_duration_ns
from .validation import (
    OrderNormalizer,
    OrderValidationCode,
    OrderValidationError,
    RawOrderInput,
)

logger = logging.getLogger(__name__)

_FIX_APPLICATION_BASE = fix.Application if fix is not None else object


RoutableReport = Union[ExecutionReport, CancelReject]


def configured_fix_session_ids(config_path: str) -> set[str]:
    """Read repeated QuickFIX SESSION blocks into stable SessionID strings."""
    defaults: dict[str, str] = {}
    sessions: list[dict[str, str]] = []
    current: Optional[dict[str, str]] = None
    section: Optional[str] = None
    for line_number, raw_line in enumerate(
        Path(config_path).read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            if section == "SESSION" and current is not None:
                sessions.append(current)
            section = line[1:-1].strip().upper()
            current = {} if section == "SESSION" else None
            continue
        if "=" not in line:
            raise ValueError(
                f"Invalid QuickFIX setting at {config_path}:{line_number}"
            )
        key, value = (part.strip() for part in line.split("=", 1))
        if section == "DEFAULT":
            defaults[key] = value
        elif section == "SESSION" and current is not None:
            current[key] = value
    if section == "SESSION" and current is not None:
        sessions.append(current)
    if not sessions:
        raise ValueError("QuickFIX settings contain no SESSION blocks")

    result: set[str] = set()
    for index, overrides in enumerate(sessions, start=1):
        settings = {**defaults, **overrides}
        missing = [
            name
            for name in ("BeginString", "SenderCompID", "TargetCompID")
            if not settings.get(name)
        ]
        if missing:
            raise ValueError(
                f"QuickFIX SESSION {index} is missing: {', '.join(missing)}"
            )
        session_id = (
            f"{settings['BeginString']}:"
            f"{settings['SenderCompID']}->{settings['TargetCompID']}"
        )
        if session_id in result:
            raise ValueError(f"Duplicate QuickFIX session: {session_id}")
        result.add(session_id)
    return result


def report_destination_id(report: RoutableReport) -> Optional[str]:
    """Return the immutable destination captured on a domain report."""
    return report.session_id


class SessionRegistry:
    """Thread-safe owner of live transport objects at the gateway boundary."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._identities: Dict[str, SessionIdentity] = {}
        self._transports: Dict[str, object] = {}
        self._accepted_cl_ord_ids: Dict[str, set[str]] = {}
        self._accepted_list_ids: Dict[str, set[str]] = {}

    def register(
        self,
        identity: SessionIdentity,
        transport_session: object,
    ) -> None:
        with self._lock:
            replaced = identity.session_id in self._transports
            self._identities[identity.session_id] = identity
            self._transports[identity.session_id] = transport_session
            self._accepted_cl_ord_ids.setdefault(identity.session_id, set())
        logger.info(
            "FIX session registered",
            extra={
                "session_id": identity.session_id,
                "session_alias": identity.alias,
                "session_replaced": replaced,
            },
        )

    def unregister(
        self,
        session_id: str,
        transport_session: Optional[object] = None,
    ) -> bool:
        with self._lock:
            current = self._transports.get(session_id)
            if current is None:
                return False
            if (
                transport_session is not None
                and current is not transport_session
            ):
                logger.warning(
                    "Ignored stale FIX session unregistration",
                    extra={"session_id": session_id},
                )
                return False
            self._transports.pop(session_id, None)
            self._identities.pop(session_id, None)
        logger.info(
            "FIX session unregistered",
            extra={"session_id": session_id},
        )
        return True

    def resolve_transport(self, session_id: str) -> Optional[object]:
        with self._lock:
            return self._transports.get(session_id)

    def resolve_identity(self, session_id: str) -> Optional[SessionIdentity]:
        with self._lock:
            return self._identities.get(session_id)

    def is_connected(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._transports

    def has_cl_ord_id(self, session_id: str, cl_ord_id: str) -> bool:
        with self._lock:
            return cl_ord_id in self._accepted_cl_ord_ids.get(
                session_id,
                set(),
            )

    def remember_cl_ord_id(self, session_id: str, cl_ord_id: str) -> None:
        """Retain accepted IDs for the stable SessionID until process restart."""
        with self._lock:
            self._accepted_cl_ord_ids.setdefault(session_id, set()).add(
                cl_ord_id
            )

    def claim_cl_ord_id(self, session_id: str, cl_ord_id: str) -> bool:
        """Atomically claim one D/F ClOrdID in the process-lifetime namespace."""
        with self._lock:
            accepted = self._accepted_cl_ord_ids.setdefault(session_id, set())
            if cl_ord_id in accepted:
                return False
            accepted.add(cl_ord_id)
            return True

    def claim_list_id(self, session_id: str, list_id: str) -> bool:
        with self._lock:
            accepted = self._accepted_list_ids.setdefault(session_id, set())
            if list_id in accepted:
                return False
            accepted.add(list_id)
            return True

    @property
    def connected_count(self) -> int:
        with self._lock:
            return len(self._transports)


class FIXApplication(_FIX_APPLICATION_BASE):
    """QuickFIX application for handling FIX protocol messages.

    The matching engine and scheduler live on the asyncio event loop
    running in the main thread.  Callback methods invoked by QuickFIX may
    run on background threads, so any interaction with the engine or
    scheduler must be marshalled back to the main thread using
    ``self.loop.call_soon_threadsafe`` where ``self.loop has been set to the
    asyncio event loop running in the main thread.
    """

    def __init__(
        self,
        engine: MatchingEngine,
        metrics: MetricsCollector,
        latency_tracker: LatencyTracker,
        *,
        session_registry: Optional[SessionRegistry] = None,
        transport_sender: Optional[
            Callable[[object, RoutableReport], object]
        ] = None,
        monotonic_clock: Optional[MonotonicClock] = None,
        lifecycle: Optional[ExchangeLifecycle] = None,
        delivery_queue: Optional[OutboundFixDeliveryQueue] = None,
        order_normalizer: Optional[OrderNormalizer] = None,
        session_participants: Optional[Mapping[str, str]] = None,
        delivery_retry_interval_ms: int = 1_000,
        delivery_max_attempts_per_cycle: int = 3,
        delivery_terminal_retention_records: int = 10_000,
        max_orders_per_list: int = 100,
    ):
        super().__init__()
        self.engine = engine
        self.metrics = metrics
        self.latency_tracker = latency_tracker
        self.session_registry = session_registry or SessionRegistry()
        self.transport_sender = transport_sender or self._send_to_quickfix
        self.monotonic_clock = monotonic_clock or PerfCounterClock()
        self.lifecycle = lifecycle
        self.order_normalizer = (
            order_normalizer or OrderNormalizer(engine.instruments)
        )
        self.session_participants = dict(session_participants or {})
        if not isinstance(max_orders_per_list, int) or isinstance(max_orders_per_list, bool) or not 1 <= max_orders_per_list <= 1000:
            raise ValueError("max_orders_per_list must be between 1 and 1000")
        self.max_orders_per_list = max_orders_per_list
        self.delivery_queue = delivery_queue or OutboundFixDeliveryQueue(
            retry_interval_ms=delivery_retry_interval_ms,
            max_attempts_per_cycle=delivery_max_attempts_per_cycle,
            terminal_retention_records=(
                delivery_terminal_retention_records
            ),
        )
        self._draining_sessions: set[str] = set()
        self._draining_sessions_lock = threading.Lock()
        self._metric_suppression = threading.local()
        self.identifier_scope = engine.identifier_scope
        self.loop = None
        self._ingress_lock = threading.RLock()
        self._next_ingress_sequence = 0
        self._ingress_receipts: Dict[
            tuple[int, int], Future[Optional[FixIngressEnvelope]]
        ] = {}
        self._pending_ingress: list[FixIngressEnvelope] = []
        self._pending_lifecycle_actions: set[Future[None]] = set()
        self.on_report_delivered: Optional[Callable[[RoutableReport, object], object]] = None

    def reset_run_state(self, identifiers: RunIdentifierScope) -> None:
        """Reset all run-local ingress state and install the new ID scope."""
        logger.info("Resetting FIX application run state.")
        with self._ingress_lock:
            for future in self._ingress_receipts.values():
                if not future.done():
                    future.set_exception(
                        LifecycleIngressRejected("LIFECYCLE_CHANGED")
                    )
            self._ingress_receipts.clear()
            self._pending_ingress.clear()
            self._next_ingress_sequence = 0
            self.identifier_scope = identifiers
        self.delivery_queue.begin_generation(identifiers.generation)

    def onCreate(self, sessionID):
        """Called when a new session is created."""
        logger.info(f"FIX session created: {sessionID}")

    def onLogon(self, sessionID):
        """Called when a session logs on."""
        identity = self._session_identity(sessionID)
        self.register_session(identity, sessionID)
        logger.info(
            "FIX session logon",
            extra={"session_id": identity.session_id},
        )

    def onLogout(self, sessionID):
        """Called when a session logs out."""
        session_id = sessionID.toString()
        self.unregister_session(session_id, sessionID)
        logger.info(
            "FIX session logout",
            extra={"session_id": session_id},
        )

    def register_session(
        self,
        identity: SessionIdentity,
        transport_session: object,
    ) -> None:
        """Register or atomically replace a connected transport."""
        self.session_registry.register(identity, transport_session)
        self.metrics.set_connected_sessions(
            self.session_registry.connected_count
        )
        loop = self.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(
                self.retry_pending_for_session,
                identity.session_id,
            )
        elif loop is None:
            self.retry_pending_for_session(identity.session_id)

    def unregister_session(
        self,
        session_id: str,
        transport_session: Optional[object] = None,
    ) -> bool:
        """Disconnect one stable session without affecting any peer."""
        removed = self.session_registry.unregister(
            session_id,
            transport_session,
        )
        self.metrics.set_connected_sessions(
            self.session_registry.connected_count
        )
        return removed

    def toAdmin(self, message, sessionID):
        """Called for outgoing admin messages."""
        msg_type = message.getHeader().getField(fix.MsgType())
        logger.debug(f"Admin message to {sessionID}: {msg_type}")

    def fromAdmin(self, message, sessionID):
        """Called for incoming admin messages."""
        msg_type = message.getHeader().getField(fix.MsgType())
        logger.debug(f"Admin message from {sessionID}: {msg_type}")
        if self.lifecycle is None or self.lifecycle.state == LifecycleState.ACTIVE:
            self.metrics.record_fix_message(
                msg_type,
                "received",
                self.engine.scheduler.current_time_ns(),
            )

    def toApp(self, message, sessionID):
        """Called for outgoing application messages."""
        msg_type = message.getHeader().getField(fix.MsgType())
        logger.debug(f"App message to {sessionID}: {msg_type}")
        if (
            not getattr(
                self._metric_suppression,
                "suppress_to_app",
                False,
            )
            and (
                self.lifecycle is None
                or self.lifecycle.state == LifecycleState.ACTIVE
            )
        ):
            self.metrics.record_fix_message(
                msg_type,
                "sent",
                self.engine.scheduler.current_time_ns(),
            )

    def fromApp(self, message, sessionID):
        """Called for incoming application messages."""
        msg_type_field = fix.MsgType()
        message.getHeader().getField(msg_type_field)
        msg_type = msg_type_field.getValue()
        ref_seq_num = self._resolve_ref_seq_num(message, sessionID)

        logger.debug(f"App message from {sessionID}: {msg_type}")
        token = (
            self.lifecycle.token
            if self.lifecycle is not None
            else IngressToken(
                LifecycleState.ACTIVE,
                self.identifier_scope.generation,
            )
        )
        if self.lifecycle is not None:
            rejection = self.lifecycle.ingress_rejection(token)
            if rejection is not None:
                self._send_lifecycle_reject(
                    sessionID,
                    rejection,
                    ref_msg_type=msg_type,
                    ref_seq_num=ref_seq_num,
                )
                return

        try:
            if msg_type == fix.MsgType_NewOrderSingle:
                self.handle_new_order_single(
                    message, sessionID, token, ref_seq_num
                )
            elif msg_type == fix.MsgType_OrderCancelRequest:
                self.handle_order_cancel_request(
                    message, sessionID, token, ref_seq_num
                )
            elif msg_type == "E":
                self.handle_new_order_list(message, sessionID, token, ref_seq_num)
            elif msg_type == "G":
                self.handle_order_cancel_replace_request(message, sessionID, token, ref_seq_num)
            elif msg_type == fix.MsgType_MarketDataRequest:
                self.handle_market_data_request(
                    message, sessionID, token, ref_seq_num
                )
            else:
                logger.warning(f"Unsupported message type: {msg_type}")
                self._schedule_lifecycle_action(
                    sessionID,
                    msg_type,
                    token,
                    lambda: self.send_business_reject(
                        sessionID,
                        f"Unsupported message type: {msg_type}",
                        ref_msg_type=msg_type,
                        ref_seq_num=ref_seq_num,
                        business_reject_reason=3,
                    ),
                    ref_seq_num=ref_seq_num,
                )
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            self.send_business_reject(
                sessionID,
                str(e),
                ref_msg_type=msg_type,
                ref_seq_num=ref_seq_num,
                business_reject_reason=5,
            )

    def _schedule_lifecycle_action(
        self,
        sessionID,
        ref_msg_type: str,
        token: IngressToken,
        action: Callable[[], object],
        *,
        ref_seq_num: Optional[int] = None,
    ) -> None:
        resolved_ref_seq_num = ref_seq_num or self._session_ref_seq_num(
            sessionID
        )
        loop = self.loop
        if loop is None or loop.is_closed():
            self._send_lifecycle_reject(
                sessionID,
                "LIFECYCLE_FAILED",
                ref_msg_type=ref_msg_type,
                ref_seq_num=resolved_ref_seq_num,
            )
            return

        completion: Future[None] = Future()
        with self._ingress_lock:
            self._pending_lifecycle_actions.add(completion)

        def _record_and_run():
            self.metrics.record_fix_message(
                ref_msg_type,
                "received",
                self.engine.scheduler.current_time_ns(),
            )
            return action()

        if self.lifecycle is None:
            def _execute_without_lifecycle() -> None:
                try:
                    _record_and_run()
                except BaseException as exc:
                    completion.set_exception(exc)
                    raise
                else:
                    completion.set_result(None)

            loop.call_soon_threadsafe(_execute_without_lifecycle)
            return

        async def _execute() -> None:
            try:
                await self.lifecycle.execute_ingress(token, _record_and_run)
            except LifecycleIngressRejected as exc:
                try:
                    self._send_lifecycle_reject(
                        sessionID,
                        exc.code,
                        ref_msg_type=ref_msg_type,
                        ref_seq_num=resolved_ref_seq_num,
                    )
                except BaseException as send_exc:
                    completion.set_exception(send_exc)
                    raise
                else:
                    completion.set_result(None)
            except BaseException as exc:
                completion.set_exception(exc)
                raise
            else:
                completion.set_result(None)

        loop.call_soon_threadsafe(lambda: loop.create_task(_execute()))

    async def _await_lifecycle_actions(self) -> None:
        """Wait for validation/lifecycle rejects scheduled by FIX callbacks."""
        with self._ingress_lock:
            pending = tuple(self._pending_lifecycle_actions)
        if pending:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in pending)
            )
        with self._ingress_lock:
            self._pending_lifecycle_actions.difference_update(pending)

    @staticmethod
    def _command_message_type(command_type: FixCommandType) -> str:
        return {FixCommandType.NEW_ORDER: "D", FixCommandType.CANCEL_ORDER: "F",
                FixCommandType.REPLACE_ORDER: "G", FixCommandType.NEW_ORDER_LIST: "E"}[command_type]

    @staticmethod
    def _receive_event_type(command_type: FixCommandType) -> str:
        return {FixCommandType.NEW_ORDER: "new_order_received", FixCommandType.CANCEL_ORDER: "cancel_received",
                FixCommandType.REPLACE_ORDER: "replace_received", FixCommandType.NEW_ORDER_LIST: "order_list_received"}[command_type]

    @staticmethod
    def _list_member_envelope(parent: FixOrderListIngressEnvelope, member: NewOrderListMember) -> FixIngressEnvelope:
        command = member.command
        return FixIngressEnvelope(
            command.command_type, parent.session, parent.msg_seq_num, command.cl_ord_id,
            None, command.symbol, command.side, command.order_type, command.order_qty_lots,
            command.price_ticks, command.tif, parent.ingress_sequence,
            parent.simulated_receive_timestamp, parent.run_generation,
            list_id=parent.list_id, rejection_code=command.rejection_code,
            rejection_text=command.rejection_text, list_seq_no=member.list_seq_no,
        )

    def _submit_ingress(
        self,
        parsed: ParsedFixCommand | ParsedOrderListCommand,
        token: IngressToken,
        transport_session: object,
    ) -> int:
        """Reserve a run-local sequence and marshal immutable data to the loop."""
        loop = self.loop
        if loop is None or loop.is_closed():
            self._send_lifecycle_reject(
                transport_session,
                "LIFECYCLE_FAILED",
                ref_msg_type=self._command_message_type(parsed.command_type),
                ref_seq_num=parsed.msg_seq_num,
            )
            raise LifecycleIngressRejected("LIFECYCLE_FAILED")

        with self._ingress_lock:
            sequence = self._next_ingress_sequence
            self._next_ingress_sequence += 1
            receipt: Future[Optional[FixIngressEnvelope]] = Future()
            self._ingress_receipts[(token.generation, sequence)] = receipt

        async def _enqueue() -> None:
            def _commit() -> FixIngressEnvelope:
                if isinstance(parsed, ParsedOrderListCommand):
                    envelope = FixOrderListIngressEnvelope(
                        parsed.session, parsed.msg_seq_num, parsed.list_id, parsed.members,
                        sequence, self.engine.scheduler.current_time, token.generation,
                    )
                else:
                    envelope = FixIngressEnvelope(
                    command_type=parsed.command_type,
                    session=parsed.session,
                    msg_seq_num=parsed.msg_seq_num,
                    cl_ord_id=parsed.cl_ord_id,
                    orig_cl_ord_id=parsed.orig_cl_ord_id,
                    symbol=parsed.symbol,
                    side=parsed.side,
                    order_type=parsed.order_type,
                    order_qty_lots=parsed.order_qty_lots,
                    price_ticks=parsed.price_ticks,
                    tif=parsed.tif,
                    ingress_sequence=sequence,
                    simulated_receive_timestamp=(
                        self.engine.scheduler.current_time
                    ),
                    run_generation=token.generation,
                    list_id=parsed.list_id,
                    exchange_order_id=parsed.exchange_order_id,
                    rejection_code=parsed.rejection_code,
                    rejection_text=parsed.rejection_text,
                )
                with self._ingress_lock:
                    self._pending_ingress.append(envelope)
                    if not receipt.done():
                        receipt.set_result(envelope)
                return envelope

            try:
                if self.lifecycle is None:
                    _commit()
                else:
                    await self.lifecycle.execute_ingress(token, _commit)
            except LifecycleIngressRejected as exc:
                try:
                    self._send_lifecycle_reject(
                        transport_session,
                        exc.code,
                        ref_msg_type=self._command_message_type(parsed.command_type),
                        ref_seq_num=parsed.msg_seq_num,
                    )
                except Exception as rejection_error:
                    with self._ingress_lock:
                        if not receipt.done():
                            receipt.set_exception(rejection_error)
                    logger.exception("FIX lifecycle rejection could not be sent")
                else:
                    with self._ingress_lock:
                        # A delivered lifecycle rejection settles this submission.
                        # Existing barrier waiters must also be able to proceed
                        # so completed reports can drain after run failure.
                        if not receipt.done():
                            receipt.set_result(None)
                        self._ingress_receipts.pop((token.generation, sequence), None)
            except Exception as exc:
                with self._ingress_lock:
                    if not receipt.done():
                        receipt.set_exception(exc)
                logger.exception(
                    "FIX ingress enqueue failed",
                    extra={
                        "run_generation": token.generation,
                        "ingress_sequence": sequence,
                    },
                )

        loop.call_soon_threadsafe(lambda: loop.create_task(_enqueue()))
        return sequence

    def ingress_high_water_mark(
        self,
        generation: Optional[int] = None,
    ) -> IngressBarrier:
        """Snapshot the submitted sequence without waiting or scheduling."""
        resolved_generation = (
            self.identifier_scope.generation
            if generation is None
            else generation
        )
        with self._ingress_lock:
            through = self._next_ingress_sequence - 1
        return IngressBarrier(resolved_generation, through)

    async def await_ingress_enqueued(
        self,
        through_sequence: Optional[int] = None,
        *,
        generation: Optional[int] = None,
    ) -> IngressBarrier:
        """Wait until captured submissions are enqueued or lifecycle-rejected."""
        barrier = self.ingress_high_water_mark(generation)
        target = (
            barrier.through_sequence
            if through_sequence is None
            else through_sequence
        )
        if target < -1:
            raise ValueError("Ingress sequence cannot be less than -1")
        if target > barrier.through_sequence:
            raise ValueError(
                "Ingress sequence exceeds the submitted high-water mark"
            )
        with self._ingress_lock:
            receipts = [
                future
                for (receipt_generation, sequence), future
                in self._ingress_receipts.items()
                if receipt_generation == barrier.run_generation
                and sequence <= target
            ]
        if receipts:
            await asyncio.gather(
                *(asyncio.wrap_future(receipt) for receipt in receipts)
            )
        return IngressBarrier(barrier.run_generation, target)

    def drain_ingress_locked(
        self,
        generation: int,
        through_sequence: int,
    ) -> IngressBarrier:
        """Convert acknowledged envelopes to sorted scheduler receive events.

        The caller must hold the lifecycle lock.  Ordering is explicitly
        ``(receive timestamp, stable SessionID, MsgSeqNum, ingress sequence)``.
        """
        with self._ingress_lock:
            ready = sorted(
                (
                    envelope
                    for envelope in self._pending_ingress
                    if envelope.run_generation == generation
                    and envelope.ingress_sequence <= through_sequence
                ),
                key=lambda envelope: envelope.ordering_key,
            )
            ready_ids = {
                (envelope.run_generation, envelope.ingress_sequence)
                for envelope in ready
            }
            self._pending_ingress = [
                envelope
                for envelope in self._pending_ingress
                if (
                    envelope.run_generation,
                    envelope.ingress_sequence,
                )
                not in ready_ids
            ]

        for index, envelope in enumerate(ready):
            if self.lifecycle is not None and self.lifecycle.check_persistence_failure():
                self._reject_failed_ingress_batch(ready[index:])
                break
            msg_type = self._command_message_type(envelope.command_type)
            self.metrics.record_fix_message(
                msg_type,
                "received",
                self.engine.scheduler.current_time_ns(),
            )
            if self.lifecycle is not None and self.lifecycle.check_persistence_failure():
                self._reject_failed_ingress_batch(ready[index:])
                break
            if isinstance(envelope, FixOrderListIngressEnvelope):
                if not self.session_registry.claim_list_id(envelope.session.session_id, envelope.list_id):
                    transport = self.session_registry.resolve_transport(envelope.session.session_id)
                    if transport is None:
                        transport = fix.SessionID("FIX.4.4", envelope.session.sender_comp_id, envelope.session.target_comp_id)
                    self.send_business_reject(transport, "Duplicate ListID in session", ref_msg_type="E",
                                              ref_seq_num=envelope.msg_seq_num, business_reject_reason=5,
                                              business_reject_ref_id=envelope.list_id)
                else:
                    for member in envelope.members:
                        self.engine.scheduler.schedule_event("new_order_received", self._list_member_envelope(envelope, member), 0)
            else:
                self.engine.scheduler.schedule_event(self._receive_event_type(envelope.command_type), envelope, 0)
            with self._ingress_lock:
                self._ingress_receipts.pop(
                    (envelope.run_generation, envelope.ingress_sequence),
                    None,
                )
        return IngressBarrier(generation, through_sequence)

    def _reject_failed_ingress_batch(
        self, envelopes: list[FixIngressEnvelope]
    ) -> None:
        """Settle ingress detached by the current drain before the run failed."""
        for envelope in envelopes:
            self.reject_failed_command(
                self._receive_event_type(envelope.command_type),
                envelope,
                "METRICS_INCOMPLETE: Metrics were lost; this simulation must be rerun.",
            )
            self._settle_ingress_receipt(envelope)

    def _settle_ingress_receipt(self, envelope: FixIngressEnvelope) -> None:
        with self._ingress_lock:
            receipt = self._ingress_receipts.pop(
                (envelope.run_generation, envelope.ingress_sequence), None
            )
            if receipt is not None and not receipt.done():
                receipt.set_result(envelope)

    def fail_pending_commands(self, reason: str) -> None:
        """Reject all queued trading work, retaining delivery and feed events."""
        events = self.engine.scheduler.extract_events({
            "new_order_received", "cancel_received", "order_arrived",
            "order_process", "cancel_arrived_at_exchange", "cancel_engine_process",
            "replace_received", "replace_arrived_at_exchange", "replace_engine_process",
        })
        for event in events:
            self.reject_failed_command(event.event_type, event.data, reason)
        with self._ingress_lock:
            pending = sorted(self._pending_ingress, key=lambda item: item.ordering_key)
            self._pending_ingress.clear()
        for envelope in pending:
            self.reject_failed_command(
                self._receive_event_type(envelope.command_type),
                envelope,
                reason,
            )
            self._settle_ingress_receipt(envelope)

    def reject_failed_command(
        self, event_type: str, data: object, reason: str
    ) -> None:
        """Settle a removed or already-popped logical command exactly once."""
        if event_type == "order_process":
            if not isinstance(data, ProcessOrderCommand):
                raise TypeError("order_process requires ProcessOrderCommand")
            self.engine.reject_pending_order(data, reason)
        elif event_type == "cancel_engine_process":
            if not isinstance(data, CancelOrderCommand):
                raise TypeError("cancel_engine_process requires CancelOrderCommand")
            self._reject_failed_cancel(data, reason)
        elif event_type == "replace_engine_process":
            if not isinstance(data, ReplaceOrderCommand):
                raise TypeError("replace_engine_process requires ReplaceOrderCommand")
            self._reject_failed_replace(data, reason)
        elif event_type == "order_list_received":
            if not isinstance(data, FixOrderListIngressEnvelope):
                raise TypeError("list receive requires FixOrderListIngressEnvelope")
            self.session_registry.claim_list_id(data.session.session_id, data.list_id)
            for member in data.members:
                self.reject_failed_command("new_order_received", self._list_member_envelope(data, member), reason)
        elif event_type in {"new_order_received", "cancel_received", "replace_received"}:
            if not isinstance(data, FixIngressEnvelope):
                raise TypeError("receive event requires FixIngressEnvelope")
            if data.run_generation != self.identifier_scope.generation:
                return
            self.session_registry.claim_cl_ord_id(data.session.session_id, data.cl_ord_id)
            if data.command_type == FixCommandType.NEW_ORDER:
                self._send_input_rejection(
                    cl_ord_id=data.cl_ord_id, symbol=data.symbol, side=data.side,
                    order_type=data.order_type or OrderType.LIMIT,
                    tif=data.tif or TimeInForce.GTC,
                    order_qty_lots=data.order_qty_lots, price_ticks=data.price_ticks,
                    session_id=data.session.session_id, session_alias=data.session.alias,
                    participant_id=data.session.participant_id,
                    code="METRICS_INCOMPLETE",
                    text=reason.removeprefix("METRICS_INCOMPLETE: "),
                    list_id=data.list_id,
                )
            elif data.command_type == FixCommandType.REPLACE_ORDER:
                command = self._replace_command(data, duplicate=False)
                self._reject_failed_replace(command, reason)
            else:
                command = self._cancel_command(data, duplicate=False)
                self.latency_tracker.cancel_received(command)
                self._reject_failed_cancel(command, reason)
        # Arrival events have a paired processing event; rejecting both would
        # emit duplicate terminal reports.

    def _reject_failed_cancel(self, command: CancelOrderCommand, reason: str) -> None:
        report = self.engine.reject_cancel_unavailable(command, reason)
        self.enqueue_report(report)
        self.latency_tracker.cancel_report_created(command, report)

    def _reject_failed_replace(self, command: ReplaceOrderCommand, reason: str) -> None:
        report = self.engine.reject_replace_unavailable(command, reason)
        self.enqueue_report(report)

    async def wait_for_ingress(
        self,
        through_sequence: Optional[int] = None,
    ) -> IngressBarrier:
        """Acknowledge and schedule the captured FIX ingress high-water mark."""
        await self._await_lifecycle_actions()
        observed = (
            self.lifecycle.token
            if self.lifecycle is not None
            else IngressToken(
                LifecycleState.ACTIVE,
                self.identifier_scope.generation,
            )
        )
        rejection = (
            self.lifecycle.ingress_rejection(observed)
            if self.lifecycle is not None
            else None
        )
        if rejection is not None:
            raise LifecycleIngressRejected(rejection)
        barrier = await self.await_ingress_enqueued(
            through_sequence,
            generation=observed.generation,
        )
        if self.lifecycle is None:
            return self.drain_ingress_locked(
                barrier.run_generation,
                barrier.through_sequence,
            )
        async with self.lifecycle.lock:
            if observed != self.lifecycle.token:
                raise LifecycleIngressRejected("LIFECYCLE_CHANGED")
            rejection = self.lifecycle.ingress_rejection(observed)
            if rejection is not None:
                raise LifecycleIngressRejected(rejection)
            return self.drain_ingress_locked(
                barrier.run_generation,
                barrier.through_sequence,
            )

    def _send_lifecycle_reject(
        self,
        sessionID,
        text: str,
        *,
        ref_msg_type: str,
        ref_seq_num: int,
    ) -> None:
        previous = getattr(
            self._metric_suppression,
            "suppress_to_app",
            False,
        )
        self._metric_suppression.suppress_to_app = True
        try:
            self.send_business_reject(
                sessionID,
                text,
                ref_msg_type=ref_msg_type,
                ref_seq_num=ref_seq_num,
                business_reject_reason=4,
            )
        finally:
            self._metric_suppression.suppress_to_app = previous

    def handle_new_order_single(
        self,
        message,
        sessionID,
        token: Optional[IngressToken] = None,
        ref_seq_num: Optional[int] = None,
    ):
        """Parse NewOrderSingle into immutable data and enqueue it centrally."""
        resolved_ref_seq_num = ref_seq_num or self._resolve_ref_seq_num(
            message, sessionID
        )
        try:
            msg_seq_num = self._get_msg_seq_num(message)
        except Exception as exc:
            error_text = str(exc)
            self._schedule_lifecycle_action(
                sessionID,
                fix.MsgType_NewOrderSingle,
                token
                or IngressToken(
                    LifecycleState.ACTIVE,
                    self.identifier_scope.generation,
                ),
                lambda: self.send_reject(
                    sessionID,
                    error_text,
                    ref_msg_type=fix.MsgType_NewOrderSingle,
                    ref_seq_num=resolved_ref_seq_num,
                    ref_tag_id=34,
                    session_reject_reason=1,
                ),
                ref_seq_num=resolved_ref_seq_num,
            )
            return
        try:
            identity = self._session_identity(sessionID)
            cl_ord_id = self._get_field(message, fix.ClOrdID())
            symbol = self._get_field(message, fix.Symbol())
            side_value = self._get_field(message, fix.Side())
            order_qty = self._get_field(message, fix.StringField(38))
            ord_type = self._get_field(message, fix.OrdType())
            if side_value == fix.Side_BUY:
                raw_side = Side.BUY
            elif side_value == fix.Side_SELL:
                raw_side = Side.SELL
            else:
                raw_side = side_value

            raw_price = None
            if ord_type == fix.OrdType_LIMIT:
                raw_order_type = OrderType.LIMIT
                raw_price = self._get_field(message, fix.StringField(44))
            elif ord_type == fix.OrdType_MARKET:
                raw_order_type = OrderType.MARKET
                if message.isSetField(fix.Price()):
                    raw_price = self._get_field(message, fix.StringField(44))
            else:
                raw_order_type = ord_type

            raw_tif = None
            if message.isSetField(fix.TimeInForce()):
                tif_value = self._get_field(message, fix.TimeInForce())
                if tif_value == fix.TimeInForce_IMMEDIATE_OR_CANCEL:
                    raw_tif = TimeInForce.IOC
                elif tif_value == fix.TimeInForce_GOOD_TILL_CANCEL:
                    raw_tif = TimeInForce.GTC
                else:
                    raw_tif = tif_value

            command = self.order_normalizer.normalize(
                RawOrderInput(
                    session_id=identity.session_id,
                    participant_id=identity.participant_id,
                    cl_ord_id=cl_ord_id,
                    symbol=symbol,
                    side=raw_side,
                    order_type=raw_order_type,
                    quantity=order_qty,
                    price=raw_price,
                    time_in_force=raw_tif,
                    session_alias=identity.alias,
                ),
                submitted_at=self.engine.scheduler.current_time,
            )

            parsed = ParsedFixCommand(
                command_type=FixCommandType.NEW_ORDER,
                session=identity,
                msg_seq_num=msg_seq_num,
                cl_ord_id=command.cl_ord_id,
                symbol=command.symbol,
                side=command.side,
                order_type=command.order_type,
                order_qty_lots=command.order_qty_lots,
                price_ticks=command.price_ticks,
                tif=command.tif,
            )
            self._submit_ingress(
                parsed,
                token
                or IngressToken(
                    LifecycleState.ACTIVE,
                    self.identifier_scope.generation,
                ),
                sessionID,
            )
        except OrderValidationError as exc:
            logger.info("FIX order validation failed: %s", exc)
            validation_code = exc.code
            validation_message = exc.message
            _identity = (
                identity
                if "identity" in locals()
                else self._session_identity(sessionID)
            )
            if exc.code == OrderValidationCode.INVALID_CL_ORD_ID:
                self._schedule_lifecycle_action(
                    sessionID,
                    fix.MsgType_NewOrderSingle,
                    token
                    or IngressToken(
                        LifecycleState.ACTIVE,
                        self.identifier_scope.generation,
                    ),
                    lambda code=validation_code, message=validation_message: (
                        self.send_business_reject(
                            sessionID,
                            f"{code.value}: {message}",
                            ref_msg_type=fix.MsgType_NewOrderSingle,
                            ref_seq_num=msg_seq_num,
                            business_reject_reason=5,
                        )
                    ),
                    ref_seq_num=msg_seq_num,
                )
                return

            def _reject(
                code=validation_code,
                message=validation_message,
                rejected_identity=_identity,
                rejected_cl_ord_id=(
                    cl_ord_id if "cl_ord_id" in locals() else "UNAVAILABLE"
                ),
                rejected_symbol=(
                    str(symbol).strip().upper()
                    if "symbol" in locals()
                    else "UNKNOWN"
                ),
                rejected_side=(
                    raw_side
                    if "raw_side" in locals()
                    and isinstance(raw_side, Side)
                    else Side.BUY
                ),
                rejected_order_type=(
                    raw_order_type
                    if "raw_order_type" in locals()
                    and isinstance(raw_order_type, OrderType)
                    else OrderType.LIMIT
                ),
                rejected_tif=(
                    raw_tif
                    if "raw_tif" in locals()
                    and isinstance(raw_tif, TimeInForce)
                    else TimeInForce.GTC
                ),
            ):
                self._send_input_rejection(
                    cl_ord_id=rejected_cl_ord_id,
                    symbol=rejected_symbol,
                    side=rejected_side,
                    order_type=rejected_order_type,
                    tif=rejected_tif,
                    session_id=rejected_identity.session_id,
                    session_alias=rejected_identity.alias,
                    participant_id=rejected_identity.participant_id,
                    code=code.value,
                    text=message,
                )

            self._schedule_lifecycle_action(
                sessionID,
                fix.MsgType_NewOrderSingle,
                token
                or IngressToken(
                    LifecycleState.ACTIVE,
                    self.identifier_scope.generation,
                ),
                _reject,
                ref_seq_num=msg_seq_num,
            )
        except Exception as exc:
            logger.info("Malformed FIX NewOrderSingle: %s", exc)
            malformed_text = (
                "INVALID_CL_ORD_ID: missing or unusable ClOrdID"
                if "cl_ord_id" not in locals()
                else f"INVALID_FIX_MESSAGE: {exc}"
            )
            self._schedule_lifecycle_action(
                sessionID,
                fix.MsgType_NewOrderSingle,
                token
                or IngressToken(
                    LifecycleState.ACTIVE,
                    self.identifier_scope.generation,
                ),
                lambda text=malformed_text: self.send_business_reject(
                    sessionID,
                    text,
                    ref_msg_type=fix.MsgType_NewOrderSingle,
                    ref_seq_num=msg_seq_num,
                    business_reject_reason=5,
                ),
                ref_seq_num=msg_seq_num,
            )

    def _parse_new_order_fields(self, message, identity: SessionIdentity, msg_seq_num: int, *, list_id: Optional[str] = None) -> ParsedFixCommand:
        side_value = self._get_field(message, fix.Side())
        side = {fix.Side_BUY: Side.BUY, fix.Side_SELL: Side.SELL}.get(side_value, side_value)
        type_value = self._get_field(message, fix.OrdType())
        order_type = {fix.OrdType_LIMIT: OrderType.LIMIT, fix.OrdType_MARKET: OrderType.MARKET}.get(type_value, type_value)
        price = self._get_field(message, fix.StringField(44)) if message.isSetField(44) else None
        tif_value = self._get_field(message, fix.TimeInForce()) if message.isSetField(59) else None
        tif = {fix.TimeInForce_GOOD_TILL_CANCEL: TimeInForce.GTC,
               fix.TimeInForce_IMMEDIATE_OR_CANCEL: TimeInForce.IOC}.get(tif_value, tif_value)
        command = self.order_normalizer.normalize(RawOrderInput(
            identity.session_id, identity.participant_id, self._get_field(message, fix.ClOrdID()),
            self._get_field(message, fix.Symbol()), side, order_type,
            self._get_field(message, fix.StringField(38)), price, tif, identity.alias,
        ), submitted_at=self.engine.scheduler.current_time)
        return ParsedFixCommand(
            FixCommandType.NEW_ORDER, identity, msg_seq_num, command.cl_ord_id,
            command.symbol, command.side, command.order_type, command.order_qty_lots,
            command.price_ticks, command.tif, list_id=list_id,
        )

    def handle_new_order_list(self, message, sessionID, token: Optional[IngressToken] = None, ref_seq_num: Optional[int] = None) -> None:
        """Validate the physical list before reserving any member identifiers."""
        observed = token or IngressToken(LifecycleState.ACTIVE, self.identifier_scope.generation)
        list_id: Optional[str] = None
        try:
            msg_seq_num = self._get_msg_seq_num(message)
            identity = self._session_identity(sessionID)
            list_id = self.order_normalizer._cl_ord_id(self._get_field(message, fix.StringField(66)))
            if self._get_field(message, fix.StringField(394)) != "3":
                raise ValueError("Only non-bidding lists (BidType=3) are supported")
            if message.isSetField(433) and self._get_field(message, fix.StringField(433)) != "1":
                raise ValueError("Only immediate ListExecInstType=1 is supported")
            if not message.isSetField(414) or self._get_field(message, fix.StringField(414)) != "3":
                raise ValueError("ProgRptReqs=3 is required; list status workflows are unsupported")
            if message.isSetField(893) and self._get_field(message, fix.StringField(893)) != "Y":
                raise ValueError("Fragmented lists are unsupported")
            total = int(self._get_field(message, fix.StringField(68)))
            count = int(self._get_field(message, fix.StringField(73)))
            if not 1 <= count <= self.max_orders_per_list or count != total:
                raise ValueError("TotNoOrders and NoOrders must agree within the list limit")
            members = []
            sequence_numbers: set[int] = set()
            for index in range(1, count + 1):
                group = fix.Group(73, 11)
                message.getGroup(index, group)
                list_seq_no = int(self._get_field(group, fix.StringField(67)))
                if list_seq_no in sequence_numbers or not 1 <= list_seq_no <= count:
                    raise ValueError("ListSeqNo values must be unique and contiguous")
                sequence_numbers.add(list_seq_no)
                cl_ord_id = self.order_normalizer._cl_ord_id(self._get_field(group, fix.ClOrdID()))
                try:
                    parsed = self._parse_new_order_fields(group, identity, msg_seq_num, list_id=list_id)
                except Exception as exc:
                    def optional(tag: int, default: str) -> str:
                        return self._get_field(group, fix.StringField(tag)) if group.isSetField(tag) else default
                    side = Side.SELL if optional(54, "1") == "2" else Side.BUY
                    parsed = ParsedFixCommand(
                        FixCommandType.NEW_ORDER, identity, msg_seq_num, cl_ord_id,
                        optional(55, "UNKNOWN").strip().upper() or "UNKNOWN", side,
                        OrderType.MARKET if optional(40, "2") == "1" else OrderType.LIMIT,
                        order_qty_lots=0, tif=TimeInForce.GTC, list_id=list_id,
                        rejection_code=exc.code.value if isinstance(exc, OrderValidationError) else "INVALID_REQUEST",
                        rejection_text=exc.message if isinstance(exc, OrderValidationError) else str(exc),
                    )
                members.append(NewOrderListMember(list_seq_no, parsed))
            parsed_list = ParsedOrderListCommand(identity, msg_seq_num, list_id,
                                                tuple(sorted(members, key=lambda member: member.list_seq_no)))
            self._submit_ingress(parsed_list, observed, sessionID)
        except Exception as exc:
            text = str(exc)
            self._schedule_lifecycle_action(
                sessionID, "E", observed,
                lambda: self.send_business_reject(sessionID, text, ref_msg_type="E",
                    ref_seq_num=ref_seq_num or self._resolve_ref_seq_num(message, sessionID),
                    business_reject_reason=5, business_reject_ref_id=list_id),
                ref_seq_num=ref_seq_num,
            )

    def handle_order_cancel_replace_request(self, message, sessionID, token: Optional[IngressToken] = None, ref_seq_num: Optional[int] = None) -> None:
        observed = token or IngressToken(LifecycleState.ACTIVE, self.identifier_scope.generation)
        try:
            msg_seq_num = self._get_msg_seq_num(message)
            identity = self._session_identity(sessionID)
            cl_ord_id = self.order_normalizer._cl_ord_id(self._get_field(message, fix.ClOrdID()))
            orig_cl_ord_id = self.order_normalizer._cl_ord_id(self._get_field(message, fix.OrigClOrdID()))
            if not message.isSetField(59) or self._get_field(message, fix.TimeInForce()) != fix.TimeInForce_GOOD_TILL_CANCEL:
                raise ValueError("Replacement requires explicit TimeInForce=1")
            parsed = self._parse_new_order_fields(message, identity, msg_seq_num)
            if parsed.order_type != OrderType.LIMIT:
                raise ValueError("Replacement supports limit orders only")
            self._submit_ingress(ParsedFixCommand(
                FixCommandType.REPLACE_ORDER, identity, msg_seq_num, cl_ord_id,
                parsed.symbol, parsed.side, parsed.order_type, parsed.order_qty_lots,
                parsed.price_ticks, parsed.tif, orig_cl_ord_id,
                list_id=self._get_field(message, fix.StringField(66)) if message.isSetField(66) else None,
                exchange_order_id=self._get_field(message, fix.StringField(37)) if message.isSetField(37) else None,
            ), observed, sessionID)
        except Exception as exc:
            text = str(exc)
            if "cl_ord_id" in locals() and "orig_cl_ord_id" in locals():
                def reject_invalid() -> None:
                    self.session_registry.claim_cl_ord_id(identity.session_id, cl_ord_id)
                    order_id = self.engine.client_order_index.get(ClientOrderKey(identity.session_id, orig_cl_ord_id))
                    order = self.engine.orders.get(order_id) if order_id is not None else None
                    reject = CancelReject(
                        self.identifier_scope.next_cancel_reject_id(), cl_ord_id, orig_cl_ord_id,
                        order.id if order else None, order.status if order else None,
                        CancelRejectReason.INVALID_REQUEST, text, identity.session_id, identity.alias,
                        self.engine.scheduler.current_time, exchange_order_id=order.exchange_order_id if order else None,
                        run_generation=self.identifier_scope.generation, response_to="2",
                        list_id=order.list_id if order else None, run_id=order.run_id if order else self.engine.run_id,
                    )
                    self.enqueue_report(reject)
                    if self.engine.on_replace_reject is not None:
                        self.engine.on_replace_reject(reject)
                self._schedule_lifecycle_action(sessionID, "G", observed, reject_invalid, ref_seq_num=ref_seq_num)
            else:
                self._schedule_lifecycle_action(sessionID, "G", observed,
                    lambda: self.send_business_reject(sessionID, text, ref_msg_type="G",
                        ref_seq_num=ref_seq_num, business_reject_reason=5), ref_seq_num=ref_seq_num)

    def process_new_order_envelope(
        self,
        envelope: FixIngressEnvelope,
    ) -> Optional[int]:
        """Handle a deterministically ordered new-order receive event."""
        if envelope.command_type != FixCommandType.NEW_ORDER:
            raise TypeError("Expected a NEW_ORDER ingress envelope")
        if envelope.run_generation != self.identifier_scope.generation:
            return None
        if envelope.rejection_code is not None:
            self.session_registry.claim_cl_ord_id(envelope.session.session_id, envelope.cl_ord_id)
            self._send_input_rejection(
                cl_ord_id=envelope.cl_ord_id, symbol=envelope.symbol, side=envelope.side,
                order_type=envelope.order_type or OrderType.LIMIT, tif=envelope.tif or TimeInForce.GTC,
                session_id=envelope.session.session_id, session_alias=envelope.session.alias,
                participant_id=envelope.session.participant_id, code=envelope.rejection_code,
                text=envelope.rejection_text or "Invalid list member", list_id=envelope.list_id,
            )
            return None
        command = NewOrderCommand(
            cl_ord_id=envelope.cl_ord_id,
            symbol=envelope.symbol,
            side=envelope.side,
            order_type=envelope.order_type or OrderType.LIMIT,
            order_qty_lots=envelope.order_qty_lots,
            price_ticks=envelope.price_ticks,
            tif=envelope.tif or TimeInForce.GTC,
            submitted_at=envelope.simulated_receive_timestamp,
            session_id=envelope.session.session_id,
            session_alias=envelope.session.alias,
            participant_id=envelope.session.participant_id,
            list_id=envelope.list_id,
        )
        if not self.session_registry.claim_cl_ord_id(
            command.session_id,
            command.cl_ord_id,
        ):
            self._send_input_rejection(
                cl_ord_id=command.cl_ord_id,
                symbol=command.symbol,
                side=command.side,
                order_type=command.order_type,
                order_qty_lots=command.order_qty_lots,
                price_ticks=command.price_ticks,
                tif=command.tif,
                session_id=command.session_id,
                session_alias=command.session_alias,
                participant_id=command.participant_id,
                code=OrderValidationCode.DUPLICATE_CL_ORD_ID.value,
                text=f"Duplicate ClOrdID in session: {command.cl_ord_id}",
                list_id=command.list_id,
            )
            return None
        try:
            order_id = self.engine.submit_order(command)
        except Exception:
            logger.exception(
                "Validated FIX command failed during engine submission"
            )
            raise
        logger.debug(
            "Deterministic FIX order submitted: %s -> %s",
            command.cl_ord_id,
            order_id,
        )
        return order_id

    def submit_new_order_command(self, command: NewOrderCommand) -> int:
        """Commit one parsed FIX order under the lifecycle serialization lock."""
        session_id = command.session_id
        if session_id is None:
            raise ValueError("FIX order requires a stable session ID")
        if not self.session_registry.claim_cl_ord_id(
            session_id,
            command.cl_ord_id,
        ):
            raise ValueError(
                "Duplicate ClOrdID in FIX session: "
                f"{command.cl_ord_id}"
            )
        return self.engine.submit_order(command)

    def _send_input_rejection(
        self,
        *,
        cl_ord_id: str,
        symbol: str,
        side: Side,
        order_type: OrderType,
        tif: TimeInForce,
        session_id: Optional[str],
        session_alias: Optional[str],
        participant_id: Optional[str],
        code: str,
        text: str,
        order_qty_lots: int = 0,
        price_ticks: Optional[int] = None,
        list_id: Optional[str] = None,
    ) -> None:
        """Build a terminal immutable snapshot for a malformed new order."""
        simulated_time = self.engine.scheduler.current_time
        instrument = self.engine.instruments.get(symbol)
        if instrument is None:
            instrument = self.engine.instruments.require(
                self.engine.instruments.symbols[0]
            )
        safe_qty_lots = (
            order_qty_lots
            if isinstance(order_qty_lots, int)
            and not isinstance(order_qty_lots, bool)
            and order_qty_lots >= 0
            else 0
        )
        safe_price_ticks = (
            price_ticks
            if isinstance(price_ticks, int)
            and not isinstance(price_ticks, bool)
            and price_ticks > 0
            else None
        )
        rejection_text = f"{code}: {text}"
        exec_id = self.identifier_scope.next_exec_id()
        exchange_order_id = self.identifier_scope.next_order_id()
        report = ExecutionReport(
            exec_id=exec_id,
            order_id=0,
            exchange_order_id=exchange_order_id,
            run_generation=self.identifier_scope.generation,
            cl_ord_id=cl_ord_id,
            symbol=symbol,
            instrument=instrument,
            side=side,
            order_type=order_type,
            order_qty_lots=safe_qty_lots,
            order_price_ticks=safe_price_ticks,
            tif=tif,
            ord_status=OrderStatus.REJECTED,
            exec_type=ExecutionType.REJECTED,
            leaves_qty_lots=0,
            cum_qty_lots=0,
            canceled_qty_lots=safe_qty_lots,
            filled_notional_tick_lots=0,
            reject_reason=rejection_text,
            text=rejection_text,
            session_id=session_id,
            session_alias=session_alias,
            participant_id=participant_id,
            submitted_at=simulated_time,
            timestamp=simulated_time,
            order_version=0,
            list_id=list_id,
            run_id=self.engine.run_id,
        )
        self.enqueue_report(report)
        self.metrics.record_order(report)

    def handle_order_cancel_request(
        self,
        message,
        sessionID,
        token: Optional[IngressToken] = None,
        ref_seq_num: Optional[int] = None,
    ):
        """Parse OrderCancelRequest into immutable data and enqueue it."""
        resolved_ref_seq_num = ref_seq_num or self._resolve_ref_seq_num(
            message, sessionID
        )
        try:
            msg_seq_num = self._get_msg_seq_num(message)
        except Exception as exc:
            error_text = str(exc)
            self._schedule_lifecycle_action(
                sessionID,
                fix.MsgType_OrderCancelRequest,
                token
                or IngressToken(
                    LifecycleState.ACTIVE,
                    self.identifier_scope.generation,
                ),
                lambda: self.send_reject(
                    sessionID,
                    error_text,
                    ref_msg_type=fix.MsgType_OrderCancelRequest,
                    ref_seq_num=resolved_ref_seq_num,
                    ref_tag_id=34,
                    session_reject_reason=1,
                ),
                ref_seq_num=resolved_ref_seq_num,
            )
            return
        try:
            cl_ord_id = self._get_field(message, fix.ClOrdID())
            orig_cl_ord_id = self._get_field(message, fix.OrigClOrdID())
            symbol = self._get_field(message, fix.Symbol()).strip().upper()
            side_value = self._get_field(message, fix.Side())
            raw_qty = self._get_field(message, fix.StringField(38))
            identity = self._session_identity(sessionID)
            self.order_normalizer._cl_ord_id(cl_ord_id)
            self.order_normalizer._cl_ord_id(orig_cl_ord_id)
            instrument = self.engine.instruments.get(symbol)
            if instrument is None:
                raise OrderValidationError(
                    OrderValidationCode.UNKNOWN_SYMBOL,
                    "symbol",
                    f"unsupported symbol: {symbol}",
                )
            qty_lots = instrument.quantity_to_lots(raw_qty)
            if side_value == fix.Side_BUY:
                order_side = Side.BUY
            elif side_value == fix.Side_SELL:
                order_side = Side.SELL
            else:
                raise ValueError(f"Invalid side: {side_value}")
            parsed = ParsedFixCommand(
                command_type=FixCommandType.CANCEL_ORDER,
                session=identity,
                msg_seq_num=msg_seq_num,
                cl_ord_id=cl_ord_id,
                orig_cl_ord_id=orig_cl_ord_id,
                symbol=symbol,
                side=order_side,
                order_qty_lots=qty_lots,
                list_id=self._get_field(message, fix.StringField(66)) if message.isSetField(66) else None,
                exchange_order_id=self._get_field(message, fix.StringField(37)) if message.isSetField(37) else None,
            )
            self._submit_ingress(
                parsed,
                token
                or IngressToken(
                    LifecycleState.ACTIVE,
                    self.identifier_scope.generation,
                ),
                sessionID,
            )
        except Exception as e:
            logger.error(f"Error parsing OrderCancelRequest: {e}")
            error_text = str(e)
            invalid_cl_ord_id = (
                cl_ord_id if "cl_ord_id" in locals() else "UNKNOWN"
            )
            invalid_orig_cl_ord_id = (
                orig_cl_ord_id
                if "orig_cl_ord_id" in locals()
                else "UNKNOWN"
            )

            def _reject_parse_error() -> None:
                invalid_command = CancelOrderCommand(
                    cancel_cl_ord_id=invalid_cl_ord_id,
                    orig_cl_ord_id=invalid_orig_cl_ord_id,
                    symbol=(
                        symbol
                        if "symbol" in locals() and symbol
                        else "UNKNOWN"
                    ),
                    side=(
                        order_side
                        if "order_side" in locals()
                        else Side.BUY
                    ),
                    order_qty_lots=(
                        qty_lots if "qty_lots" in locals() else 0
                    ),
                    requested_at=self.engine.scheduler.current_time,
                    session_id=sessionID.toString(),
                    session_alias=sessionID.getTargetCompID().getValue(),
                    participant_id=(
                        identity.participant_id
                        if "identity" in locals()
                        else None
                    ),
                    run_generation=self.identifier_scope.generation,
                )
                rejection = create_cancel_reject(
                    invalid_command,
                    reject_id=self.identifier_scope.next_cancel_reject_id(),
                    reason=CancelRejectReason.INVALID_REQUEST,
                    text=error_text,
                    timestamp=self.engine.scheduler.current_time,
                )
                rejection = replace(rejection, ingress_sequence=None, run_id=self.engine.run_id)
                self.enqueue_report(rejection)
                self.metrics.record_cancel_reject(rejection)

            self._schedule_lifecycle_action(
                sessionID,
                fix.MsgType_OrderCancelRequest,
                token
                or IngressToken(
                    LifecycleState.ACTIVE,
                    self.identifier_scope.generation,
                ),
                _reject_parse_error,
                ref_seq_num=msg_seq_num,
            )

    def process_cancel_envelope(self, envelope: FixIngressEnvelope) -> None:
        """Start the staged cancel lifecycle from a sorted receive event."""
        if envelope.command_type != FixCommandType.CANCEL_ORDER:
            raise TypeError("Expected a CANCEL_ORDER ingress envelope")
        duplicate = not self.session_registry.claim_cl_ord_id(
            envelope.session.session_id,
            envelope.cl_ord_id,
        )
        command = self._cancel_command(envelope, duplicate=duplicate)
        self.latency_tracker.cancel_received(command)
        self.engine.scheduler.schedule_cancel_processing(
            command,
            self.engine.exec_params.network_latency_ms,
            self.engine.exec_params.engine_latency_ms,
            session_alias=command.session_alias,
            inbound_ms=(
                self.engine.exec_params.inbound_network_latency_ms
            ),
        )

    def _cancel_command(
        self, envelope: FixIngressEnvelope, *, duplicate: bool
    ) -> CancelOrderCommand:
        return CancelOrderCommand(
            cancel_cl_ord_id=envelope.cl_ord_id,
            orig_cl_ord_id=envelope.orig_cl_ord_id or "",
            symbol=envelope.symbol,
            side=envelope.side,
            order_qty_lots=envelope.order_qty_lots,
            requested_at=envelope.simulated_receive_timestamp,
            session_id=envelope.session.session_id,
            session_alias=envelope.session.alias,
            participant_id=envelope.session.participant_id,
            run_generation=envelope.run_generation,
            ingress_sequence=envelope.ingress_sequence,
            msg_seq_num=envelope.msg_seq_num,
            duplicate_request=duplicate,
            list_id=envelope.list_id,
            exchange_order_id=envelope.exchange_order_id,
            run_id=self.engine.run_id,
        )

    def process_replace_envelope(self, envelope: FixIngressEnvelope) -> None:
        if envelope.command_type != FixCommandType.REPLACE_ORDER:
            raise TypeError("Expected REPLACE_ORDER ingress envelope")
        duplicate = not self.session_registry.claim_cl_ord_id(envelope.session.session_id, envelope.cl_ord_id)
        command = self._replace_command(envelope, duplicate=duplicate)
        self.latency_tracker.replace_received(command)
        self.engine.scheduler.schedule_replace_processing(
            command, self.engine.exec_params.network_latency_ms, self.engine.exec_params.engine_latency_ms,
            session_alias=command.session_alias, inbound_ms=self.engine.exec_params.simulated_inbound_latency_ms,
        )

    def _replace_command(self, envelope: FixIngressEnvelope, *, duplicate: bool) -> ReplaceOrderCommand:
        return ReplaceOrderCommand(
            envelope.cl_ord_id, envelope.orig_cl_ord_id or "", envelope.symbol, envelope.side,
            envelope.order_qty_lots, envelope.price_ticks or 0, envelope.simulated_receive_timestamp,
            envelope.session.session_id, envelope.session.alias, envelope.session.participant_id,
            envelope.run_generation, envelope.ingress_sequence, envelope.msg_seq_num, duplicate,
            list_id=envelope.list_id, exchange_order_id=envelope.exchange_order_id, run_id=self.engine.run_id,
        )

    def handle_market_data_request(
        self,
        message,
        sessionID,
        token: Optional[IngressToken] = None,
        ref_seq_num: Optional[int] = None,
    ):
        """Handle MarketDataRequest (35=V) message."""
        # For now, send a simple reject as market data is handled via REST/WebSocket
        req_id = self._get_field(message, fix.MDReqID())
        self._schedule_lifecycle_action(
            sessionID,
            fix.MsgType_MarketDataRequest,
            token
            or IngressToken(
                LifecycleState.ACTIVE,
                self.identifier_scope.generation,
            ),
            lambda: self.send_market_data_reject(
                sessionID,
                req_id,
                "Market data available via REST API",
            ),
            ref_seq_num=(
                ref_seq_num
                or self._resolve_ref_seq_num(message, sessionID)
            ),
        )

    def _send_to_quickfix(
        self,
        transport_session: object,
        report: RoutableReport,
    ) -> bool:
        """Serialize and send through QuickFIX at the adapter boundary."""
        if fix is None:
            raise RuntimeError("QuickFIX is unavailable")
        try:
            if isinstance(report, ExecutionReport):
                message = self._send_quickfix_execution_report(report)
            else:
                message = self._send_quickfix_cancel_reject(report)
        except Exception as exc:
            raise FixDeliverySerializationError(
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return bool(fix.Session.sendToTarget(message, transport_session))

    def _send_quickfix_execution_report(
        self,
        report: ExecutionReport,
    ):
        """Serialize one immutable execution report."""
        serialize_start_ns = self.monotonic_clock.now_ns()
        fields = execution_report_fields(report)
        msg = fix.Message()
        msg.getHeader().setField(fix.MsgType(fix.MsgType_ExecutionReport))

        # Required fields
        msg.setField(fix.ClOrdID(str(fields["ClOrdID"])))
        msg.setField(fix.ExecType(str(fields["ExecType"])))
        msg.setField(fix.OrdStatus(str(fields["OrdStatus"])))
        msg.setField(fix.ExecID(str(fields["ExecID"])))
        msg.setField(fix.OrderID(str(fields["OrderID"])))
        msg.setField(fix.Symbol(str(fields["Symbol"])))
        msg.setField(fix.Side(str(fields["Side"])))
        msg.setField(fix.StringField(151, str(fields["LeavesQty"])))
        msg.setField(fix.StringField(14, str(fields["CumQty"])))
        msg.setField(fix.StringField(6, str(fields["AvgPx"])))
        msg.setField(fix.StringField(38, str(fields["OrderQty"])))
        msg.setField(fix.OrdType(str(fields["OrdType"])))
        msg.setField(fix.TimeInForce(str(fields["TimeInForce"])))

        # Optional fields
        if "OrigClOrdID" in fields:
            msg.setField(fix.OrigClOrdID(str(fields["OrigClOrdID"])))
        if "ListID" in fields:
            msg.setField(fix.StringField(66, str(fields["ListID"])))
        if "Price" in fields:
            msg.setField(fix.StringField(44, str(fields["Price"])))
        if "LastPx" in fields:
            msg.setField(fix.StringField(31, str(fields["LastPx"])))
        if "LastQty" in fields:
            msg.setField(fix.StringField(32, str(fields["LastQty"])))
        if "TradeID" in fields:
            msg.setField(fix.StringField(1003, str(fields["TradeID"])))
        if "Text" in fields:
            msg.setField(fix.Text(str(fields["Text"])))
        if "OrdRejReason" in fields:
            msg.setField(fix.OrdRejReason(int(fields["OrdRejReason"])))

        serialize_end_ns = self.monotonic_clock.now_ns()
        try:
            duration_ns = monotonic_duration_ns(
                serialize_start_ns,
                serialize_end_ns,
                context=f"execution report {report.exec_id} serialization",
            )
        except ValueError:
            self.metrics.record_invalid_latency(
                metric=SERIALIZATION_DURATION,
                order_id=report.order_id,
                session_id=report.session_id,
            )
        else:
            try:
                self.latency_tracker.record_serialization_duration(
                    report,
                    duration_ns,
                )
            except Exception:
                logger.exception(
                    "FIX serialization metric observer failed",
                    extra={
                        "order_id": report.order_id,
                        "session_id": report.session_id,
                        "report_type": type(report).__name__,
                    },
                )

        return msg

    def _send_quickfix_cancel_reject(
        self,
        cancel_reject: CancelReject,
    ):
        """Serialize one immutable OrderCancelReject."""
        serialize_start_ns = self.monotonic_clock.now_ns()
        fields = cancel_reject_fields(cancel_reject)
        reject = fix.Message()
        reject.getHeader().setField(fix.MsgType(fix.MsgType_OrderCancelReject))

        # Required fields
        reject.setField(fix.ClOrdID(str(fields["ClOrdID"])))
        reject.setField(fix.OrigClOrdID(str(fields["OrigClOrdID"])))
        reject.setField(fix.OrdStatus(str(fields["OrdStatus"])))
        reject.setField(
            fix.CxlRejResponseTo(str(fields["CxlRejResponseTo"]))
        )

        # The OrderID is conditionally required. If the cancel request is rejected and the order exists,
        # it should be included.
        if "OrderID" in fields:
            reject.setField(fix.OrderID(str(fields["OrderID"])))

        # Optional fields
        reject.setField(fix.CxlRejReason(int(fields["CxlRejReason"])))
        reject.setField(fix.Text(str(fields["Text"])))
        if "ListID" in fields:
            reject.setField(fix.StringField(66, str(fields["ListID"])))

        serialize_end_ns = self.monotonic_clock.now_ns()
        try:
            duration_ns = monotonic_duration_ns(
                serialize_start_ns,
                serialize_end_ns,
                context=(
                    f"cancel reject {cancel_reject.reject_id} serialization"
                ),
            )
        except ValueError:
            self.metrics.record_invalid_latency(
                metric=SERIALIZATION_DURATION,
                order_id=cancel_reject.order_id,
                session_id=cancel_reject.session_id,
            )
        else:
            self.latency_tracker.record_cpu_duration(
                SERIALIZATION_DURATION,
                cancel_reject.order_id,
                cancel_reject.session_id,
                duration_ns,
                self.engine.scheduler.current_time,
                cancel_reject.reject_id,
            )

        return reject

    def send_market_data_reject(self, sessionID, md_req_id: str, text: str):
        """Send MarketDataRequestReject (35=Y) message."""
        reject = fix.Message()
        reject.getHeader().setField(fix.MsgType(fix.MsgType_MarketDataRequestReject))

        reject.setField(fix.MDReqID(md_req_id))
        reject.setField(fix.MDReqRejReason('0'))  # Unknown symbol
        reject.setField(fix.Text(text))

        fix.Session.sendToTarget(reject, sessionID)

    @staticmethod
    def _build_session_reject(
        *,
        text: str,
        ref_seq_num: int,
        ref_msg_type: str,
        session_reject_reason: int,
        ref_tag_id: Optional[int] = None,
    ):
        """Build one dictionary-complete FIX session Reject."""
        if ref_seq_num <= 0:
            raise ValueError("RefSeqNum must be positive")
        if not ref_msg_type:
            raise ValueError("RefMsgType is required")
        reject = fix.Message()
        reject.getHeader().setField(fix.MsgType(fix.MsgType_Reject))
        reject.setField(fix.RefSeqNum(ref_seq_num))
        reject.setField(fix.RefMsgType(ref_msg_type))
        if ref_tag_id is not None:
            reject.setField(fix.RefTagID(ref_tag_id))
        reject.setField(fix.SessionRejectReason(session_reject_reason))
        reject.setField(fix.Text(text))
        return reject

    @staticmethod
    def _build_business_reject(
        *,
        text: str,
        ref_msg_type: str,
        business_reject_reason: int,
        ref_seq_num: Optional[int] = None,
        business_reject_ref_id: Optional[str] = None,
    ):
        """Build one dictionary-complete BusinessMessageReject."""
        if not ref_msg_type:
            raise ValueError("RefMsgType is required")
        if ref_seq_num is not None and ref_seq_num <= 0:
            raise ValueError("RefSeqNum must be positive when supplied")
        reject = fix.Message()
        reject.getHeader().setField(fix.MsgType("j"))
        if ref_seq_num is not None:
            reject.setField(fix.RefSeqNum(ref_seq_num))
        reject.setField(fix.RefMsgType(ref_msg_type))
        if business_reject_ref_id:
            reject.setField(fix.BusinessRejectRefID(business_reject_ref_id))
        reject.setField(fix.BusinessRejectReason(business_reject_reason))
        reject.setField(fix.Text(text))
        return reject

    def send_reject(
        self,
        sessionID,
        text: str,
        *,
        ref_seq_num: int,
        ref_msg_type: str,
        session_reject_reason: int,
        ref_tag_id: Optional[int] = None,
    ) -> None:
        reject = self._build_session_reject(
            text=text,
            ref_seq_num=ref_seq_num,
            ref_msg_type=ref_msg_type,
            session_reject_reason=session_reject_reason,
            ref_tag_id=ref_tag_id,
        )
        fix.Session.sendToTarget(reject, sessionID)

    def send_business_reject(
        self,
        sessionID,
        text: str,
        *,
        ref_msg_type: str,
        business_reject_reason: int,
        ref_seq_num: Optional[int] = None,
        business_reject_ref_id: Optional[str] = None,
    ) -> None:
        reject = self._build_business_reject(
            text=text,
            ref_msg_type=ref_msg_type,
            business_reject_reason=business_reject_reason,
            ref_seq_num=ref_seq_num,
            business_reject_ref_id=business_reject_ref_id,
        )
        fix.Session.sendToTarget(reject, sessionID)

    def enqueue_report(
        self,
        report: RoutableReport,
        *,
        delay_ms: Optional[int] = None,
    ):
        """Persist one immutable logical report before scheduling its due event."""
        session_id = report_destination_id(report)
        if not session_id:
            logger.debug(
                "Skipping FIX delivery for report without a destination",
                extra={"report_type": type(report).__name__},
            )
            return None
        resolved_delay = (
            self.engine.exec_params.simulated_outbound_latency_ms
            if delay_ms is None
            else delay_ms
        )
        if (
            not isinstance(resolved_delay, int)
            or isinstance(resolved_delay, bool)
            or resolved_delay < 0
        ):
            raise ValueError("FIX delivery delay must be a non-negative integer")
        identity = self.session_registry.resolve_identity(session_id)
        if identity is None:
            identity = SessionIdentity(
                session_id=session_id,
                alias=report.session_alias,
            )
        record, fresh = self.delivery_queue.enqueue(
            delivery_id=self.identifier_scope.next_delivery_id(),
            destination=identity,
            payload=report,
            simulated_due_timestamp=(
                self.engine.scheduler.current_time
                + timedelta(milliseconds=resolved_delay)
            ),
        )
        if fresh:
            if record is None:
                raise RuntimeError("Fresh FIX delivery has no retained record")
            command = self.delivery_queue.current_due_command(record.delivery_id)
            if command is None:
                raise RuntimeError("Fresh FIX delivery has no armed deadline")
            self.engine.scheduler.schedule_event(
                "fix_delivery_due",
                command,
                resolved_delay,
            )
            self.metrics.record_fix_delivery_enqueued()
            self._refresh_delivery_metrics()
        return record

    def process_delivery_due(self, command: FixDeliveryDueCommand) -> None:
        """Consume a current deadline and attempt the destination FIFO."""
        record = self.delivery_queue.mark_due(
            command, simulated_timestamp=self.engine.scheduler.current_time
        )
        if record is None:
            return
        self._refresh_delivery_metrics()
        self._drain_session(record.destination.session_id)

    def retry_pending_for_session(self, session_id: str) -> None:
        """Begin a fresh bounded retry cycle after a stable-session logon."""
        record = self.delivery_queue.start_reconnect_cycle(session_id)
        if record is None:
            return
        self._refresh_delivery_metrics()
        self._drain_session(session_id)

    def reschedule_deliveries_after_reset(self) -> None:
        """Reinstall nonterminal delivery events after scheduler reset."""
        for command, delay_ms in (
            self.delivery_queue.prepare_after_scheduler_reset(
                self.engine.scheduler.current_time
            )
        ):
            self.engine.scheduler.schedule_event(
                "fix_delivery_due",
                command,
                delay_ms,
            )
        self._refresh_delivery_metrics()

    def _drain_session(self, session_id: str) -> None:
        with self._draining_sessions_lock:
            if session_id in self._draining_sessions:
                return
            self._draining_sessions.add(session_id)
        try:
            while True:
                record = self.delivery_queue.head_ready(session_id)
                if record is None:
                    return
                transport = self.session_registry.resolve_transport(session_id)
                if transport is None:
                    self.delivery_queue.note_unavailable(
                        record.delivery_id,
                        reason="FIX session is disconnected",
                    )
                    self.metrics.record_report_delivery_failure(record.payload)
                    self.metrics.record_fix_delivery_failure()
                    self._refresh_delivery_metrics()
                    logger.warning(
                        "FIX delivery retained for disconnected session",
                        extra={
                            "delivery_id": record.delivery_id,
                            "domain_id": record.domain_id,
                            "session_id": session_id,
                        },
                    )
                    return

                attempted = self.delivery_queue.start_attempt(
                    record.delivery_id,
                    simulated_timestamp=self.engine.scheduler.current_time,
                )
                self.metrics.record_fix_delivery_attempt()
                if attempted.attempt_count > 1:
                    self.metrics.record_fix_delivery_retry()
                self._refresh_delivery_metrics()
                try:
                    result = self.transport_sender(
                        transport,
                        attempted.payload,
                    )
                except FixDeliverySerializationError as exc:
                    self.delivery_queue.record_permanent_failure(
                        attempted.delivery_id,
                        reason=str(exc),
                    )
                    self.metrics.record_report_delivery_failure(
                        attempted.payload
                    )
                    self.metrics.record_fix_delivery_failure(permanent=True)
                    self._refresh_delivery_metrics()
                    logger.exception(
                        "FIX delivery permanently failed serialization",
                        extra={
                            "delivery_id": attempted.delivery_id,
                            "domain_id": attempted.domain_id,
                            "session_id": session_id,
                        },
                    )
                    return
                except Exception as exc:
                    self._record_retryable_failure(attempted, exc)
                    return
                if result is False:
                    self._record_retryable_failure(
                        attempted,
                        RuntimeError("QuickFIX sendToTarget returned False"),
                    )
                    return

                delivered = self.delivery_queue.record_delivered(
                    attempted.delivery_id
                )
                self.metrics.record_fix_delivery_success()
                self._refresh_delivery_metrics()
                self._complete_delivery(delivered.payload)
        finally:
            with self._draining_sessions_lock:
                self._draining_sessions.discard(session_id)

    def _record_retryable_failure(
        self,
        record,
        exc: BaseException,
    ) -> None:
        failed = self.delivery_queue.record_transport_failure(
            record.delivery_id,
            reason=f"{type(exc).__name__}: {exc}",
            simulated_timestamp=self.engine.scheduler.current_time,
        )
        self.metrics.record_report_delivery_failure(record.payload)
        self.metrics.record_fix_delivery_failure()
        self._refresh_delivery_metrics()
        logger.warning(
            "FIX delivery failed and was retained",
            extra={
                "delivery_id": record.delivery_id,
                "domain_id": record.domain_id,
                "session_id": record.destination.session_id,
                "attempt_count": failed.attempt_count,
                "last_failure_reason": failed.last_failure_reason,
            },
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if failed.next_retry_timestamp is not None:
            command = self.delivery_queue.current_due_command(failed.delivery_id)
            if command is None:
                raise RuntimeError("FIX retry has no armed deadline")
            self.engine.scheduler.schedule_event(
                "fix_delivery_due",
                command,
                self.delivery_queue.retry_interval_ms,
            )

    def _complete_delivery(self, report: RoutableReport) -> None:
        if self.on_report_delivered is not None:
            self.on_report_delivered(report, self.engine.scheduler.current_time)
        try:
            if isinstance(report, CancelReject):
                if report.ingress_sequence is None:
                    return
                if report.response_to == "2":
                    self.latency_tracker.replace_report_delivered(report, delivered_at=self.engine.scheduler.current_time)
                else:
                    self.latency_tracker.cancel_report_delivered(report, delivered_at=self.engine.scheduler.current_time)
                return
            if report.replace_ingress_sequence is not None:
                self.latency_tracker.replace_report_delivered(report, delivered_at=self.engine.scheduler.current_time)
            elif report.cancel_ingress_sequence is None:
                self.latency_tracker.report_delivered(
                    report,
                    self.engine.scheduler.current_time,
                )
            else:
                self.latency_tracker.cancel_report_delivered(
                    report,
                    delivered_at=self.engine.scheduler.current_time,
                )
            if (
                report.ord_status in {
                    OrderStatus.FILLED,
                    OrderStatus.REJECTED,
                    OrderStatus.CANCELLED,
                }
                and self.latency_tracker.complete_order(report)
            ):
                self.latency_tracker.cleanup_order(
                    report.order_id,
                    run_generation=report.run_generation,
                )
        except Exception:
            logger.exception(
                "FIX delivery completion observer failed",
                extra={
                    "domain_id": (
                        report.exec_id
                        if isinstance(report, ExecutionReport)
                        else report.reject_id
                    ),
                    "session_id": report.session_id,
                },
            )

    def _refresh_delivery_metrics(self) -> None:
        self.metrics.update_fix_delivery_gauges(
            self.delivery_queue.snapshot()
        )

    def _get_field(self, message, field):
        """
        Get field value from FIX message.

        Args:
            message: FIX message
            field: FIX field object

        Returns:
            Field value
        """
        if not fix:
            return ""

        message.getField(field)
        return field.getValue()

    @staticmethod
    def _get_msg_seq_num(message) -> int:
        """Read the required FIX header sequence as a validated integer."""
        field = fix.MsgSeqNum()
        message.getHeader().getField(field)
        try:
            value = int(field.getValue())
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid or missing FIX MsgSeqNum") from exc
        if value <= 0:
            raise ValueError("FIX MsgSeqNum must be positive")
        return value

    @staticmethod
    def _session_ref_seq_num(sessionID) -> int:
        """Use QuickFIX sequence state only when the inbound header is unusable."""
        if fix is not None:
            try:
                session = fix.Session.lookupSession(sessionID)
                if session is not None:
                    value = int(session.getExpectedTargetNum())
                    if value > 0:
                        return value
            except Exception:
                logger.debug(
                    "Unable to read QuickFIX target sequence fallback",
                    exc_info=True,
                )
        return 1

    def _resolve_ref_seq_num(self, message, sessionID) -> int:
        try:
            return self._get_msg_seq_num(message)
        except Exception:
            return self._session_ref_seq_num(sessionID)

    def _session_identity(self, sessionID) -> SessionIdentity:
        """Convert a live QuickFIX SessionID to a domain-safe identity."""
        session_id = sessionID.toString()
        participant_id = self.session_participants.get(session_id)
        if participant_id is None:
            raise RuntimeError(
                f"FIX session has no participant mapping: {session_id}"
            )
        return SessionIdentity(
            session_id=session_id,
            sender_comp_id=sessionID.getSenderCompID().getValue(),
            target_comp_id=sessionID.getTargetCompID().getValue(),
            alias=sessionID.getTargetCompID().getValue(),
            participant_id=participant_id,
        )

class FIXGateway:
    """Manages FIX protocol connectivity."""

    def __init__(
        self,
        fix_cfg: str,
        engine,
        metrics: MetricsCollector,
        latency_tracker: LatencyTracker,
        *,
        application: Optional[FIXApplication] = None,
        session_participants: Optional[Mapping[str, str]] = None,
    ):
        self.session_participants = dict(session_participants or {})
        self.application = application or FIXApplication(
            engine,
            metrics,
            latency_tracker,
            session_participants=self.session_participants,
        )

        # FIX components
        self.fix_cfg = fix_cfg
        self.fix_acceptor = None
        self.fix_settings = None
        self.fix_store_factory = None
        self.fix_log_factory = None
        self.accept_port = None
        self.started = False

    def start(self):
        """Start FIX acceptor."""
        if self.started:
            return
        if fix is None:
            raise RuntimeError(
                "QuickFIX is unavailable; install the native quickfix module to start the FIX gateway"
            )
        if self.application.loop is None:
            raise RuntimeError(
                "FIX event-loop handoff must be configured before acceptor start"
            )
        try:
            logger.info(f"Using FIX configuration: {self.fix_cfg}")

            # Create FIX settings
            self.fix_settings = fix.SessionSettings(self.fix_cfg)
            configured_sessions = configured_fix_session_ids(self.fix_cfg)
            mapped_sessions = set(self.session_participants)
            missing = sorted(configured_sessions - mapped_sessions)
            extra = sorted(mapped_sessions - configured_sessions)
            if missing or extra:
                details = []
                if missing:
                    details.append(
                        "missing participant mappings: " + ", ".join(missing)
                    )
                if extra:
                    details.append(
                        "unknown mapped FIX sessions: " + ", ".join(extra)
                    )
                raise ValueError("; ".join(details))
            # self.accept_port = self.get_session_setting("SocketAcceptPort")

            # Create store and log factories
            self.fix_store_factory = fix.MemoryStoreFactory()
            self.fix_log_factory = fix.ScreenLogFactory(self.fix_settings)

            # Create acceptor
            self.fix_acceptor = fix.SocketAcceptor(
                self.application,
                self.fix_store_factory,
                self.fix_settings,
                self.fix_log_factory
            )

            # Start acceptor
            self.fix_acceptor.start()
            self.started = True

            try:
                self.accept_port = self.get_session_setting(
                    "SocketAcceptPort"
                )
            except Exception:
                logger.debug(
                    "FIX accept port was not available from session settings",
                    exc_info=True,
                )
            logger.info(
                "FIX acceptor started%s",
                f" on port {self.accept_port}" if self.accept_port else "",
            )

        except Exception as e:
            logger.error(f"Failed to start FIX gateway: {e}")
            raise

    def stop(self):
        """Stop FIX acceptor."""
        if self.fix_acceptor and self.started:
            self.fix_acceptor.stop()
            self.started = False
            logger.info("FIX acceptor stopped")

    def process_execution_report(self, report: ExecutionReport):
        """Process execution report from scheduler."""
        return self.application.process_execution_report(report)

    def get_session_setting(self, key: str) -> Optional[str]:
        """Get a specific setting value from the FIX session configuration."""
        sender = self.fix_settings.get().getString("SenderCompID")
        target = self.fix_settings.get().getString("TargetCompID")
        session_id = fix.SessionID("FIX.4.4", sender, target)
        session_dict = self.fix_settings.get(session_id)
        return session_dict.getString(key)

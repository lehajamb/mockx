"""FIX protocol gateway for the mock exchange."""

try:
    import quickfix as fix
except ModuleNotFoundError:  # Pure report mapping and non-FIX tests remain importable.
    fix = None
import asyncio
from concurrent.futures import Future
import logging
from typing import Callable, Dict, Optional, Union
import math
import threading

from .identifiers import RunIdentifierScope
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
    ExecutionReport,
    ExecutionType,
    FixCommandType,
    FixIngressEnvelope,
    IngressBarrier,
    NewOrderCommand,
    OrderStatus,
    ParsedFixCommand,
    SessionIdentity,
    Side,
    TimeInForce,
    create_cancel_reject,
)
from .fix_fields import cancel_reject_fields, execution_report_fields
from .metrics import MetricsCollector, LatencyTracker
from .engine import MatchingEngine
from .latency import MonotonicClock, PerfCounterClock, monotonic_duration_ns

logger = logging.getLogger(__name__)

_FIX_APPLICATION_BASE = fix.Application if fix is not None else object


RoutableReport = Union[ExecutionReport, CancelReject]


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
            Callable[[object, RoutableReport], None]
        ] = None,
        monotonic_clock: Optional[MonotonicClock] = None,
        lifecycle: Optional[ExchangeLifecycle] = None,
    ):
        super().__init__()
        self.engine = engine
        self.metrics = metrics
        self.latency_tracker = latency_tracker
        self.session_registry = session_registry or SessionRegistry()
        self.transport_sender = transport_sender or self._send_to_quickfix
        self.monotonic_clock = monotonic_clock or PerfCounterClock()
        self.lifecycle = lifecycle
        self._metric_suppression = threading.local()
        self.identifier_scope = engine.identifier_scope
        self.loop = None
        self._ingress_lock = threading.RLock()
        self._next_ingress_sequence = 0
        self._ingress_receipts: Dict[
            tuple[int, int], Future[FixIngressEnvelope]
        ] = {}
        self._pending_ingress: list[FixIngressEnvelope] = []

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
                )
                return

        try:
            if msg_type == fix.MsgType_NewOrderSingle:
                self.handle_new_order_single(message, sessionID, token)
            elif msg_type == fix.MsgType_OrderCancelRequest:
                self.handle_order_cancel_request(message, sessionID, token)
            elif msg_type == fix.MsgType_MarketDataRequest:
                self.handle_market_data_request(message, sessionID, token)
            else:
                logger.warning(f"Unsupported message type: {msg_type}")
                self._schedule_lifecycle_action(
                    sessionID,
                    msg_type,
                    token,
                    lambda: self.send_reject(
                        sessionID,
                        f"Unsupported message type: {msg_type}",
                        ref_msg_type=msg_type,
                    ),
                )
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            self.send_reject(sessionID, str(e), ref_msg_type=msg_type)

    def _schedule_lifecycle_action(
        self,
        sessionID,
        ref_msg_type: str,
        token: IngressToken,
        action: Callable[[], object],
    ) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            self._send_lifecycle_reject(
                sessionID,
                "LIFECYCLE_FAILED",
                ref_msg_type=ref_msg_type,
            )
            return

        def _record_and_run():
            self.metrics.record_fix_message(
                ref_msg_type,
                "received",
                self.engine.scheduler.current_time_ns(),
            )
            return action()

        if self.lifecycle is None:
            loop.call_soon_threadsafe(_record_and_run)
            return

        async def _execute() -> None:
            try:
                await self.lifecycle.execute_ingress(token, _record_and_run)
            except LifecycleIngressRejected as exc:
                self._send_lifecycle_reject(
                    sessionID,
                    exc.code,
                    ref_msg_type=ref_msg_type,
                )

        loop.call_soon_threadsafe(lambda: loop.create_task(_execute()))

    def _submit_ingress(
        self,
        parsed: ParsedFixCommand,
        token: IngressToken,
        transport_session: object,
    ) -> int:
        """Reserve a run-local sequence and marshal immutable data to the loop."""
        loop = self.loop
        if loop is None or loop.is_closed():
            self._send_lifecycle_reject(
                transport_session,
                "LIFECYCLE_FAILED",
                ref_msg_type=(
                    fix.MsgType_NewOrderSingle
                    if parsed.command_type == FixCommandType.NEW_ORDER
                    else fix.MsgType_OrderCancelRequest
                ),
            )
            raise LifecycleIngressRejected("LIFECYCLE_FAILED")

        with self._ingress_lock:
            sequence = self._next_ingress_sequence
            self._next_ingress_sequence += 1
            receipt: Future[FixIngressEnvelope] = Future()
            self._ingress_receipts[(token.generation, sequence)] = receipt

        async def _enqueue() -> None:
            def _commit() -> FixIngressEnvelope:
                envelope = FixIngressEnvelope(
                    command_type=parsed.command_type,
                    session=parsed.session,
                    msg_seq_num=parsed.msg_seq_num,
                    cl_ord_id=parsed.cl_ord_id,
                    orig_cl_ord_id=parsed.orig_cl_ord_id,
                    symbol=parsed.symbol,
                    side=parsed.side,
                    qty=parsed.qty,
                    price=parsed.price,
                    tif=parsed.tif,
                    ingress_sequence=sequence,
                    simulated_receive_timestamp=(
                        self.engine.scheduler.current_time
                    ),
                    run_generation=token.generation,
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
                with self._ingress_lock:
                    if not receipt.done():
                        receipt.set_exception(exc)
                self._send_lifecycle_reject(
                    transport_session,
                    exc.code,
                    ref_msg_type=(
                        fix.MsgType_NewOrderSingle
                        if parsed.command_type == FixCommandType.NEW_ORDER
                        else fix.MsgType_OrderCancelRequest
                    ),
                )
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
        """Wait until the captured run-local commands are centrally enqueued."""
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

        for envelope in ready:
            msg_type = (
                fix.MsgType_NewOrderSingle
                if envelope.command_type == FixCommandType.NEW_ORDER
                else fix.MsgType_OrderCancelRequest
            )
            self.metrics.record_fix_message(
                msg_type,
                "received",
                self.engine.scheduler.current_time_ns(),
            )
            self.engine.scheduler.schedule_event(
                (
                    "new_order_received"
                    if envelope.command_type == FixCommandType.NEW_ORDER
                    else "cancel_received"
                ),
                envelope,
                0,
            )
            with self._ingress_lock:
                self._ingress_receipts.pop(
                    (envelope.run_generation, envelope.ingress_sequence),
                    None,
                )
        return IngressBarrier(generation, through_sequence)

    async def wait_for_ingress(
        self,
        through_sequence: Optional[int] = None,
    ) -> IngressBarrier:
        """Acknowledge and schedule the captured FIX ingress high-water mark."""
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
        ref_msg_type: Optional[str],
    ) -> None:
        previous = getattr(
            self._metric_suppression,
            "suppress_to_app",
            False,
        )
        self._metric_suppression.suppress_to_app = True
        try:
            self.send_reject(
                sessionID,
                text,
                ref_msg_type=ref_msg_type,
            )
        finally:
            self._metric_suppression.suppress_to_app = previous

    def handle_new_order_single(
        self,
        message,
        sessionID,
        token: Optional[IngressToken] = None,
    ):
        """Parse NewOrderSingle into immutable data and enqueue it centrally."""
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
                ),
            )
            return
        try:
            cl_ord_id = self._get_field(message, fix.ClOrdID())
            symbol = self._get_field(message, fix.Symbol()).strip().upper()
            side = self._get_field(message, fix.Side())
            order_qty = self._get_field(message, fix.OrderQty())
            ord_type = self._get_field(message, fix.OrdType())
            identity = self._session_identity(sessionID)

            if side == fix.Side_BUY:
                order_side = Side.BUY
            elif side == fix.Side_SELL:
                order_side = Side.SELL
            else:
                raise ValueError(f"Invalid side: {side}")

            price = None
            if ord_type == fix.OrdType_LIMIT:
                price = float(self._get_field(message, fix.Price()))
            elif ord_type != fix.OrdType_MARKET:
                raise ValueError(f"Unsupported order type: {ord_type}")

            tif = TimeInForce.GTC
            if message.isSetField(fix.TimeInForce()):
                tif_value = self._get_field(message, fix.TimeInForce())
                if tif_value == fix.TimeInForce_IMMEDIATE_OR_CANCEL:
                    tif = TimeInForce.IOC
                elif tif_value != fix.TimeInForce_GOOD_TILL_CANCEL:
                    raise ValueError(f"Unsupported TimeInForce: {tif_value}")

            parsed = ParsedFixCommand(
                command_type=FixCommandType.NEW_ORDER,
                session=identity,
                msg_seq_num=msg_seq_num,
                cl_ord_id=cl_ord_id,
                symbol=symbol,
                side=order_side,
                qty=float(order_qty),
                price=price,
                tif=tif,
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
            logger.error(f"Error handling NewOrderSingle: {e}")
            _cl_ord_id = cl_ord_id if 'cl_ord_id' in locals() else "UNKNOWN"
            _symbol = symbol if 'symbol' in locals() else "UNKNOWN"
            _side = order_side if 'order_side' in locals() else Side.BUY
            _qty = float(order_qty) if 'order_qty' in locals() else 0.0
            _price = price if 'price' in locals() else None
            _tif = tif if 'tif' in locals() else TimeInForce.GTC
            _identity = (
                identity
                if "identity" in locals()
                else self._session_identity(sessionID)
            )
            _error_text = str(e)

            def _reject():
                self._send_input_rejection(
                    sessionID,
                    cl_ord_id=_cl_ord_id,
                    symbol=_symbol,
                    side=_side,
                    qty=_qty,
                    price=_price,
                    tif=_tif,
                    session_id=_identity.session_id,
                    session_alias=_identity.alias,
                    text=_error_text,
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
            )

    def process_new_order_envelope(
        self,
        envelope: FixIngressEnvelope,
    ) -> Optional[int]:
        """Handle a deterministically ordered new-order receive event."""
        if envelope.command_type != FixCommandType.NEW_ORDER:
            raise TypeError("Expected a NEW_ORDER ingress envelope")
        if envelope.run_generation != self.identifier_scope.generation:
            return None
        command = NewOrderCommand(
            cl_ord_id=envelope.cl_ord_id,
            symbol=envelope.symbol,
            side=envelope.side,
            qty=envelope.qty,
            price=envelope.price,
            tif=envelope.tif or TimeInForce.GTC,
            submitted_at=envelope.simulated_receive_timestamp,
            session_id=envelope.session.session_id,
            session_alias=envelope.session.alias,
        )
        if not self.session_registry.claim_cl_ord_id(
            command.session_id,
            command.cl_ord_id,
        ):
            self._send_input_rejection(
                None,
                cl_ord_id=command.cl_ord_id,
                symbol=command.symbol,
                side=command.side,
                qty=command.qty,
                price=command.price,
                tif=command.tif,
                session_id=command.session_id,
                session_alias=command.session_alias,
                text=(
                    "Duplicate ClOrdID in FIX session: "
                    f"{command.cl_ord_id}"
                ),
            )
            return None
        order_id = self.engine.submit_order(command)
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
        sessionID,
        *,
        cl_ord_id: str,
        symbol: str,
        side: Side,
        qty: float,
        price: Optional[float],
        tif: TimeInForce,
        session_id: Optional[str],
        session_alias: Optional[str],
        text: str,
    ) -> None:
        """Build a terminal immutable snapshot for a malformed new order."""
        simulated_time = self.engine.scheduler.current_time
        safe_qty = qty if math.isfinite(qty) and qty >= 0 else 0.0
        safe_price = (
            price
            if price is not None and math.isfinite(price) and price > 0.0
            else None
        )
        exec_id = self.identifier_scope.next_exec_id()
        exchange_order_id = self.identifier_scope.next_order_id()
        report = ExecutionReport(
            exec_id=exec_id,
            order_id=0,
            exchange_order_id=exchange_order_id,
            run_generation=self.identifier_scope.generation,
            cl_ord_id=cl_ord_id,
            symbol=symbol,
            side=side,
            order_qty=safe_qty,
            order_price=safe_price,
            tif=tif,
            ord_status=OrderStatus.REJECTED,
            exec_type=ExecutionType.REJECTED,
            leaves_qty=0.0,
            cum_qty=0.0,
            canceled_qty=safe_qty,
            avg_px=0.0,
            reject_reason=text,
            text=text,
            session_id=session_id,
            session_alias=session_alias,
            submitted_at=simulated_time,
            timestamp=simulated_time,
            order_version=0,
        )
        self.metrics.record_order(report)
        self.process_execution_report(report)

    def handle_order_cancel_request(
        self,
        message,
        sessionID,
        token: Optional[IngressToken] = None,
    ):
        """Parse OrderCancelRequest into immutable data and enqueue it."""
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
                ),
            )
            return
        try:
            cl_ord_id = self._get_field(message, fix.ClOrdID())
            orig_cl_ord_id = self._get_field(message, fix.OrigClOrdID())
            symbol = self._get_field(message, fix.Symbol()).strip().upper()
            side = self._get_field(message, fix.Side())
            qty = float(self._get_field(message, fix.OrderQty()))
            identity = self._session_identity(sessionID)
            if side == fix.Side_BUY:
                order_side = Side.BUY
            elif side == fix.Side_SELL:
                order_side = Side.SELL
            else:
                raise ValueError(f"Invalid side: {side}")
            parsed = ParsedFixCommand(
                command_type=FixCommandType.CANCEL_ORDER,
                session=identity,
                msg_seq_num=msg_seq_num,
                cl_ord_id=cl_ord_id,
                orig_cl_ord_id=orig_cl_ord_id,
                symbol=symbol,
                side=order_side,
                qty=qty,
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
                    qty=(
                        qty
                        if "qty" in locals()
                        and math.isfinite(qty)
                        and qty > 0
                        else 1.0
                    ),
                    requested_at=self.engine.scheduler.current_time,
                    session_id=sessionID.toString(),
                    session_alias=sessionID.getTargetCompID().getValue(),
                    run_generation=self.identifier_scope.generation,
                )
                rejection = create_cancel_reject(
                    invalid_command,
                    reason=CancelRejectReason.INVALID_REQUEST,
                    text=error_text,
                    timestamp=self.engine.scheduler.current_time,
                )
                self.metrics.record_cancel_reject(rejection)
                self.process_cancel_reject(rejection)

            self._schedule_lifecycle_action(
                sessionID,
                fix.MsgType_OrderCancelRequest,
                token
                or IngressToken(
                    LifecycleState.ACTIVE,
                    self.identifier_scope.generation,
                ),
                _reject_parse_error,
            )

    def process_cancel_envelope(self, envelope: FixIngressEnvelope) -> None:
        """Start the staged cancel lifecycle from a sorted receive event."""
        if envelope.command_type != FixCommandType.CANCEL_ORDER:
            raise TypeError("Expected a CANCEL_ORDER ingress envelope")
        duplicate = not self.session_registry.claim_cl_ord_id(
            envelope.session.session_id,
            envelope.cl_ord_id,
        )
        command = CancelOrderCommand(
            cancel_cl_ord_id=envelope.cl_ord_id,
            orig_cl_ord_id=envelope.orig_cl_ord_id or "",
            symbol=envelope.symbol,
            side=envelope.side,
            qty=envelope.qty,
            requested_at=envelope.simulated_receive_timestamp,
            session_id=envelope.session.session_id,
            session_alias=envelope.session.alias,
            run_generation=envelope.run_generation,
            ingress_sequence=envelope.ingress_sequence,
            msg_seq_num=envelope.msg_seq_num,
            duplicate_request=duplicate,
        )
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

    def handle_market_data_request(
        self,
        message,
        sessionID,
        token: Optional[IngressToken] = None,
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
        )
        
    def _send_to_quickfix(
        self,
        transport_session: object,
        report: RoutableReport,
    ) -> None:
        """Serialize and send through QuickFIX at the adapter boundary."""
        if fix is None:
            raise RuntimeError("QuickFIX is unavailable")
        if isinstance(report, ExecutionReport):
            self._send_quickfix_execution_report(
                transport_session,
                report,
            )
        else:
            self._send_quickfix_cancel_reject(
                transport_session,
                report,
            )

    def _send_quickfix_execution_report(
        self,
        transport_session: object,
        report: ExecutionReport,
    ) -> None:
        """Serialize one immutable execution report and send it."""
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
        msg.setField(fix.LeavesQty(float(fields["LeavesQty"])))
        msg.setField(fix.CumQty(float(fields["CumQty"])))
        msg.setField(fix.AvgPx(float(fields["AvgPx"])))
        msg.setField(fix.OrderQty(float(fields["OrderQty"])))
        msg.setField(fix.TimeInForce(str(fields["TimeInForce"])))

        # Optional fields
        if "OrigClOrdID" in fields:
            msg.setField(fix.OrigClOrdID(str(fields["OrigClOrdID"])))
        if "Price" in fields:
            msg.setField(fix.Price(float(fields["Price"])))
        if "LastPx" in fields:
            msg.setField(fix.LastPx(float(fields["LastPx"])))
        if "LastQty" in fields:
            msg.setField(fix.LastQty(float(fields["LastQty"])))
        if "TradeID" in fields:
            msg.setField(fix.StringField(1003, str(fields["TradeID"])))
        if "Text" in fields:
            msg.setField(fix.Text(str(fields["Text"])))

        serialize_end_ns = self.monotonic_clock.now_ns()
        try:
            duration_ns = monotonic_duration_ns(
                serialize_start_ns,
                serialize_end_ns,
                context=f"execution report {report.exec_id} serialization",
            )
        except ValueError:
            self.metrics.record_invalid_latency(
                metric="mockx_real_report_serialization_duration_seconds",
                order_id=report.order_id,
                session_id=report.session_id,
            )
        else:
            try:
                self.latency_tracker.record_real_report_serialization_duration(
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

        fix.Session.sendToTarget(msg, transport_session)

    def _send_quickfix_cancel_reject(
        self,
        transport_session: object,
        cancel_reject: CancelReject,
    ) -> None:
        """Serialize and send one immutable OrderCancelReject."""
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

        fix.Session.sendToTarget(reject, transport_session)

    def send_market_data_reject(self, sessionID, md_req_id: str, text: str):
        """Send MarketDataRequestReject (35=Y) message."""
        reject = fix.Message()
        reject.getHeader().setField(fix.MsgType(fix.MsgType_MarketDataRequestReject))
        
        reject.setField(fix.MDReqID(md_req_id))
        reject.setField(fix.MDReqRejReason('0'))  # Unknown symbol
        reject.setField(fix.Text(text))
        
        fix.Session.sendToTarget(reject, sessionID)
        
    def send_reject(
        self,
        sessionID,
        text: str,
        *,
        ref_msg_type: Optional[str] = None,
    ):
        """Send generic Reject (35=3) message."""
        reject = fix.Message()
        reject.getHeader().setField(fix.MsgType(fix.MsgType_Reject))
        if ref_msg_type is not None:
            reject.setField(fix.RefMsgType(ref_msg_type))
        reject.setField(fix.Text(text))
        
        fix.Session.sendToTarget(reject, sessionID)
        
    def process_execution_report(self, report: ExecutionReport) -> bool:
        """Deliver strictly to the immutable report destination."""
        delivered = self._deliver_report(report)
        if delivered and report.cancel_ingress_sequence is None:
            try:
                self.latency_tracker.report_delivered(
                    report,
                    self.engine.scheduler.current_time,
                )
            except Exception:
                logger.exception(
                    "Report delivery latency observer failed",
                    extra={
                        "order_id": report.order_id,
                        "session_id": report.session_id,
                        "report_type": type(report).__name__,
                    },
                )
        return delivered

    def process_cancel_reject(self, cancel_reject: CancelReject) -> bool:
        """Deliver a cancel reject strictly to its immutable destination."""
        return self._deliver_report(cancel_reject)

    def send_execution_report(self, report: ExecutionReport) -> bool:
        """Compatibility alias for immutable execution-report delivery."""
        return self.process_execution_report(report)

    def send_cancel_reject(self, cancel_reject: CancelReject) -> bool:
        """Compatibility alias for immutable cancel-reject delivery."""
        return self.process_cancel_reject(cancel_reject)

    def _deliver_report(self, report: RoutableReport) -> bool:
        session_id = report_destination_id(report)
        transport = (
            self.session_registry.resolve_transport(session_id)
            if session_id
            else None
        )
        if transport is None:
            self.metrics.record_report_delivery_failure(report)
            logger.warning(
                "FIX report destination is unknown or disconnected",
                extra={
                    "order_id": report.order_id,
                    "session_id": session_id,
                    "cl_ord_id": (
                        report.cl_ord_id
                        if isinstance(report, ExecutionReport)
                        else report.cancel_cl_ord_id
                    ),
                    "report_type": type(report).__name__,
                },
            )
            return False
        try:
            self.transport_sender(transport, report)
        except Exception:
            self.metrics.record_report_delivery_failure(report)
            logger.exception(
                "FIX report delivery failed",
                extra={
                    "order_id": report.order_id,
                    "session_id": session_id,
                    "cl_ord_id": (
                        report.cl_ord_id
                        if isinstance(report, ExecutionReport)
                        else report.cancel_cl_ord_id
                    ),
                    "report_type": type(report).__name__,
                },
            )
            return False
        return True
            
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
    def _session_identity(sessionID) -> SessionIdentity:
        """Convert a live QuickFIX SessionID to a domain-safe identity."""
        return SessionIdentity(
            session_id=sessionID.toString(),
            sender_comp_id=sessionID.getSenderCompID().getValue(),
            target_comp_id=sessionID.getTargetCompID().getValue(),
            alias=sessionID.getTargetCompID().getValue(),
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
    ):
        self.application = application or FIXApplication(
            engine,
            metrics,
            latency_tracker,
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
            # self.accept_port = self.get_session_setting("SocketAcceptPort")
        
            # Create store and log factories
            self.fix_store_factory = fix.FileStoreFactory(self.fix_settings)
            self.fix_log_factory = fix.FileLogFactory(self.fix_settings)

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

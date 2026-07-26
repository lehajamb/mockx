"""FIX protocol gateway for the mock exchange."""

try:
    import quickfix as fix
except ModuleNotFoundError:  # Pure report mapping and non-FIX tests remain importable.
    fix = None
import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Union
import math
import threading

from .models import (
    CancelReject,
    CancelRejectReason,
    CancelOrderCommand,
    ClientOrderKey,
    ExecutionReport,
    ExecutionType,
    InvalidOrderTransition,
    NewOrderCommand,
    OrderStatus,
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

    def register(
        self,
        identity: SessionIdentity,
        transport_session: object,
    ) -> None:
        with self._lock:
            replaced = identity.session_id in self._transports
            self._identities[identity.session_id] = identity
            self._transports[identity.session_id] = transport_session
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
    ):
        super().__init__()
        self.engine = engine
        self.metrics = metrics
        self.latency_tracker = latency_tracker
        self.session_registry = session_registry or SessionRegistry()
        self.transport_sender = transport_sender or self._send_to_quickfix
        self.monotonic_clock = monotonic_clock or PerfCounterClock()
        # ClOrdID is scoped by FIX session; it is not globally unique.
        self.order_map: Dict[ClientOrderKey, int] = {}
        self._next_gateway_exec_id = 1
        self.loop = None  # <-- Add this line to store the event loop

    def reset(self):
        """Resets the FIX application's session state."""
        logger.info("Resetting FIX application state.")
        self.order_map.clear()
        self._next_gateway_exec_id = 1
        # Connected transports persist; only per-run order keys are reset.

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
        self.metrics.record_fix_message(msg_type, 'received', self.engine.scheduler.current_time_ns())
        
    def toApp(self, message, sessionID):
        """Called for outgoing application messages."""
        msg_type = message.getHeader().getField(fix.MsgType())
        logger.debug(f"App message to {sessionID}: {msg_type}")
        self.metrics.record_fix_message(msg_type, 'sent', self.engine.scheduler.current_time_ns())
        
    def fromApp(self, message, sessionID):
        """Called for incoming application messages."""
        msg_type_field = fix.MsgType()
        message.getHeader().getField(msg_type_field)
        msg_type = msg_type_field.getValue()
        
        logger.debug(f"App message from {sessionID}: {msg_type}")
        self.metrics.record_fix_message(msg_type, 'received', self.engine.scheduler.current_time_ns())
        
        try:
            if msg_type == fix.MsgType_NewOrderSingle:
                self.handle_new_order_single(message, sessionID)
            elif msg_type == fix.MsgType_OrderCancelRequest:
                self.handle_order_cancel_request(message, sessionID)
            elif msg_type == fix.MsgType_MarketDataRequest:
                self.handle_market_data_request(message, sessionID)
            else:
                logger.warning(f"Unsupported message type: {msg_type}")
                self.send_reject(sessionID, f"Unsupported message type: {msg_type}")
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            self.send_reject(sessionID, str(e))
            
    def handle_new_order_single(self, message, sessionID):
        """Handle NewOrderSingle (35=D) message."""
        try:
            # Extract order fields
            cl_ord_id = self._get_field(message, fix.ClOrdID())
            symbol = self._get_field(message, fix.Symbol())
            side = self._get_field(message, fix.Side())
            order_qty = self._get_field(message, fix.OrderQty())
            ord_type = self._get_field(message, fix.OrdType())
            session_id = sessionID.toString()
            session_alias = sessionID.getTargetCompID().getValue()
            
            # Convert side
            if side == fix.Side_BUY:
                order_side = Side.BUY
            elif side == fix.Side_SELL:
                order_side = Side.SELL
            else:
                raise ValueError(f"Invalid side: {side}")
                
            # Extract price for limit orders
            price = None
            if ord_type == fix.OrdType_LIMIT:
                price = float(self._get_field(message, fix.Price()))
            elif ord_type != fix.OrdType_MARKET:
                raise ValueError(f"Unsupported order type: {ord_type}")
                
            # Extract time in force
            tif = TimeInForce.GTC  # Default
            if message.isSetField(fix.TimeInForce()):
                tif_value = self._get_field(message, fix.TimeInForce())
                if tif_value == fix.TimeInForce_IMMEDIATE_OR_CANCEL:
                    tif = TimeInForce.IOC
                elif tif_value != fix.TimeInForce_GOOD_TILL_CANCEL:
                    raise ValueError(f"Unsupported TimeInForce: {tif_value}")

            command = NewOrderCommand(
                cl_ord_id=cl_ord_id,
                symbol=symbol,
                side=order_side,
                qty=float(order_qty),
                price=price,
                tif=tif,
                submitted_at=self.engine.scheduler.current_time,
                session_id=session_id,
                session_alias=session_alias,
            )

            # Submit to engine on the event loop thread
            def _submit():
                try:
                    order_id = self.engine.submit_order(command)
                    self.order_map[
                        ClientOrderKey(session_id, cl_ord_id)
                    ] = order_id
                    logger.debug(
                        "Order submitted: %s -> %s",
                        cl_ord_id,
                        order_id,
                    )
                except Exception as exc:
                    logger.error("Order submission rejected: %s", exc)
                    self._send_input_rejection(
                        sessionID,
                        cl_ord_id=command.cl_ord_id,
                        symbol=command.symbol,
                        side=command.side,
                        qty=command.qty,
                        price=command.price,
                        tif=command.tif,
                        session_id=command.session_id,
                        session_alias=command.session_alias,
                        text=str(exc),
                    )

            self.loop.call_soon_threadsafe(_submit)
            
        except Exception as e:
            logger.error(f"Error handling NewOrderSingle: {e}")

            # Capture the necessary variables that are in the local scope
            # to pass them to the thread-safe function.
            _cl_ord_id = cl_ord_id if 'cl_ord_id' in locals() else "UNKNOWN"
            _symbol = symbol if 'symbol' in locals() else "UNKNOWN"
            _side = order_side if 'order_side' in locals() else Side.BUY
            _qty = float(order_qty) if 'order_qty' in locals() else 0.0
            _price = price if 'price' in locals() else None
            _tif = tif if 'tif' in locals() else TimeInForce.GTC
            _session_id = session_id if 'session_id' in locals() else sessionID.toString()
            _session_alias = session_alias if 'session_alias' in locals() else "UNKNOWN"
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
                    session_id=_session_id,
                    session_alias=_session_alias,
                    text=_error_text,
                )

            self.loop.call_soon_threadsafe(_reject)

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
        exec_id = f"GATEWAY-REJECT-{self._next_gateway_exec_id}"
        self._next_gateway_exec_id += 1
        report = ExecutionReport(
            exec_id=exec_id,
            order_id=0,
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

    def handle_order_cancel_request(self, message, sessionID):
        """Handle OrderCancelRequest (35=F) message."""
        try:
            cl_ord_id = self._get_field(message, fix.ClOrdID())
            orig_cl_ord_id = self._get_field(message, fix.OrigClOrdID())
            session_id = sessionID.toString()
            session_alias = sessionID.getTargetCompID().getValue()

            def _cancel_logic():
                """This function will run on the main asyncio event loop."""
                try:
                    order_id = self.order_map.get(
                        ClientOrderKey(session_id, orig_cl_ord_id),
                        0,
                    )
                    order = self.engine.orders.get(order_id)
                    command = CancelOrderCommand(
                        order_id=order_id,
                        expected_version=order.version if order else 0,
                        cancel_cl_ord_id=cl_ord_id,
                        orig_cl_ord_id=orig_cl_ord_id,
                        requested_at=self.engine.scheduler.current_time,
                        session_id=session_id,
                        session_alias=session_alias,
                    )

                    rejection = self.engine.cancel_order(command)
                    if rejection:
                        self.process_cancel_reject(rejection)

                except InvalidOrderTransition:
                    logger.exception("Invalid order transition while cancelling %s", orig_cl_ord_id)
                    raise
                except Exception as exc:
                    logger.error(f"Error handling OrderCancelRequest: {exc}")
                    order_id_for_reject = self.order_map.get(
                        ClientOrderKey(session_id, orig_cl_ord_id)
                    )
                    order_for_reject = self.engine.orders.get(
                        order_id_for_reject or 0
                    )
                    command = CancelOrderCommand(
                        order_id=order_id_for_reject or 0,
                        expected_version=(
                            order_for_reject.version
                            if order_for_reject
                            else 0
                        ),
                        cancel_cl_ord_id=cl_ord_id,
                        orig_cl_ord_id=orig_cl_ord_id,
                        requested_at=self.engine.scheduler.current_time,
                        session_id=session_id,
                        session_alias=session_alias,
                    )
                    rejection = (
                        create_cancel_reject(
                            command,
                            reason=CancelRejectReason.INVALID_REQUEST,
                            text=str(exc),
                            timestamp=self.engine.scheduler.current_time,
                            order=order_for_reject,
                        )
                    )
                    self.metrics.record_cancel_reject(rejection)
                    self.process_cancel_reject(rejection)

            # Schedule the cancellation logic to run on the event loop
            self.loop.call_soon_threadsafe(_cancel_logic)
            
        except Exception as e:
            # This outer try-except now only catches errors in parsing the FIX message itself
            logger.error(f"Error parsing OrderCancelRequest: {e}")
            invalid_command = CancelOrderCommand(
                order_id=0,
                expected_version=0,
                cancel_cl_ord_id=(
                    cl_ord_id if 'cl_ord_id' in locals() else "UNKNOWN"
                ),
                orig_cl_ord_id=(
                    orig_cl_ord_id
                    if 'orig_cl_ord_id' in locals()
                    else "UNKNOWN"
                ),
                requested_at=self.engine.scheduler.current_time,
                session_id=sessionID.toString(),
                session_alias=sessionID.getTargetCompID().getValue(),
            )
            rejection = create_cancel_reject(
                    invalid_command,
                    reason=CancelRejectReason.INVALID_REQUEST,
                    text=str(e),
                    timestamp=self.engine.scheduler.current_time,
                )
            self.metrics.record_cancel_reject(rejection)
            self.process_cancel_reject(rejection)
            
    def handle_market_data_request(self, message, sessionID):
        """Handle MarketDataRequest (35=V) message."""
        # For now, send a simple reject as market data is handled via REST/WebSocket
        req_id = self._get_field(message, fix.MDReqID())
        self.send_market_data_reject(sessionID, req_id, "Market data available via REST API")
        
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
        
    def send_reject(self, sessionID, text: str):
        """Send generic Reject (35=3) message."""
        reject = fix.Message()
        reject.getHeader().setField(fix.MsgType(fix.MsgType_Reject))
        reject.setField(fix.Text(text))
        
        fix.Session.sendToTarget(reject, sessionID)
        
    def process_execution_report(self, report: ExecutionReport) -> bool:
        """Deliver strictly to the immutable report destination."""
        delivered = self._deliver_report(report)
        if delivered:
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
            
    def generate_exec_id(self) -> str:
        """Generate unique execution ID."""
        return f"EXEC{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        
    def generate_order_id(self) -> str:
        """Generate unique order ID."""
        return f"ORD{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"

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

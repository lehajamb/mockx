"""FIX protocol gateway for the mock exchange."""

try:
    import quickfix as fix
except ModuleNotFoundError:  # Pure report mapping and non-FIX tests remain importable.
    fix = None
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Optional
import json
import math
import time

from .models import (
    CancelReject,
    CancelRejectReason,
    CancelOrderCommand,
    ExecutionReport,
    ExecutionType,
    InvalidOrderTransition,
    NewOrderCommand,
    OrderStatus,
    Side,
    TimeInForce,
    create_cancel_reject,
)
from .fix_fields import cancel_reject_fields, execution_report_fields
from .metrics import MetricsCollector, LatencyTracker
from .engine import MatchingEngine

logger = logging.getLogger(__name__)

_FIX_APPLICATION_BASE = fix.Application if fix is not None else object


class FIXApplication(_FIX_APPLICATION_BASE):
    """QuickFIX application for handling FIX protocol messages.

    The matching engine and scheduler live on the asyncio event loop
    running in the main thread.  Callback methods invoked by QuickFIX may
    run on background threads, so any interaction with the engine or
    scheduler must be marshalled back to the main thread using
    ``self.loop.call_soon_threadsafe`` where ``self.loop has been set to the
    asyncio event loop running in the main thread.
    """

    def __init__(self, engine: MatchingEngine, metrics: MetricsCollector, latency_tracker: LatencyTracker):
        super().__init__()
        self.engine = engine
        self.metrics = metrics
        self.latency_tracker = latency_tracker

        self.sessions = {}
        # ClOrdID is scoped by FIX session; it is not globally unique.
        self.order_map: Dict[tuple[str, str], int] = {}
        self._next_gateway_exec_id = 1
        self.loop = None  # <-- Add this line to store the event loop

    def reset(self):
        """Resets the FIX application's session state."""
        logger.info("Resetting FIX application state.")
        self.order_map.clear()
        self._next_gateway_exec_id = 1
        # Note: We do not clear self.sessions as the FIX sessions themselves persist.
        # We only clear the mapping of orders to those sessions.

    def onCreate(self, sessionID):
        """Called when a new session is created."""
        logger.info(f"FIX session created: {sessionID}")
        self.sessions[sessionID.toString()] = sessionID
        
    def onLogon(self, sessionID):
        """Called when a session logs on."""
        logger.info(f"FIX session logon: {sessionID}")
        self.metrics.record_fix_connection(1, self.engine.scheduler.current_time_ns())
        
    def onLogout(self, sessionID):
        """Called when a session logs out."""
        logger.info(f"FIX session logout: {sessionID}")
        self.metrics.record_fix_connection(-1, self.engine.scheduler.current_time_ns())
        
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
                    self.latency_tracker.order_submitted(order_id)
                    self.order_map[(session_id, cl_ord_id)] = order_id
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
        self.send_execution_report(sessionID, report)

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
                    order_id = self.order_map.get((session_id, orig_cl_ord_id), 0)
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
                        self.send_cancel_reject(sessionID, rejection)

                except InvalidOrderTransition:
                    logger.exception("Invalid order transition while cancelling %s", orig_cl_ord_id)
                    raise
                except Exception as exc:
                    logger.error(f"Error handling OrderCancelRequest: {exc}")
                    order_id_for_reject = self.order_map.get((session_id, orig_cl_ord_id))
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
                    self.send_cancel_reject(
                        sessionID,
                        create_cancel_reject(
                            command,
                            reason=CancelRejectReason.INVALID_REQUEST,
                            text=str(exc),
                            timestamp=self.engine.scheduler.current_time,
                            order=order_for_reject,
                        ),
                    )

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
            self.send_cancel_reject(
                sessionID,
                create_cancel_reject(
                    invalid_command,
                    reason=CancelRejectReason.INVALID_REQUEST,
                    text=str(e),
                    timestamp=self.engine.scheduler.current_time,
                ),
            )
            
    def handle_market_data_request(self, message, sessionID):
        """Handle MarketDataRequest (35=V) message."""
        # For now, send a simple reject as market data is handled via REST/WebSocket
        req_id = self._get_field(message, fix.MDReqID())
        self.send_market_data_reject(sessionID, req_id, "Market data available via REST API")
        
    def send_execution_report(self, sessionID, report: ExecutionReport):
        """Send ExecutionReport (35=8) message."""
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

        # Send message
        fix.Session.sendToTarget(msg, sessionID)

        # Record E2E latency now that the message is sent
        self.latency_tracker.record_e2e_latency(report.order_id)
        
    def send_cancel_reject(
        self,
        sessionID,
        cancel_reject: CancelReject,
    ):
        """Send OrderCancelReject (35=9) message."""
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
        
        fix.Session.sendToTarget(reject, sessionID)
        
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
        
    def process_execution_report(self, report: ExecutionReport):
        """Process execution report from engine and send via FIX."""
        session_obj = self.sessions.get(report.session_id) if report.session_id else None

        # Backward-compatible fallback for snapshots that predate session routing:
        # route only when ClOrdID identifies exactly one known FIX session.
        if session_obj is None and report.session_id is None:
            matching_session_ids = {
                session_id
                for session_id, cl_ord_id in self.order_map
                if cl_ord_id == report.cl_ord_id
            }
            if len(matching_session_ids) == 1:
                session_obj = self.sessions.get(matching_session_ids.pop())

        if session_obj:
            self.send_execution_report(session_obj, report)
        else:
            logger.warning(
                "Session not found for order %s (session_id=%s)",
                report.cl_ord_id,
                report.session_id,
            )
            
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

class FIXGateway:
    """Manages FIX protocol connectivity."""
    
    def __init__(self, fix_cfg: str, engine, metrics: MetricsCollector, latency_tracker: LatencyTracker):
        #TODO: maybe pass FIXApplication as a parameter instead of creating it here
        self.application = FIXApplication(engine, metrics, latency_tracker)

        # FIX components
        self.fix_cfg = fix_cfg
        self.fix_acceptor = None
        self.fix_settings = None
        self.fix_store_factory = None
        self.fix_log_factory = None
        self.accept_port = None
        
    def start(self):
        """Start FIX acceptor."""
        if fix is None:
            raise RuntimeError(
                "QuickFIX is unavailable; install the native quickfix module to start the FIX gateway"
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

            # logger.info(f"FIX acceptor started on port {self.accept_port}")
            logger.info(f"FIX acceptor started on port 4001")
            
        except Exception as e:
            logger.error(f"Failed to start FIX gateway: {e}")
            raise
            
    def stop(self):
        """Stop FIX acceptor."""
        if self.fix_acceptor:
            self.fix_acceptor.stop()
            logger.info("FIX acceptor stopped")
            
    def process_execution_report(self, report: ExecutionReport):
        """Process execution report from scheduler."""
        self.application.process_execution_report(report)

    def get_session_setting(self, key: str) -> Optional[str]:
        """Get a specific setting value from the FIX session configuration."""
        sender = self.fix_settings.get().getString("SenderCompID")
        target = self.fix_settings.get().getString("TargetCompID")
        session_id = fix.SessionID("FIX.4.4", sender, target)
        session_dict = self.fix_settings.get(session_id)
        return session_dict.getString(key)

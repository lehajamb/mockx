"""FIX protocol gateway for the mock exchange."""

import quickfix as fix
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Optional
import json
import time

from .models import Order, Side, TimeInForce, OrderStatus, ExecutionReport
from .metrics import MetricsCollector, LatencyTracker
from .engine import MatchingEngine

logger = logging.getLogger(__name__)

exec_type_map = {
    "NEW": '0',        # NEW - correct
    "PARTIAL": 'F',    # TRADE - for partial fills
    "FILLED": 'F',     # TRADE - for complete fills
    "CANCELLED": '4',  # CANCELED - correct
    "REJECTED": '8',   # REJECTED - correct
}

ord_status_map = {
    OrderStatus.NEW: '0',
    OrderStatus.PARTIAL: '1',
    OrderStatus.FILLED: '2',
    OrderStatus.CANCELLED: '4',
    OrderStatus.REJECTED: '8',
}

side_map = {
    Side.BUY: fix.Side_BUY,
    Side.SELL: fix.Side_SELL,
}

class FIXApplication(fix.Application):
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
        # Map clOrdID to {'order_id': id, 'session_id': sessionStr}
        self.order_map: Dict[str, Dict[str, str]] = {}
        self.loop = None  # <-- Add this line to store the event loop

    def reset(self):
        """Resets the FIX application's session state."""
        logger.info("Resetting FIX application state.")
        self.order_map.clear()
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
                                
            # Submit to engine on the event loop thread
            def _submit():
                # Create order object
                order = Order(
                    id=0,  # Will be assigned by engine
                    cl_ord_id=cl_ord_id,
                    symbol=symbol,
                    status= OrderStatus.NEW,
                    side=order_side,
                    qty=float(order_qty),
                    price=price,
                    tif=tif,
                    ts_submitted=self.engine.scheduler.current_time,
                    session_alias=session_alias
                )

                order_id = self.engine.submit_order(order)
                self.latency_tracker.order_submitted(order_id)

                self.order_map[cl_ord_id] = {
                    "order_id": order_id,
                    "session_id": sessionID.toString(),
                }
                logger.debug(f"Order submitted: {cl_ord_id} -> {order_id}")

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
            _session_alias = session_alias if 'session_alias' in locals() else "UNKNOWN"
            _error_text = str(e)

            def _reject():
                simulated_time = self.engine.scheduler.current_time
                
                reject_order = Order(
                    id=0,
                    cl_ord_id=_cl_ord_id,
                    symbol=_symbol,
                    side=_side,
                    qty=_qty,
                    price=_price,
                    tif=TimeInForce.GTC,
                    ts_submitted=simulated_time,
                    status=OrderStatus.REJECTED,
                    session_alias=_session_alias
                )
                report = ExecutionReport(
                    order=reject_order,
                    exec_type="REJECTED",
                    text=_error_text,
                    timestamp=simulated_time,
                )
                self.send_execution_report(sessionID, report)
            
            self.loop.call_soon_threadsafe(_reject)
            
    def handle_order_cancel_request(self, message, sessionID):
        """Handle OrderCancelRequest (35=F) message."""
        try:
            cl_ord_id = self._get_field(message, fix.ClOrdID())
            orig_cl_ord_id = self._get_field(message, fix.OrigClOrdID())
            
            def _cancel_logic():
                """This function will run on the main asyncio event loop."""
                try:
                    # Find order
                    entry = self.order_map.get(orig_cl_ord_id)
                    if not entry:
                        raise ValueError(f"Order not found: {orig_cl_ord_id}")
                    
                    order_id = entry["order_id"]
                    
                    # The engine will generate an ExecutionReport on success or failure
                    self.engine.cancel_order(order_id)

                except Exception as exc:
                    logger.error(f"Error handling OrderCancelRequest: {exc}")
                    order_id_for_reject = self.order_map.get(orig_cl_ord_id, {}).get("order_id")
                    self.send_cancel_reject(sessionID, cl_ord_id, orig_cl_ord_id, order_id_for_reject, str(exc))

            # Schedule the cancellation logic to run on the event loop
            self.loop.call_soon_threadsafe(_cancel_logic)
            
        except Exception as e:
            # This outer try-except now only catches errors in parsing the FIX message itself
            logger.error(f"Error parsing OrderCancelRequest: {e}")
            self.send_cancel_reject(
                sessionID, 
                cl_ord_id if 'cl_ord_id' in locals() else "UNKNOWN",
                orig_cl_ord_id if 'orig_cl_ord_id' in locals() else "UNKNOWN",
                None, # We don't have the order_id at this stage
                str(e)
            )
            
    def handle_market_data_request(self, message, sessionID):
        """Handle MarketDataRequest (35=V) message."""
        # For now, send a simple reject as market data is handled via REST/WebSocket
        req_id = self._get_field(message, fix.MDReqID())
        self.send_market_data_reject(sessionID, req_id, "Market data available via REST API")
        
    def send_execution_report(self, sessionID, report: ExecutionReport):
        """Send ExecutionReport (35=8) message."""
        msg = fix.Message()
        msg.getHeader().setField(fix.MsgType(fix.MsgType_ExecutionReport))
        order_id = str(report.order.id) if report.order.id else self.generate_order_id()
        # LeavesQty, CumQty, AvgPx are mandatory
        cum_qty = report.cum_qty if report.cum_qty is not None else report.order.filled_qty
        leaves_qty = report.order.qty - cum_qty
        avg_px = report.avg_px if report.avg_px is not None else (report.order.avg_fill_price or 0)

        # Required fields
        msg.setField(fix.ClOrdID(report.order.cl_ord_id))
        msg.setField(fix.ExecType(exec_type_map.get(report.exec_type, '8')))
        msg.setField(fix.OrdStatus(ord_status_map.get(report.order.status, '8')))
        msg.setField(fix.ExecID(self.generate_exec_id()))
        msg.setField(fix.OrderID(order_id))
        msg.setField(fix.Symbol(report.order.symbol))
        msg.setField(fix.Side(side_map.get(report.order.side, fix.Side_BUY)))
        msg.setField(fix.LeavesQty(float(leaves_qty)))
        msg.setField(fix.CumQty(float(cum_qty)))
        msg.setField(fix.AvgPx(float(avg_px)))

        # Optional fields
        if report.order.price is not None:
            msg.setField(fix.Price(float(report.order.price)))
        if report.last_px is not None:
            msg.setField(fix.LastPx(float(report.last_px)))
        if report.last_qty is not None:
            msg.setField(fix.LastQty(float(report.last_qty)))
        if report.text:
            msg.setField(fix.Text(report.text))

        msg.setField(fix.OrderQty(float(report.order.qty)))

        # Send message
        fix.Session.sendToTarget(msg, sessionID)
        
        # Record E2E latency now that the message is sent
        self.latency_tracker.record_e2e_latency(report.order.id)
        
    def send_cancel_reject(self, sessionID, cl_ord_id: str, orig_cl_ord_id: str, order_id: Optional[str], text: str):
        """Send OrderCancelReject (35=9) message."""
        reject = fix.Message()
        reject.getHeader().setField(fix.MsgType(fix.MsgType_OrderCancelReject))
        
        # Required fields
        reject.setField(fix.ClOrdID(cl_ord_id))
        reject.setField(fix.OrigClOrdID(orig_cl_ord_id))
        reject.setField(fix.OrdStatus('8'))  # Rejected
        
        # The OrderID is conditionally required. If the cancel request is rejected and the order exists, 
        # it should be included.
        if order_id:
            reject.setField(fix.OrderID(order_id))

        # Optional fields
        reject.setField(fix.CxlRejReason(99))  # Other
        reject.setField(fix.Text(f"Cancel failed: {text}"))
        
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
        # Find session for this order
        cl_ord_id = report.order.cl_ord_id
        
        entry = self.order_map.get(cl_ord_id)
        session_obj = None
        if entry:
            session_str = entry.get("session_id")
            if session_str:
                session_obj = self.sessions.get(session_str)

        if session_obj:
            self.send_execution_report(session_obj, report)
        else:
            logger.warning(f"Session not found for order {cl_ord_id}")
            
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

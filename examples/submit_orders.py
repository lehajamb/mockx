#!/usr/bin/env python3
"""Example script for submitting orders via FIX protocol."""

import quickfix as fix
import time
import argparse
from datetime import datetime


class FIXClient(fix.Application):
    """Simple FIX client for submitting orders."""
    
    def __init__(self):
        super().__init__()
        self.session_id = None
        
    def onCreate(self, sessionID):
        print(f"Session created: {sessionID}")
        
    def onLogon(self, sessionID):
        print(f"Logged on: {sessionID}")
        self.session_id = sessionID
        
    def onLogout(self, sessionID):
        print(f"Logged out: {sessionID}")
        
    def toAdmin(self, message, sessionID):
        pass
        
    def fromAdmin(self, message, sessionID):
        pass
        
    def toApp(self, message, sessionID):
        msg_type_field = fix.MsgType()
        message.getHeader().getField(msg_type_field)
        msg_type = msg_type_field.getValue()
        print(f"Sent: {msg_type}")
    
    def fromApp(self, message, sessionID):
        msg_type_field = fix.MsgType()
        message.getHeader().getField(msg_type_field)
        msg_type = msg_type_field.getValue()
        print(f"Received: {msg_type}")
        
        if msg_type == fix.MsgType_ExecutionReport:
            # Parse execution report - fix field extraction
            cl_ord_id_field = fix.ClOrdID()
            message.getField(cl_ord_id_field)
            cl_ord_id = cl_ord_id_field.getValue()
            
            exec_type_field = fix.ExecType()
            message.getField(exec_type_field)
            exec_type = exec_type_field.getValue()
            
            ord_status_field = fix.OrdStatus()
            message.getField(ord_status_field)
            ord_status = ord_status_field.getValue()
            
            status_map = {
                '0': 'NEW',
                '1': 'PARTIAL',
                '2': 'FILLED',
                '4': 'CANCELLED',
                '8': 'REJECTED'
            }
            
            print(f"Order {cl_ord_id}: {status_map.get(ord_status, ord_status)}")
            
            if message.isSetField(fix.LastPx()):
                last_px_field = fix.LastPx()
                message.getField(last_px_field)
                last_px = last_px_field.getValue()
                
                last_qty_field = fix.LastQty()
                message.getField(last_qty_field)
                last_qty = last_qty_field.getValue()
                
                print(f"  Executed: {last_qty} @ {last_px}")
                
            if message.isSetField(fix.Text()):
                text_field = fix.Text()
                message.getField(text_field)
                text = text_field.getValue()
                print(f"  Text: {text}")

    def send_order(self, symbol, side, qty, price=None, order_type=fix.OrdType_LIMIT):
        """Send a new order."""
        if not self.session_id:
            print("Not logged in!")
            return
            
        message = fix.Message()
        header = message.getHeader()
        
        header.setField(fix.MsgType(fix.MsgType_NewOrderSingle))
        
        # Order fields
        message.setField(fix.ClOrdID(f"ORD{datetime.now().strftime('%Y%m%d%H%M%S%f')}"))
        message.setField(fix.Symbol(symbol))
        message.setField(fix.Side(side))
        message.setField(fix.OrderQty(qty))
        message.setField(fix.OrdType(order_type))
        
        if price and order_type == fix.OrdType_LIMIT:
            message.setField(fix.Price(price))
            
        message.setField(fix.TimeInForce(fix.TimeInForce_GOOD_TILL_CANCEL))
        message.setField(fix.TransactTime())
        
        fix.Session.sendToTarget(message, self.session_id)


def main():
    parser = argparse.ArgumentParser(description='Submit orders to MockX exchange')
    parser.add_argument('--fix-config', default='examples/fix_client.cfg', help='FIX client configuration file')
    args = parser.parse_args()
    
    # Create FIX settings
    settings = fix.SessionSettings(args.fix_config)
            
    # Create and start initiator
    app = FIXClient()
    store_factory = fix.FileStoreFactory(settings)
    log_factory = fix.FileLogFactory(settings)
    
    initiator = fix.SocketInitiator(app, store_factory, settings, log_factory)
    initiator.start()
    
    # Wait for logon
    print("Connecting to exchange...")
    time.sleep(2)
    
    try:
        for i in range(10):

            # Submit some test orders
            print("\nSubmitting orders...")
            
            # Add liquidity
            app.send_order("BTC-USDT", fix.Side_SELL, 10, 50000)
            time.sleep(0.5)
            app.send_order("BTC-USDT", fix.Side_SELL, 5, 49900)
            time.sleep(0.5)
            app.send_order("BTC-USDT", fix.Side_BUY, 8, 49000)
            time.sleep(0.5)
            app.send_order("BTC-USDT", fix.Side_BUY, 3, 49100)
            time.sleep(0.5)
            
            # Cross the spread
            print("\nCrossing the spread...")
            app.send_order("BTC-USDT", fix.Side_BUY, 2, 49900)
            time.sleep(0.5)
            
            # Market order
            print("\nSending market order...")
            app.send_order("BTC-USDT", fix.Side_BUY, 1, order_type=fix.OrdType_MARKET)
            time.sleep(0.5)
            
            # Wait for responses
            print("\nWaiting for execution reports...")
            time.sleep(2)
        
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        initiator.stop()
        

if __name__ == "__main__":
    main()

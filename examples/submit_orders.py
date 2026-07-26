#!/usr/bin/env python3
"""Submit a small, bounded FIX workflow to a running MockX instance."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys
import threading

try:
    import quickfix as fix
except ImportError:  # Keep ``--help`` useful without the optional native wheel.
    fix = None


ApplicationBase = fix.Application if fix is not None else object


class FIXClient(ApplicationBase):
    """Small client that waits for logon and prints every execution report."""

    def __init__(self) -> None:
        super().__init__()
        self.session_id = None
        self.logged_on = threading.Event()
        self.report_received = threading.Event()
        self._next_order_id = 1

    def onCreate(self, sessionID):
        print(f"Session created: {sessionID}")

    def onLogon(self, sessionID):
        print(f"Logged on: {sessionID}")
        self.session_id = sessionID
        self.logged_on.set()

    def onLogout(self, sessionID):
        print(f"Logged out: {sessionID}")
        self.session_id = None
        self.logged_on.clear()

    def toAdmin(self, message, sessionID):
        return None

    def fromAdmin(self, message, sessionID):
        return None

    def toApp(self, message, sessionID):
        msg_type = fix.MsgType()
        message.getHeader().getField(msg_type)
        print(f"Sent FIX {msg_type.getValue()}")

    def fromApp(self, message, sessionID):
        msg_type = fix.MsgType()
        message.getHeader().getField(msg_type)
        if msg_type.getValue() != fix.MsgType_ExecutionReport:
            print(f"Received FIX {msg_type.getValue()}")
            return

        cl_ord_id = fix.ClOrdID()
        exec_type = fix.ExecType()
        ord_status = fix.OrdStatus()
        message.getField(cl_ord_id)
        message.getField(exec_type)
        message.getField(ord_status)
        status_map = {
            "0": "NEW",
            "1": "PARTIAL",
            "2": "FILLED",
            "4": "CANCELLED",
            "8": "REJECTED",
        }
        print(
            f"ExecutionReport {cl_ord_id.getValue()}: "
            f"exec_type={exec_type.getValue()} "
            f"status={status_map.get(ord_status.getValue(), ord_status.getValue())}"
        )
        if message.isSetField(fix.LastPx().getField()):
            last_px = fix.LastPx()
            last_qty = fix.LastQty()
            message.getField(last_px)
            message.getField(last_qty)
            print(f"  fill={last_qty.getValue()} @ {last_px.getValue()}")
        if message.isSetField(fix.Text().getField()):
            text = fix.Text()
            message.getField(text)
            print(f"  text={text.getValue()}")
        self.report_received.set()

    def send_order(
        self,
        symbol: str,
        side,
        qty: float,
        price: float | None,
    ) -> str:
        if self.session_id is None:
            raise RuntimeError("FIX session is not logged on")

        cl_ord_id = f"EXAMPLE-{self._next_order_id:04d}"
        self._next_order_id += 1
        message = fix.Message()
        message.getHeader().setField(
            fix.MsgType(fix.MsgType_NewOrderSingle)
        )
        message.setField(fix.ClOrdID(cl_ord_id))
        message.setField(fix.Symbol(symbol))
        message.setField(fix.Side(side))
        message.setField(fix.OrderQty(qty))
        if price is None:
            message.setField(fix.OrdType(fix.OrdType_MARKET))
        else:
            message.setField(fix.OrdType(fix.OrdType_LIMIT))
            message.setField(fix.Price(price))
        message.setField(
            fix.TimeInForce(fix.TimeInForce_GOOD_TILL_CANCEL)
        )
        message.setField(
            fix.TransactTime(datetime.now(timezone.utc).replace(tzinfo=None))
        )
        fix.Session.sendToTarget(message, self.session_id)
        print(
            f"Submitted {cl_ord_id}: {side} {qty} "
            f"{'MARKET' if price is None else f'@ {price}'}"
        )
        return cl_ord_id


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Submit orders using a QuickFIX client configuration. In the "
            "default external clock mode, advance MockX separately via "
            "POST /admin/advance-time."
        )
    )
    parser.add_argument(
        "--fix-config",
        default="examples/fix_client.cfg",
        help="FIX client configuration file",
    )
    parser.add_argument(
        "--symbol",
        default="BTC-USDT",
        help="Configured symbol to trade (default: BTC-USDT)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for logon and then for a report",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if fix is None:
        print(
            "quickfix is not installed; install requirements.txt first",
            file=sys.stderr,
        )
        return 2
    if args.timeout <= 0:
        print("--timeout must be positive", file=sys.stderr)
        return 2

    settings = fix.SessionSettings(args.fix_config)
    app = FIXClient()
    store_factory = fix.FileStoreFactory(settings)
    log_factory = fix.FileLogFactory(settings)
    initiator = fix.SocketInitiator(
        app,
        store_factory,
        settings,
        log_factory,
    )

    try:
        initiator.start()
        print("Connecting to exchange...")
        if not app.logged_on.wait(args.timeout):
            print("FIX logon timed out", file=sys.stderr)
            return 1

        # One passive order, one crossing order, and one non-crossing order.
        app.send_order(args.symbol, fix.Side_SELL, 5.0, 100.0)
        app.send_order(args.symbol, fix.Side_BUY, 2.0, 100.0)
        app.send_order(args.symbol, fix.Side_BUY, 1.0, 99.0)
        print(
            "Orders are queued. If MockX uses the external clock, run the "
            "documented /admin/advance-time command now."
        )
        if not app.report_received.wait(args.timeout):
            print(
                "No execution report arrived before timeout; verify readiness "
                "and advance simulated time",
                file=sys.stderr,
            )
            return 1
        return 0
    except (OSError, RuntimeError, fix.ConfigError) as exc:
        print(f"FIX client failed: {exc}", file=sys.stderr)
        return 1
    finally:
        initiator.stop()


if __name__ == "__main__":
    raise SystemExit(main())

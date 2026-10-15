"""Demonstrate bounded pipelining, NewOrderList, native replace and cancellation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.fix_support import (
    ListOrder, MultiSessionFIXApplication, ReportTracker, fix,
    require_quickfix, send_cancel, send_limit_order, send_order_list, send_replace,
)
from examples.live_market import DemoAdmin, _base_url
from src.mockx.configuration import load_config
from src.mockx.instruments import Instrument


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="examples/live_exchange.yml")
    parser.add_argument("--fix-config", default="examples/live_client.cfg")
    parser.add_argument("--base-url")
    parser.add_argument("--mode", choices=("list", "pipeline"), default="list")
    parser.add_argument("--orders", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--price", default="100.00")
    parser.add_argument("--quantity", default="0.010")
    parser.add_argument("--timeout", type=float, default=15)
    return parser.parse_args(argv)


def settle(admin, tracker, timeout):
    """Drive the external clock until every report arrives or the deadline passes."""
    deadline = time.monotonic() + timeout
    while not tracker.complete and time.monotonic() < deadline:
        admin.advance(25)
        tracker.wait(min(0.01, max(0, deadline-time.monotonic())))
    return tracker.complete


def run(argv=None) -> int:
    args = parse_args(argv)
    initiator = None
    admin = None
    started = False
    owned_run_id = None
    try:
        require_quickfix()
        if args.orders < 1 or not 1 <= args.batch_size <= 1000 or args.timeout <= 0:
            raise ValueError("positive order count/timeout and batch size 1..1000 required")
        config = load_config(args.config)
        if config.scheduler.mode != "external":
            raise ValueError("this deterministic example requires the external clock")
        symbol = args.symbol.upper()
        instrument = Instrument.from_config(symbol, config.instruments[symbol])
        price_ticks = instrument.price_to_ticks(args.price)
        qty_lots = instrument.quantity_to_lots(args.quantity)
        if price_ticks <= args.orders or qty_lots < 2:
            raise ValueError("price must allow the ladder and quantity must be at least two lots")
        if args.mode == "list" and args.batch_size > config.fix.max_orders_per_list:
            raise ValueError("batch size exceeds fix.max_orders_per_list")
        app = MultiSessionFIXApplication(print_reports=True)
        latest = {}
        app.add_listener(lambda report: latest.__setitem__(report.cl_ord_id, report))
        settings = fix.SessionSettings(args.fix_config)
        initiator = fix.SocketInitiator(
            app, fix.MemoryStoreFactory(), settings, fix.ScreenLogFactory(settings)
        )
        admin = DemoAdmin(_base_url(config, args.base_url), args.timeout)
        admin.wait_ready(args.timeout)
        initiator.start()
        if not app.wait_for_sessions(("liquidity_bot",), args.timeout):
            raise TimeoutError("FIX logon timed out")
        response = admin.start_session("fix-batch-demo")
        started = True
        owned_run_id = response['session_id']
        prefix = f"G{response['run_generation']}-BATCH"
        session = app.session("liquidity_bot")
        orders = tuple(
            ListOrder(
                f"{prefix}-{index}", symbol, "1",
                instrument.format_quantity(qty_lots),
                instrument.format_price(price_ticks-index),
            )
            for index in range(args.orders)
        )
        first_list_id = None
        for offset in range(0, len(orders), args.batch_size):
            members = orders[offset:offset+args.batch_size]
            tracker = ReportTracker({order.cl_ord_id: {"NEW", "REJECTED"} for order in members})
            app.add_listener(lambda report, tracker=tracker: tracker.record(report.cl_ord_id, report.status))
            if args.mode == "list":
                list_id = f"{prefix}-L{offset // args.batch_size}"
                first_list_id = first_list_id or list_id
                send_order_list(session, list_id=list_id, orders=members)
            else:
                for order in members:
                    send_limit_order(session, cl_ord_id=order.cl_ord_id, symbol=symbol,
                                     side=order.side, quantity=order.quantity, price=order.price)
            if not settle(admin, tracker, args.timeout):
                raise TimeoutError("member ExecutionReports timed out")
            if any(latest[order.cl_ord_id].status != "NEW" for order in members):
                raise RuntimeError("initialization member rejected; restart the simulation")
        book = admin.book(symbol)
        if sum(level['order_count'] for level in book['bids']) != len(orders):
            raise RuntimeError("initialized book does not contain every member")
        print(f"Accepted {len(orders)} orders using {args.mode}; book verified through REST")
        replacement = f"{prefix}-REPLACE"
        tracker = ReportTracker({replacement: {"NEW", "REJECTED"}})
        app.add_listener(lambda report, tracker=tracker: tracker.record(report.cl_ord_id, report.status))
        send_replace(session, cl_ord_id=replacement, orig_cl_ord_id=orders[0].cl_ord_id,
                     symbol=symbol, side="1", quantity=instrument.format_quantity(qty_lots-1),
                     price=orders[0].price, list_id=first_list_id)
        if not settle(admin, tracker, args.timeout) or latest[replacement].exec_type != "5":
            raise RuntimeError("native replacement did not receive ExecType=REPLACED")
        cancel_id = f"{prefix}-CANCEL"
        tracker = ReportTracker({cancel_id: {"CANCELLED"}})
        app.add_listener(lambda report, tracker=tracker: tracker.record(report.cl_ord_id, report.status))
        send_cancel(session, cl_ord_id=cancel_id, orig_cl_ord_id=replacement,
                    symbol=symbol, side="1", quantity=instrument.format_quantity(qty_lots-1),
                    list_id=first_list_id)
        if not settle(admin, tracker, args.timeout):
            raise TimeoutError("cancel did not settle")
        print("Native same-price reduction and cancellation completed")
        return 0
    except (httpx.HTTPError, KeyError, ValueError, RuntimeError, TimeoutError,
            getattr(fix, "ConfigError", RuntimeError)) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if started and admin is not None:
            try:
                admin.end_session_if_owned(owned_run_id)
            except Exception as exc:
                print(f"Failed to end simulation session: {exc}", file=sys.stderr)
        try:
            if initiator is not None:
                initiator.stop()
        finally:
            if admin is not None:
                admin.close()


if __name__ == "__main__":
    raise SystemExit(run())

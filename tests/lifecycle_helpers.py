from tests.exact_helpers import exact_engine, exact_metrics
from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""Shared deterministic fixtures for focused order-lifecycle tests."""

from datetime import datetime, timezone

from src.mockx.engine import MatchingEngine
from src.mockx.latency import LatencyScheduler
from src.mockx.models import (
    CancelOrderCommand,
    ExecutionParams,
    ExecutionReport,
    NewOrderCommand,
    OrderType,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)
from tests.exact_helpers import TEST_INSTRUMENTS


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def make_engine(
    *,
    network_latency_ms: int = 0,
    engine_latency_ms: int = 0,
):
    scheduler = LatencyScheduler()
    engine = exact_engine(
        ExecutionParams(
            network_latency_ms=network_latency_ms,
            engine_latency_ms=engine_latency_ms,
            reject_prob=0.0,
            rand_seed=42,
        ),
        scheduler,
        TEST_INSTRUMENTS,
    )
    reports = []
    delivered = []
    trades = []
    engine.on_execution_report = reports.append
    engine.on_trade = trades.append
    scheduler.register_handler("order_arrived", lambda command: None)
    scheduler.register_handler("order_process", engine.process_order)
    scheduler.register_handler("exec_report", delivered.append)
    return engine, reports, trades, delivered


def submit(
    engine: MatchingEngine,
    *,
    cl_ord_id: str,
    participant_id: str,
    side: Side,
    qty: int,
    price: int | None,
    tif: TimeInForce = TimeInForce.GTC,
    session_id: str | None = "FIX.4.4:CLIENT->MOCKX",
    session_alias: str | None = "CLIENT",
    process: bool = True,
) -> int:
    order_id = engine.submit_order(
        exact_command(
            cl_ord_id=cl_ord_id,
            symbol="TEST",
            side=side,
            order_type=(
                OrderType.MARKET if price is None else OrderType.LIMIT
            ),
            order_qty_lots=qty,
            price_ticks=price,
            tif=tif,
            submitted_at=NOW,
            session_id=session_id,
            session_alias=session_alias,
            participant_id=participant_id,
        )
    )
    if process:
        engine.process_order(
            ProcessOrderCommand(order_id, expected_version=0)
        )
    return order_id


def cancel(
    engine: MatchingEngine,
    order_id: int,
    *,
    cancel_cl_ord_id: str | None = None,
    orig_cl_ord_id: str | None = None,
    session_id: str | None = None,
    expected_version: int | None = None,
):
    order = engine.orders.get(order_id)
    result = engine.cancel_order(
        exact_cancel_command(
            cancel_cl_ord_id=(
                cancel_cl_ord_id
                or f"CXL-{orig_cl_ord_id or (order.cl_ord_id if order else 'UNKNOWN')}"
            ),
            orig_cl_ord_id=(
                orig_cl_ord_id
                if orig_cl_ord_id is not None
                else (order.cl_ord_id if order else "UNKNOWN")
            ),
            symbol=order.symbol if order else "TEST",
            side=order.side if order else Side.BUY,
            order_qty_lots=order.order_qty_lots if order else 1,
            requested_at=engine.scheduler.current_time,
            session_id=(
                session_id
                if session_id is not None
                else (
                    order.session_id
                    if order and order.session_id is not None
                    else "FIX.4.4:CLIENT->MOCKX"
                )
            ),
            session_alias=order.session_alias if order else None,
            participant_id=order.participant_id if order else None,
            run_generation=engine.identifier_scope.generation,
        )
    )
    return None if isinstance(result, ExecutionReport) else result

"""Shared deterministic fixtures for focused order-lifecycle tests."""

from datetime import datetime, timezone

from src.mockx.engine import MatchingEngine
from src.mockx.latency import LatencyScheduler
from src.mockx.models import (
    CancelOrderCommand,
    ExecutionParams,
    NewOrderCommand,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def make_engine(
    *,
    network_latency_ms: int = 0,
    engine_latency_ms: int = 0,
):
    scheduler = LatencyScheduler()
    engine = MatchingEngine(
        ExecutionParams(
            network_latency_ms=network_latency_ms,
            engine_latency_ms=engine_latency_ms,
            partial_fill_prob=0.0,
            reject_prob=0.0,
            rand_seed=42,
        ),
        scheduler,
    )
    engine.add_symbol("TEST")
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
    side: Side,
    qty: float,
    price: float | None,
    tif: TimeInForce = TimeInForce.GTC,
    session_id: str | None = "FIX.4.4:CLIENT->MOCKX",
    session_alias: str | None = "CLIENT",
    process: bool = True,
) -> int:
    order_id = engine.submit_order(
        NewOrderCommand(
            cl_ord_id=cl_ord_id,
            symbol="TEST",
            side=side,
            qty=qty,
            price=price,
            tif=tif,
            submitted_at=NOW,
            session_id=session_id,
            session_alias=session_alias,
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
    return engine.cancel_order(
        CancelOrderCommand(
            order_id=order_id,
            expected_version=(
                expected_version
                if expected_version is not None
                else (order.version if order else 0)
            ),
            cancel_cl_ord_id=(
                cancel_cl_ord_id
                or f"CXL-{orig_cl_ord_id or (order.cl_ord_id if order else 'UNKNOWN')}"
            ),
            orig_cl_ord_id=(
                orig_cl_ord_id
                if orig_cl_ord_id is not None
                else (order.cl_ord_id if order else "UNKNOWN")
            ),
            requested_at=engine.scheduler.current_time,
            session_id=(
                session_id
                if session_id is not None
                else (order.session_id if order else None)
            ),
            session_alias=order.session_alias if order else None,
        )
    )

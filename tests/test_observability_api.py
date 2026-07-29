from tests.exact_helpers import exact_engine, exact_metrics
from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""FastAPI stats and application-owned Prometheus registry tests."""

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.mockx.api import create_app
from src.mockx.configuration import validate_config
from src.mockx.engine import MatchingEngine
from src.mockx.gateway import FIXApplication
from src.mockx.latency import LatencyScheduler
from src.mockx.main import MockExchange
from src.mockx.metrics import LatencyTracker, MetricsCollector
from src.mockx.models import (
    CancelOrderCommand,
    ExecutionParams,
    NewOrderCommand,
    ProcessOrderCommand,
    Side,
    TimeInForce,
)


FIX_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "mockx1.cfg"


def config():
    return validate_config({
        "exchange_name": "Test-Exchange",
        "instruments": {
            "TEST": {
                "tick_size": "1",
                "lot_size": "1",
                "price_display_precision": 2,
                "quantity_display_precision": 3,
            }
        },
        "latency": {"network_ms": 0, "engine_ms": 0},
        "execution": {
            "reject_prob": 0.0,
            "random_seed": 42,
        },
        "admin_api": {"host": "127.0.0.1", "port": 8999},
        "fix": {"enabled": False, "session_participants": {}},
        "scheduler": {"mode": "external"},
        "websocket": {},
        "cors_origins": [],
        "prometheus": {"enabled": True},
    }, environ={})


def bare_app():
    scheduler = LatencyScheduler()
    engine = exact_engine(
        ExecutionParams(
            network_latency_ms=0,
            engine_latency_ms=0,
            reject_prob=0.0,
            rand_seed=42,
        ),
        scheduler,
    )
    metrics = exact_metrics("TEST")
    metrics.bind_engine(engine)
    fix_app = FIXApplication(
        engine,
        metrics,
        LatencyTracker(scheduler, metrics),
        transport_sender=lambda transport, report: None,
    )
    return create_app(engine, metrics, config(), fix_app), metrics


def submit(exchange, cl_ord_id, side, qty, price):
    order_id = exchange.engine.submit_order(
        exact_command(
            cl_ord_id=cl_ord_id,
            participant_id=cl_ord_id,
            symbol="TEST",
            side=side,
            order_qty_lots=qty,
            price_ticks=price,
            tif=TimeInForce.GTC,
            submitted_at=exchange.scheduler.current_time,
            session_id="FIX.4.4:MOCKX->CLIENT",
            session_alias="CLIENT",
        )
    )
    exchange.engine.process_order(ProcessOrderCommand(order_id, 0))
    return order_id


@pytest.mark.asyncio
async def test_stats_empty_state_is_stable_and_json_serializable():
    app, _ = bare_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["orders"]["submitted"] == 0
    assert data["orders"]["active"] == 0
    assert data["trades"] == {
        "count": 0,
        "quantity": "0",
        "notional": "0",
        "canceled_quantity": "0",
    }
    assert data["latency"]["average_match_loop_duration_us"] is None
    json.dumps(data)


@pytest.mark.asyncio
async def test_stats_populated_state_matches_authoritative_events():
    exchange = MockExchange(config(), str(FIX_CONFIG))
    submit(exchange, "PASSIVE", Side.SELL, 5, 100)
    submit(exchange, "TAKER", Side.BUY, 5, 100)
    cancel_id = submit(exchange, "CANCEL", Side.BUY, 3, 99)
    cancel_order = exchange.engine.orders[cancel_id]
    exchange.engine.cancel_order(
        exact_cancel_command(
            cancel_cl_ord_id="CXL-CANCEL",
            orig_cl_ord_id=cancel_order.cl_ord_id,
            symbol=cancel_order.symbol,
            side=cancel_order.side,
            order_qty_lots=cancel_order.order_qty_lots,
            requested_at=exchange.scheduler.current_time,
            session_id=(
                cancel_order.session_id or "FIX.4.4:MOCKX->CLIENT"
            ),
            session_alias=cancel_order.session_alias,
        )
    )
    exchange.engine.cancel_order(
        exact_cancel_command(
            cancel_cl_ord_id="CXL-UNKNOWN",
            orig_cl_ord_id="UNKNOWN",
            symbol="TEST",
            side=Side.BUY,
            order_qty_lots=1,
            requested_at=exchange.scheduler.current_time,
            session_id="FIX.4.4:MOCKX->CLIENT",
            session_alias="CLIENT",
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=exchange.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["orders"]["submitted"] == 3
    assert data["orders"]["filled"] == 2
    assert data["orders"]["canceled"] == 1
    assert data["orders"]["cancel_requests"] == 2
    assert data["orders"]["cancel_rejects"] == 1
    assert data["cancellations"] == {
        "accepted": 1,
        "unknown_order": 1,
        "already_terminal": 0,
        "wrong_owner": 0,
        "duplicate_request": 0,
        "stale_run_generation": 0,
    }
    assert data["orders"]["active"] == 0
    assert data["orders"]["resting"] == 0
    assert data["trades"]["count"] == 1
    assert data["trades"]["quantity"] == "5"
    assert data["trades"]["notional"] == "500"


@pytest.mark.asyncio
async def test_metrics_endpoint_has_expected_names_types_and_values():
    exchange = MockExchange(config(), str(FIX_CONFIG))
    submit(exchange, "PASSIVE", Side.SELL, 2, 100)
    submit(exchange, "TAKER", Side.BUY, 2, 100)

    async with AsyncClient(
        transport=ASGITransport(app=exchange.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "text/plain; version=0.0.4"
    )
    body = response.text
    assert "mockx_orders_submitted_total 2.0" in body
    assert "mockx_trades_total 1.0" in body
    assert "mockx_traded_quantity_total 2.0" in body
    assert "mockx_active_orders 0.0" in body
    assert "mockx_execution_report_delivery_failures_total" in body
    assert "mockx_match_loop_duration_us" in body
    assert "mockx_book_commit_duration_us" in body
    assert "mockx_engine_command_duration_us" in body
    assert "mockx_report_creation_duration_us" in body
    assert "mockx_serialization_duration_us" in body
    assert "mockx_sim_cancel_inbound_latency_ms" in body
    assert "mockx_sim_cancel_engine_latency_ms" in body
    assert "mockx_sim_cancel_outbound_latency_ms" in body
    assert "mockx_sim_cancel_end_to_end_latency_ms" in body
    assert "mockx_cancel_accepted_total" in body
    assert "mockx_cancel_unknown_order_total" in body
    assert "mockx_cancel_already_terminal_total" in body
    assert "mockx_cancel_wrong_owner_total" in body
    assert "mockx_cancel_duplicate_request_total" in body
    assert "mockx_cancel_stale_run_generation_total" in body


@pytest.mark.asyncio
async def test_multiple_application_instances_do_not_duplicate_collectors():
    app_one, _ = bare_app()
    app_two, _ = bare_app()

    async with AsyncClient(
        transport=ASGITransport(app=app_one),
        base_url="http://one",
    ) as first, AsyncClient(
        transport=ASGITransport(app=app_two),
        base_url="http://two",
    ) as second:
        first_response = await first.get("/metrics")
        second_response = await second.get("/metrics")

    assert first_response.status_code == 200
    assert second_response.status_code == 200


@pytest.mark.asyncio
async def test_observability_reads_do_not_change_counter_samples():
    app, metrics = bare_app()
    before = metrics.snapshot()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get("/stats")
        await client.get("/metrics")
        await client.get("/stats")
        await client.get("/metrics")

    after = metrics.snapshot()
    assert after.orders_submitted == before.orders_submitted
    assert after.execution_reports == before.execution_reports
    assert after.trades == before.trades
    assert after.report_delivery_failures == before.report_delivery_failures

"""API ownership and structured lifespan contract tests."""

import asyncio

from fastapi.testclient import TestClient

from src.mockx.api import ExchangeAPI

from .api_helpers import make_exchange


def test_mock_exchange_directly_owns_one_api_service():
    exchange = make_exchange()

    assert isinstance(exchange.api, ExchangeAPI)
    assert exchange.app is exchange.api.app
    assert exchange.app.state.exchange_api is exchange.api
    assert exchange.app.state.engine is exchange.engine
    assert exchange.app.state.scheduler is exchange.scheduler
    assert exchange.app.state.metrics is exchange.metrics


def test_startup_hands_off_loop_before_fix_acceptor_and_gates_readiness(
    monkeypatch,
):
    exchange = make_exchange(fix_enabled=True)
    events = []

    def fake_start():
        assert exchange.fix_app.loop is asyncio.get_running_loop()
        assert not exchange.app.state.ready
        assert exchange._callbacks_registered
        events.append("fix_start")
        exchange.fix_gateway.started = True

    def fake_stop():
        events.append("fix_stop")
        exchange.fix_gateway.started = False

    monkeypatch.setattr(exchange.fix_gateway, "start", fake_start)
    monkeypatch.setattr(exchange.fix_gateway, "stop", fake_stop)

    assert not exchange.app.state.ready
    with TestClient(exchange.app) as client:
        response = client.get("/readyz")
        assert response.status_code == 200
        assert exchange.app.state.ready
        assert events == ["fix_start"]

    assert events == ["fix_start", "fix_stop"]
    assert not exchange.app.state.ready


def test_shutdown_order_is_idempotent_and_flushes_after_websockets(
    monkeypatch,
):
    exchange = make_exchange()
    events = []

    async def close_websockets():
        assert not exchange.app.state.ready
        events.append("websockets")

    async def close_metrics():
        events.append("metrics")
        return True

    monkeypatch.setattr(exchange.api, "close_websockets", close_websockets)
    monkeypatch.setattr(exchange.metrics, "close", close_metrics)

    with TestClient(exchange.app):
        assert exchange.app.state.ready

    assert events == ["websockets", "metrics"]
    asyncio.run(exchange.stop_components())
    assert events == ["websockets", "metrics"]


def test_multiple_app_instances_have_no_registry_or_state_leakage():
    first = make_exchange()
    second = make_exchange()

    assert first.api is not second.api
    assert first.api.registry is not second.api.registry
    assert first.app.state is not second.app.state
    assert first.metrics.registry is not second.metrics.registry

    with TestClient(first.app) as first_client, TestClient(
        second.app
    ) as second_client:
        assert first_client.get("/metrics").status_code == 200
        assert second_client.get("/metrics").status_code == 200

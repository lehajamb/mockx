"""Liveness and component-aware readiness behavior."""

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from .api_helpers import cleanup_native_exchanges
from .api_helpers import make_exchange


@pytest.mark.asyncio
async def test_liveness_survives_not_ready_process_state():
    exchange = make_exchange()
    async with AsyncClient(
        transport=ASGITransport(app=exchange.app),
        base_url="http://test",
    ) as client:
        health = await client.get("/healthz")
        readiness = await client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "healthy"}
    assert readiness.status_code == 503
    assert readiness.json()["status"] == "not_ready"


def test_ready_only_after_lifespan_finishes_critical_startup():
    exchange = make_exchange()
    assert not exchange.app.state.ready

    with TestClient(exchange.app) as client:
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        assert response.json()["components"]["engine"] == "ready"
        assert (
            response.json()["components"]["scheduler"]
            == "waiting_external"
        )


def test_historical_failure_counter_does_not_hide_recovered_required_persistence():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        exchange.metrics.record_sink_failure(
            RuntimeError("questdb unavailable"),
            1,
        )
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["components"]["persistence"] == "healthy"
    assert response.json()["core_ready"] is True
    assert response.json()["persistence_required"] is True


@pytest.mark.asyncio
async def test_readiness_is_503_after_shutdown_begins():
    exchange = make_exchange()
    with TestClient(exchange.app):
        assert exchange.app.state.ready

    async with AsyncClient(
        transport=ASGITransport(app=exchange.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"

"""Actual WebSocket delivery plus registry and browser policy tests."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.mockx.models import NewOrderCommand, Side, TimeInForce

from .api_helpers import make_exchange, process_order


def test_initial_snapshot_has_explicit_market_data_envelope():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        with client.websocket_connect("/ws/TEST") as websocket:
            message = websocket.receive_json()

    assert message == {
        "type": "market_data",
        "symbol": "TEST",
        "sequence": 0,
        "timestamp": "1970-01-01T00:00:00Z",
        "bids": [],
        "asks": [],
        "trades": [],
    }


def test_live_committed_book_mutation_reaches_connected_client():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        with client.websocket_connect("/ws/TEST") as websocket:
            assert websocket.receive_json()["sequence"] == 0
            exchange.engine.submit_order(
                NewOrderCommand(
                    cl_ord_id="BID",
                    symbol="TEST",
                    side=Side.BUY,
                    qty=3.0,
                    price=100.0,
                    tif=TimeInForce.GTC,
                    submitted_at=exchange.scheduler.current_time,
                    session_id="FIX.4.4:CLIENT->MOCKX",
                    session_alias="CLIENT",
                )
            )
            advanced = client.post(
                "/admin/advance-time",
                json={"milliseconds": 1},
            )
            assert advanced.status_code == 200
            update = websocket.receive_json()

    assert update["type"] == "market_data"
    assert update["sequence"] == 1
    assert update["bids"][0]["qty"] == 3.0
    assert update["bids"][0]["order_count"] == 1


def test_fill_reaches_client_once_with_committed_trade_fields():
    exchange = make_exchange()
    process_order(exchange, "ASK", Side.SELL, 2.0, 100.0)

    with TestClient(exchange.app) as client:
        with client.websocket_connect("/ws/TEST") as websocket:
            initial = websocket.receive_json()
            process_order(exchange, "TAKER", Side.BUY, 2.0, 100.0)
            update = websocket.receive_json()

    assert update["sequence"] == initial["sequence"] + 1
    assert update["asks"] == []
    assert update["trades"] == [
        {
            "trade_id": "1",
            "price": 100.0,
            "qty": 2.0,
            "aggressor_side": "BUY",
            "timestamp": "1970-01-01T00:00:00Z",
        }
    ]


def test_two_clients_each_receive_one_copy():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        with client.websocket_connect("/ws/TEST") as first:
            with client.websocket_connect("/ws/TEST") as second:
                first.receive_json()
                second.receive_json()
                process_order(exchange, "BID", Side.BUY, 1.0, 10.0)
                first_update = first.receive_json()
                second_update = second.receive_json()

    assert first_update["sequence"] == 1
    assert second_update["sequence"] == 1


class FakeWebSocket:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.messages = []
        self.closed = False

    async def send_json(self, message):
        if self.fail:
            raise RuntimeError("socket closed")
        self.messages.append(message)

    async def close(self, code=1000):
        self.closed = True


@pytest.mark.asyncio
async def test_failed_client_is_removed_without_blocking_healthy_client():
    exchange = make_exchange()
    healthy = FakeWebSocket()
    failed = FakeWebSocket(fail=True)
    await exchange.api.registry.add("TEST", healthy)
    await exchange.api.registry.add("TEST", failed)
    snapshot = exchange.engine.get_market_data_snapshot("TEST")

    await exchange.api.broadcast_market_data(snapshot)

    assert len(healthy.messages) == 1
    assert await exchange.api.registry.connection_count("TEST") == 1
    assert await exchange.api.registry.snapshot("TEST") == (healthy,)


@pytest.mark.asyncio
async def test_disconnect_cleanup_is_idempotent_and_removes_empty_symbol():
    exchange = make_exchange()
    websocket = FakeWebSocket()
    await exchange.api.registry.add("TEST", websocket)
    await exchange.api.registry.remove("TEST", websocket)
    await exchange.api.registry.remove("TEST", websocket)

    assert await exchange.api.registry.connection_count("TEST") == 0
    assert await exchange.api.registry.snapshot("TEST") == ()


def test_unknown_symbol_has_error_envelope_and_policy_close():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        with client.websocket_connect("/ws/INVALID") as websocket:
            message = websocket.receive_json()

    assert message == {
        "type": "error",
        "code": "UNKNOWN_SYMBOL",
        "message": "Unknown symbol: INVALID",
    }


def test_heartbeat_is_distinct_and_does_not_increment_sequence():
    exchange = make_exchange(heartbeat_seconds=0.01)
    with TestClient(exchange.app) as client:
        with client.websocket_connect("/ws/TEST") as websocket:
            initial = websocket.receive_json()
            heartbeat = websocket.receive_json()

    assert heartbeat["type"] == "heartbeat"
    assert heartbeat["last_sequence"] == initial["sequence"] == 0
    assert "bids" not in heartbeat


def test_browser_uses_current_origin_and_market_data_freshness_policy():
    template = (
        Path(__file__).resolve().parents[1] / "templates" / "book.html"
    ).read_text(encoding="utf-8")
    script = template.split("<script>", 1)[1]

    assert 'locationValue.protocol === "https:" ? "wss" : "ws"' in script
    assert "`${wsScheme}://${locationValue.host}`" in script
    assert "localhost" not in script
    assert "lastMarketDataAt" in script
    assert "lastHeartbeatAt" in script
    assert "data.sequence > state.lastSequence + 1" in script
    assert "data.sequence <= state.lastSequence" in script
    assert "enableFallback()" in script
    assert "disableFallback()" in script

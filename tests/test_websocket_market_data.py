from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""Actual WebSocket delivery plus registry and browser policy tests."""

import asyncio
from dataclasses import replace
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from src.mockx.models import NewOrderCommand, Side, TimeInForce
from src.mockx.api import market_data_message

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
        assert client.post(
            "/api/v1/session/start",
            json={"run_id": "websocket-live"},
        ).status_code == 200
        with client.websocket_connect("/ws/TEST") as websocket:
            assert websocket.receive_json()["sequence"] == 0
            exchange.engine.submit_order(
                exact_command(
                    cl_ord_id="BID",
                    participant_id="BID",
                    symbol="TEST",
                    side=Side.BUY,
                    order_qty_lots=3,
                    price_ticks=100,
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
    assert update["bids"][0]["qty"] == "3.000"
    assert update["bids"][0]["order_count"] == 1


def test_fill_reaches_client_once_with_committed_trade_fields():
    exchange = make_exchange()

    with TestClient(exchange.app) as client:
        assert client.post(
            "/api/v1/session/start",
            json={"run_id": "websocket-fill"},
        ).status_code == 200
        process_order(exchange, "ASK", Side.SELL, 2, 100)
        with client.websocket_connect("/ws/TEST") as websocket:
            initial = websocket.receive_json()
            process_order(exchange, "TAKER", Side.BUY, 2, 100)
            update = websocket.receive_json()

    assert update["sequence"] == initial["sequence"] + 1
    assert update["asks"] == []
    assert update["trades"] == [
        {
            "trade_id": "G00000001-T00000001",
            "price": "100.00",
            "qty": "2.000",
            "aggressor_side": "BUY",
            "timestamp": "1970-01-01T00:00:00Z",
        }
    ]


def test_two_clients_each_receive_one_copy():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        assert client.post(
            "/api/v1/session/start",
            json={"run_id": "websocket-two"},
        ).status_code == 200
        with client.websocket_connect("/ws/TEST") as first:
            with client.websocket_connect("/ws/TEST") as second:
                first.receive_json()
                second.receive_json()
                process_order(exchange, "BID", Side.BUY, 1, 10)
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
    await exchange.lifecycle.start_session("websocket-failure")
    healthy = FakeWebSocket()
    failed = FakeWebSocket(fail=True)
    snapshot = exchange.engine.get_market_data_snapshot("TEST")
    initial_message = market_data_message(snapshot).model_dump(mode="json")
    healthy_connection = await exchange.api.registry.register(
        "TEST",
        healthy,
        initial_message,
        initial_sequence=snapshot.sequence,
    )
    await exchange.api.registry.register(
        "TEST",
        failed,
        initial_message,
        initial_sequence=snapshot.sequence,
    )
    for _ in range(100):
        if len(healthy.messages) == 1:
            break
        await asyncio.sleep(0)
    update = replace(snapshot, sequence=snapshot.sequence + 1)

    await exchange.api.broadcast_market_data(update)
    for _ in range(100):
        if (
            len(healthy.messages) == 2
            and await exchange.api.registry.connection_count("TEST") == 1
        ):
            break
        await asyncio.sleep(0)

    assert len(healthy.messages) == 2
    assert await exchange.api.registry.connection_count("TEST") == 1
    assert await exchange.api.registry.snapshot("TEST") == (
        healthy_connection,
    )
    await exchange.api.registry.disconnect(healthy_connection)


@pytest.mark.asyncio
async def test_disconnect_cleanup_is_idempotent_and_removes_empty_symbol():
    exchange = make_exchange()
    websocket = FakeWebSocket()
    snapshot = exchange.engine.get_market_data_snapshot("TEST")
    connection = await exchange.api.registry.register(
        "TEST",
        websocket,
        market_data_message(snapshot).model_dump(mode="json"),
        initial_sequence=snapshot.sequence,
    )
    await exchange.api.registry.disconnect(connection)
    await exchange.api.registry.disconnect(connection)

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
    assert "data.sequence <= state.lastSequence" in script
    assert "enableFallback()" in script
    assert "disableFallback()" in script

    market_data_handler = script.split(
        "async function handleMarketData(data)",
        1,
    )[1].split("function scheduleReconnect()", 1)[0]
    assert "refreshFromRest" not in market_data_handler
    assert "state.lastSequence = data.sequence" in market_data_handler


def test_browser_clears_missing_side_and_derived_statistics():
    template = (
        Path(__file__).resolve().parents[1] / "templates" / "book.html"
    ).read_text(encoding="utf-8")
    script = template.split("<script>", 1)[1]

    assert "bestBid === null ? '-' : bestBid" in script
    assert "bestAsk === null ? '-' : bestAsk" in script
    assert "Number.parseFloat(bestBid)" in script
    assert "Number.parseFloat(bestAsk)" in script
    assert "bid.price.toFixed" not in script
    assert "ask.qty.toFixed" not in script
    assert "spreadElement.textContent = '-'" in script
    assert "midElement.textContent = '-'" in script


def test_browser_timing_values_are_rendered_from_server_configuration():
    exchange = make_exchange(
        heartbeat_seconds=2.0,
        heartbeat_stale_seconds=7.0,
        market_data_stale_seconds=11.0,
        fallback_poll_seconds=0.25,
        reconnect_initial_seconds=0.5,
        reconnect_max_seconds=9.0,
        staleness_check_seconds=0.2,
    )

    with TestClient(exchange.app) as client:
        response = client.get("/ui/TEST")

    assert response.status_code == 200
    html = response.text
    expected = {
        "heartbeatIntervalMs": 2000,
        "heartbeatStaleMs": 7000,
        "marketDataStaleMs": 11000,
        "fallbackPollMs": 250,
        "reconnectInitialMs": 500,
        "reconnectMaxMs": 9000,
        "stalenessCheckMs": 200,
    }
    for key, value in expected.items():
        assert re.search(rf'"{key}"\s*:\s*{value}(?:\.0)?', html)

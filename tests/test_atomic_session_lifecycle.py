"""Atomic run lifecycle, replay, identifier, and ingress regressions."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import re

from httpx import ASGITransport, AsyncClient
import pytest

from src.mockx.gateway import fix
from src.mockx.lifecycle import (
    LifecycleError,
    LifecycleIngressRejected,
    LifecycleState,
)
from src.mockx.main import MockExchange
from src.mockx.models import (
    NewOrderCommand,
    SessionIdentity,
    Side,
    TimeInForce,
)

from .api_helpers import api_config, make_exchange


SESSION_A = "FIX.4.4:CLIENT-A->MOCKX"
SESSION_B = "FIX.4.4:CLIENT-B->MOCKX"


async def _open_client(exchange: MockExchange) -> AsyncClient:
    await exchange.start_components()
    return AsyncClient(
        transport=ASGITransport(app=exchange.app),
        base_url="http://test",
    )


def _new_order(
    exchange: MockExchange,
    cl_ord_id: str,
    *,
    session_id: str | None = None,
    side: Side = Side.BUY,
) -> NewOrderCommand:
    return NewOrderCommand(
        cl_ord_id=cl_ord_id,
        symbol="TEST",
        side=side,
        qty=1.0,
        price=100.0,
        tif=TimeInForce.GTC,
        submitted_at=exchange.scheduler.current_time,
        session_id=session_id,
        session_alias="CLIENT" if session_id else None,
    )


def _fix_new_order_message():
    message = fix.Message()
    message.getHeader().setField(
        fix.MsgType(fix.MsgType_NewOrderSingle)
    )
    return message


@pytest.mark.asyncio
async def test_two_concurrent_start_requests_have_exactly_one_success(
    monkeypatch,
):
    exchange = make_exchange()
    client = await _open_client(exchange)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_flush = exchange.metrics.flush

    async def paused_flush():
        entered.set()
        await release.wait()
        return await original_flush()

    monkeypatch.setattr(exchange.metrics, "flush", paused_flush)
    first = asyncio.create_task(
        client.post(
            "/api/v1/session/start",
            json={"run_id": "first"},
        )
    )
    await entered.wait()
    second = await client.post(
        "/api/v1/session/start",
        json={"run_id": "second"},
    )
    release.set()
    first_response = await first

    assert [first_response.status_code, second.status_code].count(200) == 1
    assert second.status_code == 503
    assert second.json()["detail"]["code"] == (
        "LIFECYCLE_TRANSITION_IN_PROGRESS"
    )
    assert exchange.lifecycle.state == LifecycleState.ACTIVE
    assert exchange.lifecycle.run_id == "first"

    await client.aclose()
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_active_start_is_409_and_empty_end_has_null_public_fields():
    exchange = make_exchange()
    client = await _open_client(exchange)
    started = await client.post(
        "/api/v1/session/start",
        json={"run_id": "public-contract"},
    )
    repeated = await client.post(
        "/api/v1/session/start",
        json={"run_id": "duplicate"},
    )
    ended = await client.post("/api/v1/session/end")
    empty = await client.post("/api/v1/session/end")

    assert started.json()["run_generation"] == 1
    assert repeated.status_code == 409
    assert repeated.json()["detail"]["code"] == "SESSION_ALREADY_ACTIVE"
    assert ended.json()["run_generation"] == 1
    assert ended.json()["final_stats"]["run_generation"] == 1
    assert empty.json() == {
        "status": "no_active_session",
        "session_id": None,
        "run_generation": None,
        "final_stats": None,
    }

    await client.aclose()
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_start_serializes_end_and_time_advance(monkeypatch):
    exchange = make_exchange()
    client = await _open_client(exchange)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_flush = exchange.metrics.flush

    async def paused_flush():
        entered.set()
        await release.wait()
        return await original_flush()

    monkeypatch.setattr(exchange.metrics, "flush", paused_flush)
    starting = asyncio.create_task(
        client.post(
            "/api/v1/session/start",
            json={"run_id": "racing-start"},
        )
    )
    await entered.wait()
    ending = await client.post("/api/v1/session/end")
    advancing = await client.post(
        "/admin/advance-time",
        json={"milliseconds": 1},
    )
    release.set()
    started = await starting

    assert started.status_code == 200
    assert ending.status_code == 503
    assert advancing.status_code == 503
    assert ending.json()["detail"]["code"] == (
        "LIFECYCLE_TRANSITION_IN_PROGRESS"
    )
    assert advancing.json()["detail"]["code"] == (
        "LIFECYCLE_TRANSITION_IN_PROGRESS"
    )
    assert exchange.scheduler.current_time.isoformat().startswith(
        "1970-01-01T00:00:00"
    )

    await client.aclose()
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_fix_order_during_starting_is_session_rejected(monkeypatch):
    exchange = make_exchange()
    client = await _open_client(exchange)
    entered = asyncio.Event()
    release = asyncio.Event()
    rejected = []
    original_flush = exchange.metrics.flush

    async def paused_flush():
        entered.set()
        await release.wait()
        return await original_flush()

    monkeypatch.setattr(exchange.metrics, "flush", paused_flush)
    monkeypatch.setattr(
        exchange.fix_app,
        "send_reject",
        lambda _session, text, **kwargs: rejected.append(
            (text, kwargs.get("ref_msg_type"))
        ),
    )
    starting = asyncio.create_task(
        client.post(
            "/api/v1/session/start",
            json={"run_id": "fix-starting"},
        )
    )
    await entered.wait()
    exchange.fix_app.fromApp(
        _fix_new_order_message(),
        fix.SessionID("FIX.4.4", "CLIENT", "MOCKX"),
    )

    assert rejected == [
        (
            "LIFECYCLE_TRANSITION_IN_PROGRESS",
            fix.MsgType_NewOrderSingle,
        )
    ]
    assert exchange.engine.orders == {}
    assert exchange.metrics.snapshot().orders_submitted == 0

    release.set()
    assert (await starting).status_code == 200
    await client.aclose()
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_fix_order_during_ending_is_session_rejected(monkeypatch):
    exchange = make_exchange()
    client = await _open_client(exchange)
    assert (
        await client.post(
            "/api/v1/session/start",
            json={"run_id": "fix-ending"},
        )
    ).status_code == 200

    entered = asyncio.Event()
    release = asyncio.Event()
    rejected = []
    original_flush = exchange.metrics.flush

    async def paused_flush():
        entered.set()
        await release.wait()
        return await original_flush()

    monkeypatch.setattr(exchange.metrics, "flush", paused_flush)
    monkeypatch.setattr(
        exchange.fix_app,
        "send_reject",
        lambda _session, text, **kwargs: rejected.append(
            (text, kwargs.get("ref_msg_type"))
        ),
    )
    ending = asyncio.create_task(client.post("/api/v1/session/end"))
    await entered.wait()
    exchange.fix_app.fromApp(
        _fix_new_order_message(),
        fix.SessionID("FIX.4.4", "CLIENT", "MOCKX"),
    )

    assert rejected == [
        (
            "LIFECYCLE_TRANSITION_IN_PROGRESS",
            fix.MsgType_NewOrderSingle,
        )
    ]
    assert exchange.engine.orders == {}

    release.set()
    assert (await ending).status_code == 200
    await client.aclose()
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_mid_reset_start_failure_enters_failed_and_stays_non_ready(
    monkeypatch,
):
    exchange = make_exchange()
    client = await _open_client(exchange)

    def fail_reset():
        raise RuntimeError("engine reset failed")

    monkeypatch.setattr(exchange.engine, "reset_run_state", fail_reset)
    response = await client.post(
        "/api/v1/session/start",
        json={"run_id": "broken"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SESSION_START_FAILED"
    assert exchange.lifecycle.state == LifecycleState.FAILED
    assert exchange.lifecycle.run_id is None
    assert (await client.get("/readyz")).status_code == 503
    repeated = await client.post(
        "/api/v1/session/start",
        json={"run_id": "retry"},
    )
    assert repeated.status_code == 503
    assert repeated.json()["detail"]["code"] == "LIFECYCLE_FAILED"

    await client.aclose()
    await exchange.stop_components()


@pytest.mark.asyncio
async def test_end_flush_failure_restores_active_for_retry(monkeypatch):
    exchange = make_exchange()
    client = await _open_client(exchange)
    assert (
        await client.post(
            "/api/v1/session/start",
            json={"run_id": "end-retry"},
        )
    ).status_code == 200

    monkeypatch.setattr(exchange.metrics, "flush", lambda: _false_async())
    failed = await client.post("/api/v1/session/end")

    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "METRICS_FLUSH_FAILED"
    assert exchange.lifecycle.state == LifecycleState.ACTIVE
    assert exchange.lifecycle.run_id == "end-retry"

    await client.aclose()
    await exchange.stop_components()


async def _false_async() -> bool:
    return False


async def _rejection_pattern(
    exchange: MockExchange,
    run_id: str,
    count: int,
):
    await exchange.lifecycle.start_session(run_id)
    outcomes = []
    identifiers = []
    for index in range(count):
        handle = exchange.engine.submit_order(
            _new_order(exchange, f"{run_id}-{index}")
        )
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        order = exchange.engine.orders[handle]
        outcomes.append(order.status.value)
        identifiers.append(order.exchange_order_id)
    await exchange.lifecycle.end_session()
    return outcomes, identifiers


@pytest.mark.asyncio
async def test_rng_reseeds_and_replay_differs_only_by_generation_prefix():
    config = api_config()
    config["execution"]["reject_prob"] = 0.5
    exchange = MockExchange(config)

    first_outcomes, first_ids = await _rejection_pattern(
        exchange,
        "first",
        12,
    )
    second_outcomes, second_ids = await _rejection_pattern(
        exchange,
        "second",
        12,
    )

    fresh = MockExchange(config)
    fresh_outcomes, _ = await _rejection_pattern(fresh, "fresh", 12)
    normalize = lambda value: re.sub(r"^G\d{8}", "G", value)

    assert first_outcomes == second_outcomes == fresh_outcomes
    assert [normalize(value) for value in first_ids] == [
        normalize(value) for value in second_ids
    ]
    assert first_ids != second_ids


@pytest.mark.asyncio
async def test_public_ids_and_cl_ord_history_survive_reset_by_stable_session():
    exchange = make_exchange()
    exchange.fix_app.transport_sender = (
        lambda transport, report: transport.append(report)
    )
    transport_a = []
    transport_b = []
    exchange.fix_app.register_session(
        SessionIdentity(SESSION_A, alias="CLIENT-A"),
        transport_a,
    )
    exchange.fix_app.register_session(
        SessionIdentity(SESSION_B, alias="CLIENT-B"),
        transport_b,
    )

    await exchange.lifecycle.start_session("ids-one")
    token = exchange.lifecycle.token
    first_handle = await exchange.lifecycle.execute_ingress(
        token,
        lambda: exchange.fix_app.submit_new_order_command(
            _new_order(exchange, "SAME", session_id=SESSION_A)
        ),
    )
    first_order_id = exchange.engine.orders[
        first_handle
    ].exchange_order_id
    await exchange.lifecycle.end_session()

    exchange.fix_app.unregister_session(SESSION_A)
    exchange.fix_app.register_session(
        SessionIdentity(SESSION_A, alias="CLIENT-A"),
        transport_a,
    )
    await exchange.lifecycle.start_session("ids-two")
    token = exchange.lifecycle.token
    with pytest.raises(ValueError, match="Duplicate ClOrdID"):
        await exchange.lifecycle.execute_ingress(
            token,
            lambda: exchange.fix_app.submit_new_order_command(
                _new_order(exchange, "SAME", session_id=SESSION_A)
            ),
        )

    second_handle = await exchange.lifecycle.execute_ingress(
        token,
        lambda: exchange.fix_app.submit_new_order_command(
            _new_order(exchange, "SAME", session_id=SESSION_B)
        ),
    )
    second_order_id = exchange.engine.orders[
        second_handle
    ].exchange_order_id

    assert first_order_id == "G00000001-O00000001"
    assert second_order_id == "G00000002-O00000001"
    assert first_order_id != second_order_id


@pytest.mark.asyncio
async def test_stale_active_ingress_token_cannot_enter_a_new_generation():
    exchange = make_exchange()
    await exchange.lifecycle.start_session("token-one")
    stale = exchange.lifecycle.token
    await exchange.lifecycle.end_session()
    await exchange.lifecycle.start_session("token-two")
    called = False

    def action():
        nonlocal called
        called = True

    with pytest.raises(
        LifecycleIngressRejected,
        match="LIFECYCLE_CHANGED",
    ):
        await exchange.lifecycle.execute_ingress(stale, action)
    assert not called


@pytest.mark.asyncio
async def test_gateway_rejection_ids_use_the_active_generation():
    exchange = make_exchange()
    delivered = []
    exchange.fix_app.transport_sender = (
        lambda _transport, report: delivered.append(report)
    )
    exchange.fix_app.register_session(
        SessionIdentity(SESSION_A, alias="CLIENT-A"),
        object(),
    )
    await exchange.lifecycle.start_session("gateway-reject")

    await exchange.lifecycle.execute_ingress(
        exchange.lifecycle.token,
        lambda: exchange.fix_app._send_input_rejection(
            object(),
            cl_ord_id="BAD",
            symbol="TEST",
            side=Side.BUY,
            qty=0.0,
            price=None,
            tif=TimeInForce.GTC,
            session_id=SESSION_A,
            session_alias="CLIENT-A",
            text="bad input",
        ),
    )

    assert len(delivered) == 1
    assert delivered[0].exec_id == "G00000001-E00000001"
    assert delivered[0].exchange_order_id == "G00000001-O00000001"


@pytest.mark.asyncio
async def test_order_exec_and_trade_ids_do_not_repeat_for_continuing_session():
    exchange = make_exchange()
    reports = []
    trades = []
    original_report = exchange.engine.on_execution_report
    original_trade = exchange.engine.on_trade

    def capture_report(report):
        reports.append(report)
        original_report(report)

    def capture_trade(trade):
        trades.append(trade)
        original_trade(trade)

    exchange.engine.on_execution_report = capture_report
    exchange.engine.on_trade = capture_trade
    exchange.fix_app.register_session(
        SessionIdentity(SESSION_A, alias="CLIENT-A"),
        [],
    )

    async def run_pair(run_id: str, prefix: str):
        await exchange.lifecycle.start_session(run_id)
        token = exchange.lifecycle.token
        handles = []
        for cl_ord_id, side in (
            (f"{prefix}-SELL", Side.SELL),
            (f"{prefix}-BUY", Side.BUY),
        ):
            handles.append(
                await exchange.lifecycle.execute_ingress(
                    token,
                    lambda cl_ord_id=cl_ord_id, side=side: (
                        exchange.fix_app.submit_new_order_command(
                            _new_order(
                                exchange,
                                cl_ord_id,
                                session_id=SESSION_A,
                                side=side,
                            )
                        )
                    ),
                )
            )
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        order_ids = {
            exchange.engine.orders[handle].exchange_order_id
            for handle in handles
        }
        exec_ids = {report.exec_id for report in reports}
        trade_ids = {
            trade.exchange_trade_id
            for trade in trades
            if trade.exchange_trade_id is not None
        }
        reports.clear()
        trades.clear()
        await exchange.lifecycle.end_session()
        return order_ids, exec_ids, trade_ids

    first = await run_pair("id-run-one", "R1")
    second = await run_pair("id-run-two", "R2")

    assert all(value.startswith("G00000001-") for group in first for value in group)
    assert all(value.startswith("G00000002-") for group in second for value in group)
    for first_group, second_group in zip(first, second):
        assert first_group
        assert second_group
        assert first_group.isdisjoint(second_group)


@pytest.mark.asyncio
async def test_final_stats_are_run_local_and_empty_end_has_no_stats():
    exchange = make_exchange()

    await exchange.lifecycle.start_session("stats-one")
    exchange.engine.submit_order(_new_order(exchange, "ONE"))
    exchange.engine.submit_order(_new_order(exchange, "TWO"))
    await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
    first = await exchange.lifecycle.end_session()

    await exchange.lifecycle.start_session("stats-two")
    exchange.engine.submit_order(_new_order(exchange, "THREE"))
    await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
    second = await exchange.lifecycle.end_session()
    empty = await exchange.lifecycle.end_session()

    assert first.final_stats.orders.submitted == 2
    assert second.final_stats.orders.submitted == 1
    assert first.final_stats.run_generation == 1
    assert second.final_stats.run_generation == 2
    assert empty.run_id is None
    assert empty.run_generation is None
    assert empty.final_stats is None


@pytest.mark.asyncio
async def test_end_freezes_pending_events_until_next_start_discards_them():
    exchange = make_exchange()
    handled = []
    exchange.scheduler.register_handler("probe", handled.append)
    await exchange.lifecycle.start_session("pending")
    exchange.scheduler.schedule_event("probe", "must-not-run", 10)

    ended = await exchange.lifecycle.end_session()
    with pytest.raises(Exception) as caught:
        await exchange.lifecycle.advance_to(
            exchange.scheduler.current_time + timedelta(milliseconds=10)
        )

    assert getattr(caught.value, "code", None) == "SESSION_NOT_ACTIVE"
    assert ended.final_stats.scheduler.pending_events == 1
    assert exchange.scheduler.get_pending_count() == 1
    assert handled == []

    await exchange.lifecycle.start_session("after-pending")
    assert exchange.scheduler.get_pending_count() == 0
    assert handled == []


class _FakeWebSocket:
    def __init__(self):
        self.closed = False
        self.messages = []

    async def close(self, code=1000):
        self.closed = True

    async def send_json(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_start_closes_websockets_and_stale_generation_is_not_broadcast():
    exchange = make_exchange()
    old_client = _FakeWebSocket()
    await exchange.api.registry.add("TEST", old_client)
    await exchange.lifecycle.start_session("ws-one")
    old_snapshot = exchange.engine.get_market_data_snapshot("TEST")

    assert old_client.closed
    assert await exchange.api.registry.connection_count() == 0

    await exchange.lifecycle.end_session()
    await exchange.lifecycle.start_session("ws-two")
    new_client = _FakeWebSocket()
    await exchange.api.registry.add("TEST", new_client)
    await exchange.api.broadcast_market_data(old_snapshot)

    assert new_client.messages == []


@pytest.mark.asyncio
async def test_transition_time_websocket_registration_and_realtime_tick_are_gated(
    monkeypatch,
):
    exchange = make_exchange()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_flush = exchange.metrics.flush
    registered = False

    async def paused_flush():
        entered.set()
        await release.wait()
        return await original_flush()

    async def register():
        nonlocal registered
        registered = True

    monkeypatch.setattr(exchange.metrics, "flush", paused_flush)
    starting = asyncio.create_task(
        exchange.lifecycle.start_session("transition-gates")
    )
    await entered.wait()
    initial_time = exchange.scheduler.current_time

    with pytest.raises(LifecycleError) as websocket_error:
        await exchange.lifecycle.websocket_snapshot(register)
    realtime_events = await exchange.lifecycle.advance_realtime_by(
        timedelta(milliseconds=10)
    )

    assert getattr(websocket_error.value, "code", None) == (
        "LIFECYCLE_TRANSITION_IN_PROGRESS"
    )
    assert not registered
    assert realtime_events == 0
    assert exchange.scheduler.current_time == initial_time

    release.set()
    await starting

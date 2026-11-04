"""Native replay bootstrap, guarded cleanup, and paced-clock contracts."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from src.mockx.configuration import ConfigurationError, validate_config
from src.mockx.main import MockExchange
from src.mockx.gateway import fix
from src.mockx.models import OrderStatus
from tests.api_helpers import api_config, make_exchange


SESSION = "FIX.4.4:MOCKX1->CLIENT1"


def seed(cl_ord_id="seed-bid", side="BUY", price="99", quantity="2"):
    return dict(
        symbol="TEST",
        side=side,
        price=price,
        quantity=quantity,
        session_id=SESSION,
        cl_ord_id=cl_ord_id,
    )


def test_native_seed_is_resting_and_indexed_without_order_ack_or_trade():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        response = client.post(
            "/api/v1/session/start",
            json={
                "run_id": "seeded",
                "initial_orders": [seed(), seed("seed-ask", "SELL", "101", "3")],
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["starting_time"] == "1970-01-01T00:00:00Z"
        assert [row["cl_ord_id"] for row in data["seed_orders"]] == [
            "seed-bid",
            "seed-ask",
        ]
        for mapping in data["seed_orders"]:
            order = next(
                order
                for order in exchange.engine.orders.values()
                if order.exchange_order_id == mapping["order_id"]
            )
            assert order.participant_id == "participant-1"
            assert order.leaves_qty_lots in {2, 3}
            assert exchange.session_registry.has_cl_ord_id(SESSION, order.cl_ord_id)
        stats = client.get("/stats").json()
        assert stats["scheduler"]["current_time"] == data["starting_time"]
        assert stats["scheduler"]["rate"] == 1
        assert stats["trades"]["count"] == 0
        assert not exchange.fix_app.delivery_queue.pending_order_keys()


@pytest.mark.asyncio
async def test_seed_reports_do_not_create_new_acceptances_across_run_resets():
    from tests.test_fix_ingress_pipeline import _cancel, _new_order

    raw = api_config()
    raw["fix"]["session_participants"]["FIX.4.4:MOCKX1->CLIENT2"] = "participant-2"
    exchange = MockExchange(validate_config(raw, environ={}))
    reports = []
    await exchange.start_components()
    try:
        sessions = [
            fix.SessionID("FIX.4.4", "MOCKX1", role) for role in ("CLIENT1", "CLIENT2")
        ]
        for session in sessions:
            exchange.fix_app.register_session(
                exchange.fix_app._session_identity(session), object()
            )
        exchange.fix_app.transport_sender = lambda _session, report: reports.append(
            report
        )
        for generation in (1, 2):
            bid, ask = f"seed-bid-{generation}", f"seed-ask-{generation}"
            reports.clear()
            await exchange.lifecycle.start_session(
                f"seed-metrics-{generation}",
                initial_orders=(seed(bid), seed(ask, "SELL", "101", "3")),
            )
            before = exchange.metrics.snapshot()
            assert before.orders_submitted == before.orders_accepted == 0
            assert reports == []
            exchange.fix_app.fromApp(
                _cancel(f"cancel-{generation}", bid, generation, qty=2), sessions[0]
            )
            await exchange.fix_app.wait_for_ingress()
            await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
            canceled = exchange.metrics.snapshot()
            assert canceled.orders_canceled == 1
            assert canceled.orders_submitted == canceled.orders_accepted == 0
            exchange.fix_app.fromApp(
                _new_order(f"take-{generation}", generation, qty=3, price=101),
                sessions[1],
            )
            await exchange.fix_app.wait_for_ingress()
            await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
            filled = exchange.metrics.snapshot()
            assert filled.orders_submitted == filled.orders_accepted == 1
            assert filled.orders_filled == 2
            assert filled.trades == 1
            ended = await exchange.lifecycle.end_session()
            assert (
                ended.final_stats.orders.submitted
                == ended.final_stats.orders.accepted
                == 1
            )
            assert exchange.metrics.snapshot().orders_accepted == 0
            lifetime = exchange.metrics.process_lifetime_snapshot()
            assert lifetime["orders"]["submitted"] == generation
            assert lifetime["orders"]["accepted"] == generation
            assert (
                exchange.metrics.registry.get_sample_value(
                    "mockx_orders_accepted_total"
                )
                == generation
            )
    finally:
        await exchange.stop_components()


@pytest.mark.parametrize(
    "rows",
    [
        [seed(), seed()],
        [seed(), seed("cross", "SELL", "99")],
        [seed(), {**seed("unknown"), "session_id": "FIX.4.4:UNKNOWN->CLIENT"}],
        [seed(), seed("off-tick", price="99.5")],
    ],
)
def test_invalid_initial_book_rejects_before_any_run_or_claim_mutation(rows):
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        response = client.post(
            "/api/v1/session/start",
            json={
                "run_id": "invalid-seed",
                "initial_orders": rows,
            },
        )
        assert response.status_code == 422
        assert exchange.lifecycle.run_generation == 0
        assert exchange.engine.orders == {}
        assert not exchange.session_registry.has_cl_ord_id(SESSION, "seed-bid")
        assert (
            client.post(
                "/api/v1/session/start",
                json={
                    "run_id": "valid-seed",
                    "initial_orders": [seed()],
                },
            ).status_code
            == 200
        )


def test_stale_cleanup_cannot_end_a_new_run():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        first = client.post("/api/v1/session/start", json={"run_id": "first"}).json()
        assert (
            client.post(
                "/api/v1/session/end",
                json={
                    "expected_run_generation": first["run_generation"],
                },
            ).status_code
            == 200
        )
        second = client.post("/api/v1/session/start", json={"run_id": "second"}).json()
        stale = client.post(
            "/api/v1/session/end",
            json={
                "expected_run_generation": first["run_generation"],
            },
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "RUN_GENERATION_MISMATCH"
        assert exchange.lifecycle.run_id == "second"
        assert exchange.lifecycle.run_generation == second["run_generation"]


@pytest.mark.parametrize("rate", [0, -1, 1.1, True, float("inf"), float("nan")])
def test_scheduler_rejects_invalid_realtime_rate(rate):
    raw = api_config()
    raw["scheduler"]["rate"] = rate
    with pytest.raises(ConfigurationError):
        validate_config(raw, environ={})


class Clock:
    value = 1_000_000_000

    def now_ns(self):
        return self.value


@pytest.mark.asyncio
async def test_realtime_clock_uses_elapsed_monotonic_time_and_bounded_catchup():
    raw = api_config()
    raw["scheduler"].update(mode="realtime", rate=0.5, interval_ms=1)
    exchange = MockExchange(validate_config(raw, environ={}))
    clock = Clock()
    exchange.lifecycle.realtime_clock = clock
    await exchange.lifecycle.start_session("paced")
    origin = exchange.scheduler.current_time
    clock.value += 500_000_000
    await exchange.lifecycle.advance_realtime_clock()
    assert exchange.scheduler.current_time == origin + timedelta(milliseconds=100)
    assert exchange.lifecycle.realtime_lag_ms == 150
    for _ in range(2):
        await exchange.lifecycle.advance_realtime_clock()
    assert exchange.scheduler.current_time == origin + timedelta(milliseconds=250)
    await exchange.lifecycle.end_session()
    clock.value += 10_000_000_000
    await exchange.lifecycle.start_session("new-paced")
    origin = exchange.scheduler.current_time
    await exchange.lifecycle.advance_realtime_clock()
    assert exchange.scheduler.current_time == origin
    await exchange.aclose()


@pytest.mark.asyncio
async def test_seed_publishes_native_book_and_can_be_canceled_over_fix():
    from tests.test_itch_integration import (
        CapturingMoldTransport,
        decode_packets,
        itch_config,
        replay_displayed_book,
    )
    from tests.test_fix_ingress_pipeline import _cancel

    exchange = MockExchange(validate_config(itch_config(), environ={}))
    packets = []
    exchange.itch_feed.transport_factory = (
        lambda config, on_error: CapturingMoldTransport(
            config,
            on_error=on_error,
            packets=packets,
        )
    )
    await exchange.start_components()
    try:
        session = fix.SessionID("FIX.4.4", "MOCKX1", "CLIENT1")
        reports = []
        exchange.fix_app.register_session(
            exchange.fix_app._session_identity(session), object()
        )
        exchange.fix_app.transport_sender = lambda _session, report: reports.append(
            report
        )
        result = await exchange.lifecycle.start_session(
            "wire-seed", initial_orders=(seed(),)
        )
        _, messages = decode_packets(packets)
        reconstructed, matches = replay_displayed_book(messages)
        order = next(iter(exchange.engine.orders.values()))
        assert order.exchange_order_id == result.seed_orders[0].order_id
        assert reconstructed == {
            order.displayed_order_ref: {
                "side": b"B",
                "qty": 2,
                "price": 990_000,
            }
        }
        assert matches == []
        assert reports == []
        snapshot = await exchange.lifecycle.capture_glimpse_snapshot()
        assert snapshot.orders[0].order_ref_num == order.displayed_order_ref
        assert snapshot.itch_sequence == exchange.itch_feed.sequence_number
        exchange.fix_app.fromApp(_cancel("seed-cancel", "seed-bid", 1, qty=2), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert order.status == OrderStatus.CANCELLED
        assert reports[-1].orig_cl_ord_id == "seed-bid"
        _, messages = decode_packets(packets)
        assert replay_displayed_book(messages) == ({}, [])
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_failed_pre_reset_flush_does_not_consume_seed_claims(monkeypatch):
    from types import SimpleNamespace
    from src.mockx.lifecycle import LifecycleError, LifecycleState

    exchange = make_exchange()

    async def failed_flush():
        return SimpleNamespace(successful=False)

    monkeypatch.setattr(exchange.metrics, "flush", failed_flush)
    with pytest.raises(LifecycleError, match="Buffered metrics"):
        await exchange.lifecycle.start_session("failed", initial_orders=(seed(),))
    assert exchange.lifecycle.state == LifecycleState.STOPPED
    assert exchange.lifecycle.run_generation == 0
    assert exchange.engine.orders == {}
    assert not exchange.session_registry.has_cl_ord_id(SESSION, "seed-bid")


@pytest.mark.asyncio
async def test_unobserved_book_does_not_pay_websocket_serialization(monkeypatch):
    import src.mockx.api as api

    exchange = make_exchange()
    await exchange.lifecycle.start_session("no-viewer")

    def unexpected_payload(_snapshot):
        pytest.fail("No WebSocket subscriber requires a formatted book payload")

    monkeypatch.setattr(api, "market_data_message", unexpected_payload)
    snapshot = exchange.engine.get_market_data_snapshot("TEST")
    await exchange.api.broadcast_market_data(snapshot)
    await exchange.aclose()

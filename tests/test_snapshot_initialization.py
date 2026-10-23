"""Atomic bulk initialization creates ordinary FIX-owned resting orders."""

from fastapi.testclient import TestClient
import pytest

from src.mockx.configuration import validate_config
from src.mockx.gateway import fix
from src.mockx.main import MockExchange
from src.mockx.models import ClientOrderKey, OrderStatus, SessionIdentity
from src.mockx.lifecycle import LifecycleState
from tests.api_helpers import api_config, make_exchange


OWNER = "FIX.4.4:MOCKX1->CLIENT1"


def seed(name="seed", *, side="BUY", quantity="5", price="100", **extra):
    return dict(symbol="TEST", side=side, quantity=quantity, price=price,
                session_id=OWNER, cl_ord_id=name, **extra)


def start(client, snapshot, run_id="seeded"):
    return client.post("/api/v1/session/start", json={
        "run_id": run_id, "snapshot": snapshot,
    })


def test_snapshot_creates_owned_new_orders_without_order_entry_side_effects():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        response = start(client, [seed("bid"), seed("ask", side="SELL", price="102")])
        assert response.status_code == 200, response.text
        assert response.json()["seeded_orders"] == [
            dict(symbol="TEST", session_id=OWNER, cl_ord_id="bid", order_id="G00000001-O00000001"),
            dict(symbol="TEST", session_id=OWNER, cl_ord_id="ask", order_id="G00000001-O00000002"),
        ]
        for order in exchange.engine.orders.values():
            assert order.status == OrderStatus.NEW and order.processed
            assert order.filled_qty_lots == order.canceled_qty_lots == 0
            assert order.session_id == OWNER
            assert order.participant_id == "participant-1"
            assert order.session_alias == "CLIENT1"
            assert exchange.engine.client_order_index[ClientOrderKey(OWNER, order.cl_ord_id)] == order.id
            assert exchange.fix_app.session_registry.has_cl_ord_id(OWNER, order.cl_ord_id)
        assert exchange.scheduler.get_pending_count() == 0
        stats = client.get("/stats").json()
        assert stats["orders"]["submitted"] == 0
        assert stats["reports"]["created"] == 0
        assert stats["orders"]["resting"] == 2
        exchange.engine.books["TEST"].assert_invariants()


@pytest.mark.parametrize("invalid", [
    [seed("valid"), seed("off-grid", price="100.5")],
    [seed("same"), seed("same")],
    [seed("bid"), seed("ask", side="SELL", price="100")],
    [seed("valid"), {**seed("unknown"), "session_id": "FIX.4.4:UNKNOWN->CLIENT"}],
    [seed("valid"), seed("bad-id ")],
    [seed("valid"), seed("not-string", quantity=5)],
    [seed("valid"), seed("spoof", participant_id="forged")],
])
def test_invalid_snapshot_preserves_state_and_does_not_claim_ids(invalid):
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        before_books = exchange.engine.books
        rng = exchange.engine.rng.getstate()
        response = start(client, invalid)
        assert response.status_code in {409, 422}, response.text
        assert exchange.lifecycle.state == LifecycleState.STOPPED
        assert exchange.lifecycle.run_generation == 0
        assert exchange.engine.books is before_books
        assert exchange.engine.rng.getstate() == rng
        assert not exchange.engine.orders
        assert not exchange.fix_app.session_registry.has_cl_ord_id(OWNER, "valid")
        assert start(client, [seed("valid")]).status_code == 200


def test_active_run_cannot_be_overwritten_by_snapshot():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        assert start(client, [seed("first")]).status_code == 200
        first = exchange.engine.orders[1]
        response = start(client, [seed("second")], "second-run")
        assert response.status_code == 409
        assert exchange.engine.orders[1] is first
        assert not exchange.fix_app.session_registry.has_cl_ord_id(OWNER, "second")


@pytest.mark.parametrize("namespace", ["gateway", "engine"])
def test_snapshot_rejects_existing_process_lifetime_claims_atomically(namespace):
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        if namespace == "gateway":
            exchange.fix_app.session_registry.remember_cl_ord_id(OWNER, "taken")
        else:
            exchange.engine._claimed_client_order_ids.add(ClientOrderKey(OWNER, "taken"))
        response = start(client, [seed("fresh"), seed("taken")])
        assert response.status_code == 409, response.text
        assert exchange.lifecycle.run_generation == 0
        assert not exchange.fix_app.session_registry.has_cl_ord_id(OWNER, "fresh")
        assert ClientOrderKey(OWNER, "fresh") not in exchange.engine._claimed_client_order_ids


def test_seed_ids_remain_claimed_after_session_end():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        assert start(client, [seed("old")]).status_code == 200
        assert client.post("/api/v1/session/end").status_code == 200
        before = exchange.engine.orders[1]
        assert start(client, [seed("fresh"), seed("old")], "again").status_code == 409
        assert exchange.engine.orders[1] is before
        assert exchange.lifecycle.run_generation == 1
        assert start(client, [seed("fresh")], "again").status_code == 200


def test_fractional_snapshot_is_exact_and_websocket_matches_rest():
    raw = api_config()
    raw["instruments"]["TEST"].update(tick_size="0.01", lot_size="0.001")
    exchange = MockExchange(validate_config(raw, environ={}))
    with TestClient(exchange.app) as client:
        assert start(client, [seed(quantity="0.123", price="100.01")]).status_code == 200
        order = exchange.engine.orders[1]
        assert (order.order_qty_lots, order.price_ticks) == (123, 10001)
        with client.websocket_connect("/ws/TEST") as websocket:
            message = websocket.receive_json()
        snapshot = client.get("/book/TEST").json()
        assert message["bids"] == snapshot["bids"]
        assert message["sequence"] == snapshot["sequence"]
        assert message["bids"][0]["qty"] == "0.123"
        assert message["bids"][0]["price"] == "100.01"


def test_seed_client_ids_are_scoped_by_configured_session():
    raw = api_config()
    other = "FIX.4.4:MOCKX1->OTHER"
    raw["fix"]["session_participants"][other] = "participant-other"
    exchange = MockExchange(validate_config(raw, environ={}))
    with TestClient(exchange.app) as client:
        response = start(client, [seed("same"), {**seed("same"), "session_id": other}])
        assert response.status_code == 200, response.text
        assert len(response.json()["seeded_orders"]) == 2
        assert exchange.engine.orders[2].participant_id == "participant-other"
        assert exchange.engine.orders[2].session_alias == "OTHER"
        assert exchange.engine.client_order_index[ClientOrderKey(OWNER, "same")] == 1
        assert exchange.engine.client_order_index[ClientOrderKey(other, "same")] == 2


def test_default_start_retains_empty_initialization():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        response = client.post("/api/v1/session/start", json={"run_id": "empty"})
        assert response.status_code == 200
        assert response.json()["seeded_orders"] == []
        assert not exchange.engine.orders


def test_failed_pre_reset_start_does_not_reserve_snapshot_ids(monkeypatch):
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        async def fail_close():
            raise RuntimeError("cannot finish previous session cleanup")
        original = exchange.lifecycle._close_websockets
        monkeypatch.setattr(exchange.lifecycle, "_close_websockets", fail_close)
        assert start(client, [seed("retry")]).status_code == 503
        assert exchange.lifecycle.run_generation == 0
        assert not exchange.fix_app.session_registry.has_cl_ord_id(OWNER, "retry")
        monkeypatch.setattr(exchange.lifecycle, "_close_websockets", original)
        assert start(client, [seed("retry")]).status_code == 200


def test_claim_conflict_during_pre_reset_wait_is_atomic(monkeypatch):
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        before_books = exchange.engine.books
        async def claim_during_cleanup():
            exchange.fix_app.session_registry.remember_cl_ord_id(OWNER, "taken-late")
        monkeypatch.setattr(exchange.lifecycle, "_close_websockets", claim_during_cleanup)
        response = start(client, [seed("fresh"), seed("taken-late")])
        assert response.status_code == 409
        assert exchange.engine.books is before_books
        assert exchange.lifecycle.state == LifecycleState.STOPPED
        assert exchange.lifecycle.run_generation == 0
        assert not exchange.fix_app.session_registry.has_cl_ord_id(OWNER, "fresh")


@pytest.mark.skipif(fix is None, reason="QuickFIX is unavailable")
@pytest.mark.asyncio
async def test_seeded_orders_accept_normal_fix_fill_replace_cancel_and_ownership_checks(caplog):
    from tests.test_fix_ingress_pipeline import _exchange, _session, _new_order, _cancel
    from tests.test_replace_fix import _replace
    from src.mockx.models import CancelRejectReason, ExecutionReport, ExecutionType

    exchange = _exchange()
    await exchange.start_components()
    delivered = []
    exchange.fix_app.transport_sender = lambda _transport, report: delivered.append(report)
    owner, other = _session("CLIENT"), _session("CLIENT2")
    for session in (owner, other):
        exchange.fix_app.register_session(SessionIdentity(session.toString()), object())
    try:
        initial = tuple({**seed(name), "session_id": owner.toString()} for name in ("first", "second"))
        await exchange.lifecycle.start_session("fix-seed", initial)
        first, second = exchange.engine.orders.values()
        assert delivered == []

        async def send(message, session):
            exchange.fix_app.fromApp(message, session)
            await exchange.fix_app.wait_for_ingress()
            await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        await send(_new_order("hit", 1, qty=2, side=fix.Side_SELL), other)
        assert first.status == OrderStatus.PARTIAL and first.filled_qty_lots == 2
        assert second.status == OrderStatus.NEW
        assert delivered[0].exchange_order_id == "G00000001-O00000001"
        assert exchange.engine.orders[3].exchange_order_id == "G00000001-O00000003"
        from src.mockx.metrics import (
            SIM_END_TO_END_LATENCY, SIM_ENGINE_LATENCY, SIM_INBOUND_LATENCY,
            SIM_OUTBOUND_LATENCY,
        )
        measured = {m.metric for m in exchange.latency_tracker.measurements if m.order_id == first.id}
        assert SIM_OUTBOUND_LATENCY in measured
        assert not measured.intersection({SIM_END_TO_END_LATENCY, SIM_ENGINE_LATENCY, SIM_INBOUND_LATENCY})
        await send(_cancel("foreign-cancel", "first", 2), other)
        assert delivered[-1].reason == CancelRejectReason.SESSION_MISMATCH
        await send(_replace("resized", "first", 1, qty=4), owner)
        assert delivered[-1].exec_type == ExecutionType.REPLACED
        assert first.cl_ord_id == "resized" and first.leaves_qty_lots == 2
        assert [order.id for order in exchange.engine.books["TEST"].bids[0].orders] == [1, 2]
        await send(_cancel("cancel", "resized", 2, qty=4), owner)
        assert first.status == OrderStatus.CANCELLED
        assert "Latency trace missing" not in caplog.text
        await send(_new_order("first", 3), owner)
        assert isinstance(delivered[-1], ExecutionReport)
        assert delivered[-1].ord_status == OrderStatus.REJECTED
        assert "DUPLICATE_CL_ORD_ID" in delivered[-1].text
        assert exchange.metrics.snapshot().invalid_latency_measurements == 0
        exchange.engine.books["TEST"].assert_invariants()
    finally:
        await exchange.stop_components()


@pytest.mark.asyncio
async def test_snapshot_publishes_itch_adds_and_glimpse_with_identical_references():
    from tests.test_itch_integration import (
        CapturingMoldTransport, authoritative_wire_book, decode_packets,
        itch_config, replay_displayed_book,
    )

    exchange = MockExchange(validate_config(itch_config(), environ={}))
    packets = []
    exchange.itch_feed.transport_factory = lambda config, on_error: CapturingMoldTransport(
        config, on_error=on_error, packets=packets,
    )
    await exchange.start_components()
    try:
        await exchange.lifecycle.start_session("feed-seed", (
            seed("b1"), seed("b2"), seed("ask", side="SELL", price="102"),
        ))
        _, messages = decode_packets(packets)
        replayed, matches = replay_displayed_book(messages)
        assert replayed == authoritative_wire_book(exchange)
        assert matches == []
        assert len([message for message in messages if message[:1] == b"A"]) == 3
        snapshot = await exchange.lifecycle.capture_glimpse_snapshot()
        assert {order.order_ref_num for order in snapshot.orders} == set(replayed)
        assert snapshot.itch_sequence == exchange.itch_feed.sequence_number
        assert exchange.metrics.snapshot().execution_reports == 0
    finally:
        await exchange.stop_components()

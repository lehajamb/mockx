"""Stable FIX session identity and scoped ClOrdID routing tests."""

from datetime import datetime, timezone

import pytest

from src.mockx.engine import MatchingEngine
from src.mockx.gateway import FIXApplication
from src.mockx.latency import LatencyScheduler
from src.mockx.metrics import LatencyTracker, MetricsCollector
from src.mockx.models import (
    CancelOrderCommand,
    CancelRejectReason,
    ClientOrderKey,
    ExecutionParams,
    FixDeliveryState,
    NewOrderCommand,
    OrderStatus,
    ProcessOrderCommand,
    SessionIdentity,
    Side,
    TimeInForce,
)


SESSION_A = "FIX.4.4:MOCKX->CLIENT-A"
SESSION_B = "FIX.4.4:MOCKX->CLIENT-B"


def make_stack():
    scheduler = LatencyScheduler()
    engine = MatchingEngine(
        ExecutionParams(
            network_latency_ms=0,
            engine_latency_ms=0,
            reject_prob=0.0,
            rand_seed=42,
        ),
        scheduler,
    )
    engine.add_symbol("TEST")
    metrics = MetricsCollector("TEST")
    metrics.bind_engine(engine)
    tracker = LatencyTracker(scheduler, metrics)
    app = FIXApplication(
        engine,
        metrics,
        tracker,
        transport_sender=lambda transport, report: transport.append(report),
    )
    reports = []
    engine.on_execution_report = reports.append
    return engine, metrics, app, reports


def submit(engine, session_id, alias, qty, price, cl_ord_id="ABC"):
    order_id = engine.submit_order(
        NewOrderCommand(
            cl_ord_id=cl_ord_id,
            symbol="TEST",
            side=Side.BUY,
            qty=qty,
            price=price,
            tif=TimeInForce.GTC,
            submitted_at=engine.scheduler.current_time,
            session_id=session_id,
            session_alias=alias,
        )
    )
    engine.process_order(ProcessOrderCommand(order_id, 0))
    return order_id


def register_pair(app):
    transport_a = []
    transport_b = []
    app.register_session(
        SessionIdentity(SESSION_A, alias="shared"),
        transport_a,
    )
    app.register_session(
        SessionIdentity(SESSION_B, alias="shared"),
        transport_b,
    )
    return transport_a, transport_b


def deliver(app, report):
    record = app.enqueue_report(report, delay_ms=0)
    app.process_delivery_due(record.delivery_id)
    return (
        app.delivery_queue.get(record.delivery_id).state
        == FixDeliveryState.DELIVERED
    )


def test_same_cl_ord_id_is_distinct_and_routes_to_own_session():
    engine, _, app, reports = make_stack()
    transport_a, transport_b = register_pair(app)

    order_a = submit(engine, SESSION_A, "shared", 5.0, 99.0)
    order_b = submit(engine, SESSION_B, "shared", 7.0, 98.0)

    assert order_a != order_b
    assert engine.client_order_index[
        ClientOrderKey(SESSION_A, "ABC")
    ] == order_a
    assert engine.client_order_index[
        ClientOrderKey(SESSION_B, "ABC")
    ] == order_b

    for report in reports:
        assert deliver(app, report)

    assert {report.order_id for report in transport_a} == {order_a}
    assert {report.order_id for report in transport_b} == {order_b}
    assert all(report.session_id == SESSION_A for report in transport_a)
    assert all(report.session_id == SESSION_B for report in transport_b)


def test_cross_session_cancel_cannot_mutate_other_order_and_reject_routes_to_requester():
    engine, _, app, _ = make_stack()
    transport_a, transport_b = register_pair(app)
    order_a = submit(
        engine, SESSION_A, "shared", 5.0, 99.0, "A-OWN"
    )
    order_b = submit(
        engine, SESSION_B, "shared", 7.0, 98.0, "FOREIGN"
    )
    before_qty = sum(level.qty for level in engine.books["TEST"].bids)

    rejection = engine.cancel_order(
        CancelOrderCommand(
            cancel_cl_ord_id="CXL-BY-A",
            orig_cl_ord_id="FOREIGN",
            symbol="TEST",
            side=Side.BUY,
            qty=7,
            requested_at=engine.scheduler.current_time,
            session_id=SESSION_A,
            session_alias="shared",
        )
    )

    assert rejection.reason == CancelRejectReason.SESSION_MISMATCH
    assert deliver(app, rejection)
    assert transport_a == [rejection]
    assert transport_b == []
    assert engine.orders[order_a].status == OrderStatus.NEW
    assert engine.orders[order_b].status == OrderStatus.NEW
    assert sum(level.qty for level in engine.books["TEST"].bids) == before_qty


def test_disconnected_destination_is_not_rerouted_and_counts_failure(caplog):
    engine, metrics, app, reports = make_stack()
    transport_a, transport_b = register_pair(app)
    order_a = submit(engine, SESSION_A, "shared", 5.0, 99.0)
    report = next(report for report in reports if report.order_id == order_a)
    before_status = engine.orders[order_a].status

    assert app.unregister_session(SESSION_A)
    with caplog.at_level("WARNING"):
        assert not deliver(app, report)

    assert transport_a == []
    assert transport_b == []
    assert engine.orders[order_a].status == before_status
    assert metrics.snapshot().report_delivery_failures == 1
    assert any(
        getattr(record, "session_id", None) == SESSION_A
        for record in caplog.records
    )


def test_reconnect_replaces_transport_for_same_stable_session():
    engine, _, app, reports = make_stack()
    first_transport = []
    second_transport = []
    identity = SessionIdentity(SESSION_A, alias="CLIENT-A")
    app.register_session(identity, first_transport)
    app.register_session(identity, second_transport)
    order_id = submit(engine, SESSION_A, "CLIENT-A", 5.0, 99.0)
    report = next(report for report in reports if report.order_id == order_id)

    assert deliver(app, report)

    assert first_transport == []
    assert second_transport == [report]


def test_alias_collision_never_controls_routing():
    engine, _, app, reports = make_stack()
    transport_a, transport_b = register_pair(app)
    order_a = submit(engine, SESSION_A, "shared", 5.0, 99.0)
    order_b = submit(engine, SESSION_B, "shared", 7.0, 98.0)

    for report in reports:
        deliver(app, report)

    assert [report.order_id for report in transport_a] == [order_a]
    assert [report.order_id for report in transport_b] == [order_b]


def test_duplicate_cl_ord_id_is_rejected_within_session_until_reset():
    engine, _, _, _ = make_stack()
    submit(engine, SESSION_A, "CLIENT-A", 5.0, 99.0)

    with pytest.raises(ValueError, match="Duplicate ClOrdID"):
        submit(engine, SESSION_A, "CLIENT-A", 7.0, 98.0)

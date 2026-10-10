from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""Executable docs, OpenAPI, routes, and browser contract checks."""

from pathlib import Path
from copy import deepcopy
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from src.mockx.configuration import load_config
from src.mockx.models import NewOrderCommand, Side, TimeInForce

from .api_helpers import ROOT, make_exchange, process_order


def test_documented_core_routes_exist_and_openapi_generates():
    exchange = make_exchange()
    expected_routes = {
        "/healthz",
        "/readyz",
        "/stats",
        "/metrics",
        "/book/{symbol}",
        "/ws/{symbol}",
        "/ui/{symbol}",
        "/admin/advance-time",
        "/api/v1/session/start",
        "/api/v1/session/end",
    }
    actual_routes = {route.path for route in exchange.app.routes}
    assert expected_routes <= actual_routes

    with TestClient(exchange.app) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/book/{symbol}" in paths
    assert "/admin/advance-time" in paths
    assert "/api/v1/session/start" in paths
    assert "/api/v1/session/end" in paths


def test_http_order_submission_is_absent_from_routes_and_openapi():
    exchange = make_exchange()
    assert "/api/v1/orders" not in {route.path for route in exchange.app.routes}

    schema = exchange.app.openapi()
    assert "/api/v1/orders" not in schema["paths"]
    assert "SubmitOrderRequest" not in schema["components"]["schemas"]
    assert "SubmitOrderResponse" not in schema["components"]["schemas"]


@pytest.mark.parametrize(
    "body",
    [
        '{"session_id":"HTTP:test","participant_id":"http-participant",'
        '"cl_ord_id":"HTTP-ORDER","symbol":"TEST","side":"BUY",'
        '"order_type":"LIMIT","quantity":"1","price":"100"}',
        "{}",
        "{",
    ],
    ids=["numeric-strings", "missing-fields", "invalid-json"],
)
def test_http_order_requests_return_404_without_exchange_mutation(body):
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        assert client.post(
            "/api/v1/session/start", json={"run_id": "fix-only"}
        ).status_code == 200
        process_order(exchange, "RESTING", Side.SELL, 5, 101)
        exchange.engine.submit_order(
            exact_command(
                cl_ord_id="PENDING",
                symbol="TEST",
                side=Side.BUY,
                order_qty_lots=2,
                price_ticks=100,
                tif=TimeInForce.GTC,
                participant_id="pending-participant",
                submitted_at=exchange.scheduler.current_time,
            )
        )
        orders = deepcopy(exchange.engine.orders)
        client_index = dict(exchange.engine.client_order_index)
        claimed_ids = set(exchange.engine._claimed_client_order_ids)
        book = exchange.engine.books["TEST"]
        book_index = {
            order_id: (side, level.price_ticks)
            for order_id, (side, level) in book._order_index.items()
        }
        snapshot = exchange.engine.get_market_data_snapshot("TEST")
        queue = tuple(exchange.scheduler.event_queue)
        clock = exchange.scheduler.current_time
        submitted = exchange.metrics.snapshot().orders_submitted
        next_order_id = exchange.engine.next_order_id

        response = client.post(
            "/api/v1/orders",
            content=body,
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
        assert exchange.engine.orders == orders
        assert exchange.engine.client_order_index == client_index
        assert exchange.engine._claimed_client_order_ids == claimed_ids
        assert {
            order_id: (side, level.price_ticks)
            for order_id, (side, level) in book._order_index.items()
        } == book_index
        assert exchange.engine.get_market_data_snapshot("TEST") == snapshot
        assert tuple(exchange.scheduler.event_queue) == queue
        assert exchange.scheduler.current_time == clock
        assert exchange.metrics.snapshot().orders_submitted == submitted
        assert exchange.engine.next_order_id == next_order_id
        book.assert_invariants()


def test_submit_orders_help_matches_readme_flags():
    script = ROOT / "examples" / "submit_orders.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--fix-config" in result.stdout
    assert "--symbol" in result.stdout
    assert "--timeout" in result.stdout
    assert "--host" not in result.stdout
    assert "--port" not in result.stdout


def test_local_fix_example_identity_matches_local_acceptor():
    client = (ROOT / "examples" / "fix_client.cfg").read_text(
        encoding="utf-8"
    )
    acceptor = (ROOT / "configs" / "mockx1.cfg").read_text(
        encoding="utf-8"
    )

    assert "SenderCompID=CLIENT1" in client
    assert "TargetCompID=MOCKX1" in client
    assert "SenderCompID=MOCKX1" in acceptor
    assert "TargetCompID=CLIENT1" in acceptor

def test_docker_live_fix_identity_matches_live_acceptor():
    client = (ROOT / "examples" / "live_client_docker.cfg").read_text(
        encoding="utf-8"
    )
    acceptor = (ROOT / "examples" / "live_acceptor.cfg").read_text(
        encoding="utf-8"
    )

    assert "SenderCompID=liquidity_bot" in client
    assert "SenderCompID=taker_bot" in client
    assert "TargetCompID=MOCKX_DEMO" in client
    assert "SenderCompID=MOCKX_DEMO" in acceptor
    assert "TargetCompID=liquidity_bot" in acceptor
    assert "TargetCompID=taker_bot" in acceptor



def test_report_tracker_waits_for_every_expected_outcome():
    from examples.fix_support import ReportTracker

    tracker = ReportTracker(
        {
            "EXAMPLE-0001": {"CANCELLED"},
            "EXAMPLE-0002": {"CANCELLED"},
            "EXAMPLE-0003": {"NEW"},
        }
    )

    tracker.record("EXAMPLE-0001", "NEW")
    tracker.record("EXAMPLE-0001", "CANCELLED")
    tracker.record("EXAMPLE-0002", "CANCELLED")
    assert not tracker.complete
    assert not tracker.wait(0)

    tracker.record("EXAMPLE-0003", "NEW")
    assert tracker.complete
    assert tracker.wait(0)
    tracker.expect("EXAMPLE-DYNAMIC", {"NEW"})
    assert not tracker.complete
    tracker.record("EXAMPLE-DYNAMIC", "NEW")
    assert tracker.complete



def test_readme_documents_actual_routes_and_external_clock_step():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for route in (
        "/healthz",
        "/readyz",
        "/stats",
        "/metrics",
        "/book/{symbol}",
        "/ws/{symbol}",
        "/admin/advance-time",
        "/api/v1/session/start",
        "/api/v1/session/end",
    ):
        assert route in readme
    assert '"milliseconds":10' in readme
    assert "Full FIX 4.4 support" not in readme
    assert "Grafana is intentionally not included" in readme


def test_readme_documents_only_the_questdb_persistence_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    obsolete_database_name = "In" + "fluxDB"
    obsolete_config_key = "ts" + "db:"

    assert "QuestDB 9.4.3" in readme
    assert "TIMESTAMP_NS" in readme
    assert "(timestamp, run_id, metric_event_sequence)" in readme
    assert "questdb-data" in readme
    assert obsolete_database_name not in readme
    assert obsolete_config_key not in readme


def test_readme_authenticates_the_questdb_health_example():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert (
        "curl --fail --user mockx-user:mockx-password "
        "http://localhost:9003"
    ) in readme
    assert "curl -f http://localhost:9003" not in readme


def test_readme_local_config_and_example_files_exist():
    for relative in (
        "configs/mockx-local.yml",
        "configs/mockx1.cfg",
        "examples/fix_client.cfg",
        "examples/submit_orders.py",
        "examples/live_acceptor.cfg",
        "examples/live_client.cfg",
        "examples/live_client_docker.cfg",
        "examples/live_exchange.yml",
        "examples/live_exchange_docker.yml",
        "examples/live_market.py",
        "examples/cpp_sor/README.md",
        "examples/cpp_sor/CMakeLists.txt",
        "examples/cpp_sor/sor.cfg",
        "examples/cpp_sor/venue_a.yml",
        "examples/cpp_sor/venue_b.yml",
        "examples/cpp_sor/venue_a_acceptor.cfg",
        "examples/cpp_sor/venue_b_acceptor.cfg",
    ):
        assert (ROOT / relative).is_file()


def test_cpp_sor_venue_fixtures_are_distinct_but_contract_compatible():
    venue_a = load_config(str(ROOT / "examples" / "cpp_sor" / "venue_a.yml"))
    venue_b = load_config(str(ROOT / "examples" / "cpp_sor" / "venue_b.yml"))
    instrument_a = venue_a.instruments["BTC-USDT"]
    instrument_b = venue_b.instruments["BTC-USDT"]

    assert instrument_a.tick_size == instrument_b.tick_size
    assert instrument_a.lot_size == instrument_b.lot_size
    assert venue_a.admin_api.port != venue_b.admin_api.port
    assert venue_a.moldudp64.multicast_group != venue_b.moldudp64.multicast_group
    assert venue_a.moldudp64.multicast_port != venue_b.moldudp64.multicast_port
    assert venue_a.soupbintcp.port != venue_b.soupbintcp.port

    initiator = (ROOT / "examples" / "cpp_sor" / "sor.cfg").read_text(
        encoding="utf-8"
    )
    assert initiator.count("[SESSION]") == 2
    assert "VenueID=A" in initiator and "VenuePriority=0" in initiator
    assert "VenueID=B" in initiator and "VenuePriority=1" in initiator


def test_documented_in_process_session_workflow_processes_an_order():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        started = client.post(
            "/api/v1/session/start",
            json={"run_id": "quickstart-test"},
        )
        assert started.status_code == 200

        exchange.engine.submit_order(
            exact_command(
                cl_ord_id="QUICKSTART-1",
                participant_id="QUICKSTART-1",
                symbol="TEST",
                side=Side.BUY,
                order_qty_lots=2,
                price_ticks=100,
                tif=TimeInForce.GTC,
                submitted_at=exchange.scheduler.current_time,
                session_id="FIX.4.4:CLIENT->MOCKX",
                session_alias="CLIENT",
            )
        )
        advanced = client.post(
            "/admin/advance-time",
            json={"milliseconds": 10},
        )
        book = client.get("/book/TEST")
        stats = client.get("/stats")
        ended = client.post("/api/v1/session/end")

    assert advanced.status_code == 200
    assert advanced.json()["events_processed"] == 3
    assert book.json()["bids"][0]["qty"] == "2.000"
    assert stats.json()["orders"]["submitted"] == 1
    assert ended.status_code == 200
    assert ended.json()["status"] == "session_ended"

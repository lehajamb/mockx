from tests.exact_helpers import exact_command, exact_cancel_command, exact_order
"""Executable docs, OpenAPI, routes, and browser contract checks."""

from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient

from src.mockx.models import NewOrderCommand, Side, TimeInForce

from .api_helpers import ROOT, make_exchange


def test_documented_core_routes_exist_and_openapi_generates():
    exchange = make_exchange()
    expected_routes = {
        "/healthz",
        "/readyz",
        "/stats",
        "/metrics",
        "/book/{symbol}",
        "/ws/{symbol}",
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

def test_docker_fix_example_identity_matches_docker_acceptor():
    client = (ROOT / "examples" / "fix_client_docker.cfg").read_text(
        encoding="utf-8"
    )
    acceptor = (ROOT / "configs" / "mockx1-docker.cfg").read_text(
        encoding="utf-8"
    )

    assert "SenderCompID=liquidity_bot" in client
    assert "TargetCompID=MOCKX1" in client
    assert "SenderCompID=MOCKX1" in acceptor
    assert "TargetCompID=liquidity_bot" in acceptor



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


def test_readme_local_config_and_example_files_exist():
    for relative in (
        "configs/mockx-local.yml",
        "configs/mockx1.cfg",
        "examples/fix_client.cfg",
        "examples/submit_orders.py",
    ):
        "examples/fix_client_docker.cfg",
        "examples/live_acceptor.cfg",
        "examples/live_client.cfg",
        "examples/live_exchange.yml",
        "examples/live_market.py",
        assert (ROOT / relative).is_file()


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

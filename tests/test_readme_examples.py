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
            NewOrderCommand(
                cl_ord_id="QUICKSTART-1",
                symbol="TEST",
                side=Side.BUY,
                qty=2.0,
                price=100.0,
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
    assert book.json()["bids"][0]["qty"] == 2.0
    assert stats.json()["orders"]["submitted"] == 1
    assert ended.status_code == 200
    assert ended.json()["status"] == "session_ended"

"""Session metadata identifies the execution configuration actually in use."""

from copy import deepcopy
import re

from fastapi.testclient import TestClient
import pytest

from src.mockx import configuration
from tests.api_helpers import api_config, make_exchange


def fingerprint(raw):
    return configuration.execution_config_fingerprint(
        configuration.validate_config(raw, environ={}),
    )


def test_fingerprint_is_canonical_and_ignores_deployment_metadata():
    raw = api_config()
    expected = fingerprint(raw)
    reordered = dict(reversed(list(raw.items())))
    reordered["instruments"] = {
        "TEST": dict(reversed(list(raw["instruments"]["TEST"].items()))),
    }
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    assert fingerprint(reordered) == expected
    alternative = deepcopy(raw)
    alternative["instruments"]["TEST"].update(tick_size="1.0", lot_size="1.000")
    alternative["exchange_name"] = "Another deployment"
    alternative["admin_api"]["port"] += 1
    alternative["prometheus"]["enabled"] = False
    assert fingerprint(alternative) == expected


@pytest.mark.parametrize("path,value", [
    (("instruments", "TEST", "tick_size"), "0.5"),
    (("instruments", "TEST", "lot_size"), "0.5"),
    (("instruments", "TEST", "price_display_precision"), 3),
    (("latency", "engine_ms"), 1),
    (("latency", "session_delays"), {"CLIENT1": 1}),
    (("execution", "reject_prob"), 0.5),
    (("execution", "random_seed"), 43),
    (("scheduler", "interval_ms"), 20),
    (("fix", "enabled"), True),
    (("fix", "session_participants", "FIX.4.4:MOCKX1->CLIENT1"), "other"),
])
def test_fingerprint_changes_with_execution_settings(path, value):
    raw = api_config()
    changed = deepcopy(raw)
    target = changed
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert fingerprint(changed) != fingerprint(raw)


def test_start_response_identifies_server_execution_configuration():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        response = client.post("/api/v1/session/start", json={"run_id": "fingerprint"})
        assert response.status_code == 200, response.text
        expected = fingerprint(api_config())
        assert response.json()["execution_config_fingerprint"] == expected
        client.post("/api/v1/session/end").raise_for_status()
        repeated = client.post("/api/v1/session/start", json={"run_id": "fingerprint-again"})
        assert repeated.json()["execution_config_fingerprint"] == expected

"""Administrative simulated-time validation and inclusive semantics."""

from datetime import timedelta

from fastapi.testclient import TestClient

from .api_helpers import make_exchange


def test_advance_time_processes_target_timestamp_inclusively():
    exchange = make_exchange()
    handled = []
    exchange.scheduler.register_handler("probe", handled.append)
    exchange.scheduler.schedule_event("probe", "before", 1)
    exchange.scheduler.schedule_event("probe", "target", 2)
    exchange.scheduler.schedule_event("probe", "after", 3)
    target = exchange.scheduler.current_time + timedelta(milliseconds=2)

    with TestClient(exchange.app) as client:
        response = client.post(
            "/admin/advance-time",
            json={"target_time": target.isoformat()},
        )

    assert response.status_code == 200
    assert handled == ["before", "target"]
    assert response.json()["events_processed"] == 2
    assert response.json()["pending_events"] == 1
    assert response.json()["current_time"].endswith("Z")


def test_aware_offsets_normalize_to_utc_and_naive_time_is_rejected():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        normalized = client.post(
            "/admin/advance-time",
            json={"target_time": "1970-01-01T01:00:00+01:00"},
        )
        naive = client.post(
            "/admin/advance-time",
            json={"target_time": "1970-01-01T00:00:01"},
        )

    assert normalized.status_code == 200
    assert normalized.json()["current_time"] == "1970-01-01T00:00:00Z"
    assert naive.status_code == 422


def test_backward_movement_returns_stable_conflict_without_mutation():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        assert client.post(
            "/admin/advance-time",
            json={"milliseconds": 10},
        ).status_code == 200
        current = exchange.scheduler.current_time
        response = client.post(
            "/admin/advance-time",
            json={"target_time": "1970-01-01T00:00:00+00:00"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "TIME_REVERSAL"
    assert exchange.scheduler.current_time == current


def test_empty_relative_advancement_moves_clock_and_reports_zero_events():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        response = client.post(
            "/admin/advance-time",
            json={"milliseconds": 25},
        )

    assert response.status_code == 200
    assert response.json()["events_processed"] == 0
    assert response.json()["pending_events"] == 0
    assert response.json()["current_time"] == (
        "1970-01-01T00:00:00.025000Z"
    )


def test_invalid_relative_values_are_validation_errors():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        zero = client.post(
            "/admin/advance-time",
            json={"milliseconds": 0},
        )
        ambiguous = client.post(
            "/admin/advance-time",
            json={
                "milliseconds": 1,
                "target_time": "1970-01-01T00:00:00+00:00",
            },
        )

    assert zero.status_code == 422
    assert ambiguous.status_code == 422

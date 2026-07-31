"""Compose/config contracts without requiring a Docker daemon."""

from pathlib import Path

import pytest
import yaml

from src.mockx.cli import load_config

from .api_helpers import ROOT


def load_compose():
    return yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )


def published_ports(service):
    pairs = []
    for value in service.get("ports", []):
        host, container = str(value).split(":")
        pairs.append((int(host), int(container)))
    return pairs


def test_compose_has_unique_real_service_ports_and_readiness_checks():
    compose = load_compose()
    services = compose["services"]
    assert set(services) == {"influxdb", "mockx", "live-market"}

    all_host_ports = [
        host
        for service in services.values()
        for host, _ in published_ports(service)
    ]
    assert len(all_host_ports) == len(set(all_host_ports))
    assert published_ports(services["mockx"]) == [
        (4001, 4001),
        (8001, 8000),
    ]
    assert published_ports(services["influxdb"]) == [(8086, 8086)]
    assert published_ports(services["live-market"]) == []

    mockx_health = " ".join(services["mockx"]["healthcheck"]["test"])
    influx_health = " ".join(
        services["influxdb"]["healthcheck"]["test"]
    )
    assert "http://localhost:8000/readyz" in mockx_health
    assert "influx ping" in influx_health
    assert services["mockx"]["depends_on"]["influxdb"]["condition"] == (
        "service_healthy"
    )
    assert services["live-market"]["depends_on"]["mockx"]["condition"] == (
        "service_healthy"
    )


def test_compose_runs_live_market_with_container_and_public_addresses():
    services = load_compose()["services"]
    exchange = services["mockx"]
    driver = services["live-market"]

    assert exchange["environment"]["CFG_PATH"] == (
        "/opt/mockx/examples/live_exchange_docker.yml"
    )
    assert exchange["environment"]["FIX_CONFIG_FILE"] == (
        "/opt/mockx/examples/live_acceptor.cfg"
    )
    assert driver["entrypoint"] == ["python", "examples/live_market.py"]
    assert driver["image"] == exchange["image"]
    assert "build" not in driver
    assert driver["pull_policy"] == "never"
    assert driver["command"] == [
        "--config",
        "examples/live_exchange_docker.yml",
        "--fix-config",
        "examples/live_client_docker.cfg",
        "--base-url",
        "http://mockx:8000",
        "--ui-base-url",
        "http://localhost:8001",
    ]
    assert driver["init"] is True
    assert driver["stop_signal"] == "SIGTERM"
    assert driver["environment"]["PYTHONUNBUFFERED"] == "1"


def test_docker_image_contains_live_demo_and_ignores_runtime_artifacts():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "COPY ./examples ./examples" in dockerfile
    assert "COPY ./configs/FIX44.xml ./configs/FIX44.xml" in dockerfile
    for ignored in (".git", ".pytest_cache", "logs", "store", "tests"):
        assert ignored in dockerignore


def test_docker_live_config_loads_with_compose_influx_environment():
    compose = load_compose()
    environ = {
        key: str(value)
        for key, value in compose["services"]["mockx"]["environment"].items()
    }
    config = load_config(
        str(ROOT / "examples" / "live_exchange_docker.yml"),
        environ=environ,
    )

    assert config.admin_api.port == 8000
    assert config.admin_api.host == "0.0.0.0"
    assert config.scheduler.mode == "external"
    assert config.scheduler.max_events_per_advance == 100_000
    assert config.tsdb is not None
    assert config.tsdb.enabled
    assert config.tsdb.url == "http://influxdb:8086"
    assert config.tsdb.bucket == "mockx"


def test_compose_persists_influx_data_in_named_volume():
    compose = load_compose()
    influxdb = compose["services"]["influxdb"]

    assert "influxdb-data:/var/lib/influxdb2" in influxdb["volumes"]
    assert "influxdb-data" in compose["volumes"]


def test_scheduler_processing_limit_defaults_and_validates(tmp_path):
    source = yaml.safe_load(
        (ROOT / "configs" / "mockx-local.yml").read_text(encoding="utf-8")
    )
    source["scheduler"].pop("max_events_per_advance")
    default_path = tmp_path / "default.yml"
    default_path.write_text(yaml.safe_dump(source), encoding="utf-8")

    assert (
        load_config(str(default_path)).scheduler.max_events_per_advance
        == 100_000
    )

    source["scheduler"]["max_events_per_advance"] = 0
    invalid_path = tmp_path / "invalid.yml"
    invalid_path.write_text(yaml.safe_dump(source), encoding="utf-8")
    with pytest.raises(
        ValueError,
        match="scheduler.max_events_per_advance:",
    ):
        load_config(str(invalid_path))


def test_execution_random_seed_is_required_and_must_be_an_integer(tmp_path):
    source = yaml.safe_load(
        (ROOT / "configs" / "mockx-local.yml").read_text(encoding="utf-8")
    )
    source["execution"].pop("random_seed")
    missing_path = tmp_path / "missing-seed.yml"
    missing_path.write_text(yaml.safe_dump(source), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="execution.random_seed: Field required",
    ):
        load_config(str(missing_path))

    source["execution"]["random_seed"] = True
    invalid_path = tmp_path / "invalid-seed.yml"
    invalid_path.write_text(yaml.safe_dump(source), encoding="utf-8")
    with pytest.raises(
        ValueError,
        match="execution.random_seed:",
    ):
        load_config(str(invalid_path))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "outbound_queue_capacity",
            0,
            "websocket.outbound_queue_capacity:",
        ),
        (
            "send_timeout_seconds",
            0,
            "websocket.send_timeout_seconds:",
        ),
        (
            "heartbeat_stale_seconds",
            10,
            "websocket: heartbeat_stale_seconds must exceed heartbeat cadence",
        ),
        (
            "reconnect_initial_seconds",
            0,
            "websocket.reconnect_initial_seconds:",
        ),
        (
            "reconnect_max_seconds",
            0.5,
            "websocket: reconnect_max_seconds must be at least reconnect_initial_seconds",
        ),
        (
            "staleness_check_seconds",
            0,
            "websocket.staleness_check_seconds:",
        ),
    ],
)
def test_websocket_delivery_and_browser_timing_validation(
    tmp_path,
    field,
    value,
    message,
):
    source = yaml.safe_load(
        (ROOT / "configs" / "mockx-local.yml").read_text(encoding="utf-8")
    )
    source["websocket"][field] = value
    if field == "reconnect_max_seconds":
        source["websocket"]["reconnect_initial_seconds"] = 1
    config_path = tmp_path / f"invalid-{field}.yml"
    config_path.write_text(yaml.safe_dump(source), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_config(str(config_path))


def test_monitoring_documentation_matches_deployed_assets():
    compose = load_compose()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "prometheus" not in compose["services"]
    assert "grafana" not in compose["services"]
    assert "separate metrics server or metrics host port" in readme
    assert "Grafana is intentionally not included" in readme
    assert "mockx:8000/metrics" in readme
    assert "mockx1:8000/metrics" not in readme
    assert "mockx2:8000/metrics" not in readme

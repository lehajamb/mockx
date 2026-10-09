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
        address, _, protocol = str(value).partition("/")
        parts = address.rsplit(":", 2)
        if len(parts) == 2:
            host, container = parts
            bind = "0.0.0.0"
        else:
            bind, host, container = parts
        pairs.append((bind, int(host), int(container), protocol or "tcp"))
    return pairs


def test_compose_has_unique_real_service_ports_and_readiness_checks():
    compose = load_compose()
    services = compose["services"]
    assert set(services) == {
        "questdb", "mockx", "mold-host-relay", "live-market"
    }

    all_host_ports = [
        (host, protocol)
        for service in services.values()
        for _, host, _, protocol in published_ports(service)
    ]
    assert len(all_host_ports) == len(set(all_host_ports))
    assert published_ports(services["mockx"]) == [
        ("0.0.0.0", 4001, 4001, "tcp"),
        ("0.0.0.0", 8001, 8000, "tcp"),
        ("127.0.0.1", 18001, 18001, "tcp"),
    ]
    assert published_ports(services["questdb"]) == [
        ("0.0.0.0", 9000, 9000, "tcp"),
        ("0.0.0.0", 8812, 8812, "tcp"),
        ("0.0.0.0", 9003, 9003, "tcp"),
    ]
    assert published_ports(services["live-market"]) == []
    relay = services["mold-host-relay"]
    assert published_ports(relay) == []
    assert relay["image"] == services["mockx"]["image"]
    assert relay["entrypoint"] == ["python", "examples/moldudp64_host_relay.py"]
    assert relay["command"] == [
        "--multicast-group", "239.192.0.1", "--multicast-port", "18000",
        "--target-host", "host.docker.internal", "--target-port", "18000",
    ]
    assert relay["depends_on"]["mockx"]["condition"] == "service_healthy"
    assert services["live-market"]["depends_on"]["mold-host-relay"]["condition"] == (
        "service_started"
    )

    mockx_health = " ".join(services["mockx"]["healthcheck"]["test"])
    questdb_health = " ".join(
        services["questdb"]["healthcheck"]["test"]
    )
    assert "http://localhost:8000/readyz" in mockx_health
    assert "http://localhost:9003" in questdb_health
    assert services["questdb"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert (
        '--user "$${QDB_HTTP_USER}:$${QDB_HTTP_PASSWORD}"'
        in questdb_health
    )
    assert services["mockx"]["depends_on"]["questdb"]["condition"] == (
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


def test_docker_live_config_loads_with_compose_questdb_environment():
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
    assert config.questdb is not None
    assert config.questdb.enabled
    assert config.questdb.url == "http://questdb:9000"
    assert config.questdb.health_url == "http://questdb:9003"
    assert config.questdb.username == "mockx-user"
    assert config.questdb.password == "mockx-password"


def test_compose_persists_questdb_data_in_named_volume():
    compose = load_compose()
    questdb = compose["services"]["questdb"]

    assert "questdb-data:/var/lib/questdb" in questdb["volumes"]
    assert "questdb-data" in compose["volumes"]


def test_compose_pins_questdb_and_disables_schema_auto_creation():
    questdb = load_compose()["services"]["questdb"]

    assert questdb["image"] == "questdb/questdb:9.4.3"
    assert questdb["environment"] == {
        "QDB_HTTP_USER": "mockx-user",
        "QDB_HTTP_PASSWORD": "mockx-password",
        "QDB_PG_USER": "mockx-user",
        "QDB_PG_PASSWORD": "mockx-password",
        "QDB_LINE_AUTO_CREATE_NEW_TABLES": "false",
        "QDB_LINE_AUTO_CREATE_NEW_COLUMNS": "false",
    }


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

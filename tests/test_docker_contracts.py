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
    assert set(services) == {"mockx1", "mockx2", "influxdb"}

    all_host_ports = [
        host
        for service in services.values()
        for host, _ in published_ports(service)
    ]
    assert len(all_host_ports) == len(set(all_host_ports))
    assert published_ports(services["mockx1"]) == [
        (4001, 4001),
        (8001, 8000),
    ]
    assert published_ports(services["mockx2"]) == [
        (4002, 4002),
        (8002, 8000),
    ]
    for name in ("mockx1", "mockx2"):
        command = " ".join(services[name]["healthcheck"]["test"])
        assert "http://localhost:8000/readyz" in command


def test_compose_mounts_required_config_and_dictionary_files():
    services = load_compose()["services"]
    for name, number in (("mockx1", "1"), ("mockx2", "2")):
        volumes = "\n".join(services[name]["volumes"])
        assert f"./configs/mockx{number}.yml:" in volumes
        assert f"./configs/mockx{number}-docker.cfg:" in volumes
        assert "./configs/FIX44.xml:" in volumes


def test_instance_yaml_contracts_load_and_match_internal_api_port():
    first = load_config(str(ROOT / "configs" / "mockx1.yml"))
    second = load_config(str(ROOT / "configs" / "mockx2.yml"))

    assert first["admin_api"]["port"] == 8000
    assert second["admin_api"]["port"] == 8000
    assert first["admin_api"]["host"] == "0.0.0.0"
    assert second["admin_api"]["host"] == "0.0.0.0"
    assert first["scheduler"]["mode"] == "external"
    assert second["scheduler"]["mode"] == "external"
    assert first["scheduler"]["max_events_per_advance"] == 100_000
    assert second["scheduler"]["max_events_per_advance"] == 100_000
    for config in (first, second):
        assert config["websocket"]["outbound_queue_capacity"] == 8
        assert config["websocket"]["send_timeout_seconds"] == 5
        assert config["websocket"]["heartbeat_stale_seconds"] == 30
        assert config["websocket"]["reconnect_initial_seconds"] == 1
        assert config["websocket"]["reconnect_max_seconds"] == 30
        assert config["websocket"]["staleness_check_seconds"] == 1
    assert first["tsdb"]["bucket"] == second["tsdb"]["bucket"]


def test_scheduler_processing_limit_defaults_and_validates(tmp_path):
    source = yaml.safe_load(
        (ROOT / "configs" / "mockx-local.yml").read_text(encoding="utf-8")
    )
    source["scheduler"].pop("max_events_per_advance")
    default_path = tmp_path / "default.yml"
    default_path.write_text(yaml.safe_dump(source), encoding="utf-8")

    assert (
        load_config(str(default_path))["scheduler"][
            "max_events_per_advance"
        ]
        == 100_000
    )

    source["scheduler"]["max_events_per_advance"] = 0
    invalid_path = tmp_path / "invalid.yml"
    invalid_path.write_text(yaml.safe_dump(source), encoding="utf-8")
    with pytest.raises(
        ValueError,
        match="max_events_per_advance must be a positive integer",
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
        match="Missing required configuration field: execution.random_seed",
    ):
        load_config(str(missing_path))

    source["execution"]["random_seed"] = True
    invalid_path = tmp_path / "invalid-seed.yml"
    invalid_path.write_text(yaml.safe_dump(source), encoding="utf-8")
    with pytest.raises(
        ValueError,
        match="execution.random_seed must be an integer",
    ):
        load_config(str(invalid_path))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "outbound_queue_capacity",
            0,
            "websocket.outbound_queue_capacity must be a positive integer",
        ),
        (
            "send_timeout_seconds",
            0,
            "websocket.send_timeout_seconds must be positive",
        ),
        (
            "heartbeat_stale_seconds",
            10,
            "websocket.heartbeat_stale_seconds must exceed heartbeat cadence",
        ),
        (
            "reconnect_initial_seconds",
            0,
            "websocket.reconnect_initial_seconds must be positive",
        ),
        (
            "reconnect_max_seconds",
            0.5,
            "websocket.reconnect_max_seconds must be at least reconnect_initial_seconds",
        ),
        (
            "staleness_check_seconds",
            0,
            "websocket.staleness_check_seconds must be positive",
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

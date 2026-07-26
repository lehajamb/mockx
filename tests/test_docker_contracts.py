"""Compose/config contracts without requiring a Docker daemon."""

from pathlib import Path

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
    assert first["tsdb"]["bucket"] == second["tsdb"]["bucket"]


def test_monitoring_documentation_matches_deployed_assets():
    compose = load_compose()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "prometheus" not in compose["services"]
    assert "grafana" not in compose["services"]
    assert "separate metrics server or metrics host port" in readme
    assert "Grafana is intentionally not included" in readme

"""Contracts for the Dockerized C++ SOR demo."""

from __future__ import annotations

import importlib.util
import sys

import pytest
import yaml

from src.mockx.configuration import load_config

from .api_helpers import ROOT


SOR_ROOT = ROOT / "examples" / "cpp_sor"


def load_compose() -> dict:
    path = SOR_ROOT / "docker-compose.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_seed_module():
    path = SOR_ROOT / "seed_books.py"
    spec = importlib.util.spec_from_file_location("cpp_sor_seed_books", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cpp_sor_compose_wires_two_venues_relays_and_seed_service():
    services = load_compose()["services"]
    assert set(services) == {
        "venue-a",
        "venue-b",
        "venue-a-mold-relay",
        "venue-b-mold-relay",
        "seed-books",
        "questdb", "dashboard-projector", "grafana",
    }

    venue_a = services["venue-a"]
    venue_b = services["venue-b"]
    assert venue_a["build"]["context"] == "../.."
    assert venue_a["image"] == venue_b["image"]
    assert venue_b["pull_policy"] == "never"
    assert venue_a["ports"] == ["4101:4101", "8101:8101", "18101:18101"]
    assert venue_b["ports"] == ["4102:4102", "8102:8102", "18201:18201"]
    assert venue_a["environment"] == {
        "CFG_PATH": "/opt/mockx/examples/cpp_sor/venue_a_docker.yml",
        "FIX_CONFIG_FILE": "/opt/mockx/examples/cpp_sor/venue_a_acceptor.cfg",
    }
    assert venue_b["environment"] == {
        "CFG_PATH": "/opt/mockx/examples/cpp_sor/venue_b_docker.yml",
        "FIX_CONFIG_FILE": "/opt/mockx/examples/cpp_sor/venue_b_acceptor.cfg",
    }
    venue_a_health = " ".join(venue_a["healthcheck"]["test"])
    venue_b_health = " ".join(venue_b["healthcheck"]["test"])
    assert "http://localhost:8101/readyz" in venue_a_health
    assert "http://localhost:8102/readyz" in venue_b_health

    relay_cases = (
        ("venue-a-mold-relay", "venue-a", "239.192.10.1", "18100"),
        ("venue-b-mold-relay", "venue-b", "239.192.10.2", "18200"),
    )
    for service_name, venue_name, group, port in relay_cases:
        relay = services[service_name]
        assert relay["image"] == venue_a["image"]
        assert relay["pull_policy"] == "never"
        assert relay["entrypoint"] == [
            "python",
            "examples/moldudp64_host_relay.py",
        ]
        assert relay["command"] == [
            "--multicast-group",
            group,
            "--multicast-port",
            port,
            "--target-host",
            "host.docker.internal",
            "--target-port",
            port,
        ]
        condition = relay["depends_on"][venue_name]["condition"]
        assert condition == "service_healthy"
        assert relay["init"] is True
        assert relay["restart"] == "on-failure"

    seed = services["seed-books"]
    assert seed["image"] == venue_a["image"]
    assert seed["pull_policy"] == "never"
    assert seed["entrypoint"] == ["python", "examples/cpp_sor/seed_books.py"]
    assert seed["command"] == [
        "--fix-config",
        "examples/cpp_sor/seed_books_docker.cfg",
        "--venue-a-url",
        "http://venue-a:8101",
        "--venue-b-url",
        "http://venue-b:8102",
    ]
    assert seed["depends_on"] == {
        "venue-a": {"condition": "service_healthy"},
        "venue-b": {"condition": "service_healthy"},
        "venue-a-mold-relay": {"condition": "service_started"},
        "venue-b-mold-relay": {"condition": "service_started"},
    }
    assert seed["init"] is True


def test_cpp_sor_docker_configs_bind_published_interfaces():
    venue_a = load_config(str(SOR_ROOT / "venue_a_docker.yml"))
    venue_b = load_config(str(SOR_ROOT / "venue_b_docker.yml"))

    assert venue_a.admin_api.host == venue_b.admin_api.host == "0.0.0.0"
    assert venue_a.admin_api.port == 8101
    assert venue_b.admin_api.port == 8102
    assert venue_a.soupbintcp is not None
    assert venue_b.soupbintcp is not None
    assert venue_a.soupbintcp.host == venue_b.soupbintcp.host == "0.0.0.0"
    assert venue_a.soupbintcp.port == 18101
    assert venue_b.soupbintcp.port == 18201
    assert venue_a.moldudp64.multicast_group == "239.192.10.1"
    assert venue_b.moldudp64.multicast_group == "239.192.10.2"
    assert venue_a.moldudp64.multicast_port == 18100
    assert venue_b.moldudp64.multicast_port == 18200


def test_cpp_sor_seed_fix_sessions_have_distinct_passive_participants():
    expected_mappings = {
        "venue_a.yml": {
            "FIX.4.4:MOCKX_A->SOR_A": "sor-demo",
            "FIX.4.4:MOCKX_A->SEED_A": "passive-a",
        },
        "venue_a_docker.yml": {
            "FIX.4.4:MOCKX_A->SOR_A": "sor-demo",
            "FIX.4.4:MOCKX_A->SEED_A": "passive-a",
        },
        "venue_b.yml": {
            "FIX.4.4:MOCKX_B->SOR_B": "sor-demo",
            "FIX.4.4:MOCKX_B->SEED_B": "passive-b",
        },
        "venue_b_docker.yml": {
            "FIX.4.4:MOCKX_B->SOR_B": "sor-demo",
            "FIX.4.4:MOCKX_B->SEED_B": "passive-b",
        },
    }
    for filename, mappings in expected_mappings.items():
        config = load_config(str(SOR_ROOT / filename))
        assert config.fix.session_participants == mappings

    acceptor_a_path = SOR_ROOT / "venue_a_acceptor.cfg"
    acceptor_b_path = SOR_ROOT / "venue_b_acceptor.cfg"
    acceptor_a = acceptor_a_path.read_text(encoding="utf-8")
    acceptor_b = acceptor_b_path.read_text(encoding="utf-8")
    assert acceptor_a.count("[SESSION]") == 2
    assert "TargetCompID=SOR_A" in acceptor_a
    assert "TargetCompID=SEED_A" in acceptor_a
    assert acceptor_b.count("[SESSION]") == 2
    assert "TargetCompID=SOR_B" in acceptor_b
    assert "TargetCompID=SEED_B" in acceptor_b


def test_cpp_sor_seed_initiator_configs_target_host_and_docker_venues():
    host = (SOR_ROOT / "seed_books.cfg").read_text(encoding="utf-8")
    docker = (SOR_ROOT / "seed_books_docker.cfg").read_text(encoding="utf-8")

    for config in (host, docker):
        assert config.count("[SESSION]") == 2
        assert "SenderCompID=SEED_A" in config
        assert "TargetCompID=MOCKX_A" in config
        assert "SocketConnectPort=4101" in config
        assert "SenderCompID=SEED_B" in config
        assert "TargetCompID=MOCKX_B" in config
        assert "SocketConnectPort=4102" in config
        assert "ScreenLogShowIncoming=N" in config
        assert "ScreenLogShowOutgoing=N" in config
        assert "ScreenLogShowEvents=N" in config
        assert "FileStorePath" not in config
        assert "FileLogPath" not in config
    assert host.count("SocketConnectHost=127.0.0.1") == 2
    assert "SocketConnectHost=venue-a" in docker
    assert "SocketConnectHost=venue-b" in docker
    assert "MemoryStoreFactory" in (SOR_ROOT / "seed_books.py").read_text(
        encoding="utf-8"
    )


class FakeHttpClient:
    def __init__(
        self,
        books: dict[str, dict],
        events: list[tuple],
    ) -> None:
        self.books = books
        self.events = events
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(
        self,
        method: str,
        url: str,
        payload: dict | None = None,
    ) -> dict:
        self.calls.append((method, url, payload))
        self.events.append(("http", method, url, payload))
        if url.endswith("/readyz"):
            return {"traffic_ready": True}
        if url.endswith("/api/v1/session/start"):
            generation = 7 if "venue-a" in url else 9
            return {
                "session_id": payload["scenario_label"],
                "run_generation": generation,
            }
        if url.endswith("/book/BTC-USDT"):
            return self.books[url]
        return {"status": "ok"}


class FakeFixDriver:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events
        self.orders = ()
        self.stopped = False

    def start(self) -> None:
        self.events.append(("fix", "start"))

    def wait_for_sessions(self, roles, timeout: float) -> bool:
        self.events.append(("fix", "wait_for_sessions", tuple(roles), timeout))
        return True

    def submit(self, orders) -> None:
        self.orders = tuple(orders)
        self.events.append(("fix", "submit", self.orders))

    def wait_for_new(self, cl_ord_ids, timeout: float) -> None:
        self.events.append(("fix", "wait_for_new", tuple(cl_ord_ids), timeout))

    def stop(self) -> None:
        self.stopped = True
        self.events.append(("fix", "stop"))


def test_seed_books_resets_starts_submits_over_fix_and_verifies_depth():
    seed_books = load_seed_module()
    venues = seed_books.venue_specs(
        "http://venue-a:8101",
        "http://venue-b:8102",
    )
    books = {
        f"{venue.base_url}/book/BTC-USDT": {
            "bids": [],
            "asks": [
                {"price": price, "qty": qty, "order_count": count}
                for price, qty, count in venue.expected_asks
            ],
        }
        for venue in venues
    }
    events = []
    client = FakeHttpClient(books, events)
    fix_driver = FakeFixDriver(events)

    seed_books.bootstrap(
        venues,
        client,
        fix_driver,
        timeout_seconds=1,
        poll_interval_seconds=0,
        sleep=lambda _: None,
    )

    expected_calls = [
        ("GET", "http://venue-a:8101/readyz", None),
        ("GET", "http://venue-b:8102/readyz", None),
        ("POST", "http://venue-a:8101/api/v1/session/end", None),
        ("POST", "http://venue-b:8102/api/v1/session/end", None),
        (
            "POST",
            "http://venue-a:8101/api/v1/session/start",
            {"scenario_label": "sor-a"},
        ),
        (
            "POST",
            "http://venue-b:8102/api/v1/session/start",
            {"scenario_label": "sor-b"},
        ),
    ]
    expected_calls.extend(
        ("GET", f"{venue.base_url}/book/BTC-USDT", None) for venue in venues
    )
    assert client.calls == expected_calls
    assert fix_driver.stopped
    assert fix_driver.orders == (
        seed_books.FixOrder(
            role="SEED_A",
            cl_ord_id="SEED-A-G00000007-100",
            symbol="BTC-USDT",
            side="SELL",
            quantity="0.002",
            price="100.00",
            time_in_force="GTC",
        ),
        seed_books.FixOrder(
            role="SEED_A",
            cl_ord_id="SEED-A-G00000007-102",
            symbol="BTC-USDT",
            side="SELL",
            quantity="0.009",
            price="102.00",
            time_in_force="GTC",
        ),
        seed_books.FixOrder(
            role="SEED_B",
            cl_ord_id="SEED-B-G00000009-99",
            symbol="BTC-USDT",
            side="SELL",
            quantity="0.003",
            price="99.00",
            time_in_force="GTC",
        ),
        seed_books.FixOrder(
            role="SEED_B",
            cl_ord_id="SEED-B-G00000009-100",
            symbol="BTC-USDT",
            side="SELL",
            quantity="0.001",
            price="100.00",
            time_in_force="GTC",
        ),
    )
    assert events[:8] == [
        ("http", "GET", "http://venue-a:8101/readyz", None),
        ("http", "GET", "http://venue-b:8102/readyz", None),
        ("http", "POST", "http://venue-a:8101/api/v1/session/end", None),
        ("http", "POST", "http://venue-b:8102/api/v1/session/end", None),
        (
            "http",
            "POST",
            "http://venue-a:8101/api/v1/session/start",
            {"scenario_label": "sor-a"},
        ),
        (
            "http",
            "POST",
            "http://venue-b:8102/api/v1/session/start",
            {"scenario_label": "sor-b"},
        ),
        ("fix", "start"),
        ("fix", "wait_for_sessions", ("SEED_A", "SEED_B"), 1),
    ]
    assert events[-1] == ("fix", "stop")


def test_quickfix_seed_driver_sends_exact_limit_order_fields(monkeypatch):
    seed_books = load_seed_module()
    order = seed_books.FixOrder(
        role="SEED_A",
        cl_ord_id="SEED-A-G00000001-100",
        symbol="BTC-USDT",
        side="SELL",
        quantity="0.002",
        price="100.00",
        time_in_force="GTC",
    )
    calls = []

    class Application:
        @staticmethod
        def session(role):
            return f"session:{role}"

    class Reports:
        @staticmethod
        def expect(cl_ord_ids):
            calls.append(("expect", tuple(cl_ord_ids)))

    driver = object.__new__(seed_books.QuickFixSeedDriver)
    driver.application = Application()
    driver.reports = Reports()

    def fake_send(session, **fields):
        calls.append(("send", session, fields))

    monkeypatch.setattr(seed_books, "send_limit_order", fake_send)
    driver.submit((order,))

    assert calls == [
        ("expect", (order.cl_ord_id,)),
        (
            "send",
            "session:SEED_A",
            {
                "cl_ord_id": order.cl_ord_id,
                "symbol": "BTC-USDT",
                "side": seed_books.fix.Side_SELL,
                "quantity": "0.002",
                "price": "100.00",
                "tif": seed_books.fix.TimeInForce_GOOD_TILL_CANCEL,
            },
        ),
    ]


def test_seed_books_stops_fix_driver_when_logon_times_out():
    seed_books = load_seed_module()
    venues = seed_books.venue_specs(
        "http://venue-a:8101",
        "http://venue-b:8102",
    )
    events = []

    class LogonTimeoutDriver(FakeFixDriver):
        def wait_for_sessions(self, roles, timeout):
            super().wait_for_sessions(roles, timeout)
            return False

    driver = LogonTimeoutDriver(events)
    with pytest.raises(seed_books.BootstrapError, match="FIX logon timed out"):
        seed_books.bootstrap(
            venues,
            FakeHttpClient({}, events),
            driver,
            timeout_seconds=1,
            poll_interval_seconds=0,
            sleep=lambda _: None,
        )
    assert driver.stopped


def test_seed_books_propagates_fix_rejections_and_stops_driver():
    seed_books = load_seed_module()
    venues = seed_books.venue_specs(
        "http://venue-a:8101",
        "http://venue-b:8102",
    )
    events = []

    class RejectingDriver(FakeFixDriver):
        def wait_for_new(self, cl_ord_ids, timeout):
            super().wait_for_new(cl_ord_ids, timeout)
            raise seed_books.BootstrapError("seed order rejected")

    driver = RejectingDriver(events)
    with pytest.raises(seed_books.BootstrapError, match="seed order rejected"):
        seed_books.bootstrap(
            venues,
            FakeHttpClient({}, events),
            driver,
            timeout_seconds=1,
            poll_interval_seconds=0,
            sleep=lambda _: None,
        )
    assert driver.stopped


def test_seed_books_times_out_with_the_unexpected_book_in_the_error():
    seed_books = load_seed_module()
    venues = seed_books.venue_specs(
        "http://venue-a:8101",
        "http://venue-b:8102",
    )
    events = []
    client = FakeHttpClient(
        {
            "http://venue-a:8101/book/BTC-USDT": {"bids": [], "asks": []},
        },
        events,
    )
    driver = FakeFixDriver(events)
    clock = iter((0.0, 2.0))

    with pytest.raises(
        seed_books.BootstrapError,
        match=r"timed out waiting for Venue A seeded book: last response was",
    ):
        seed_books.bootstrap(
            venues,
            client,
            driver,
            timeout_seconds=1,
            poll_interval_seconds=0,
            monotonic=clock.__next__,
            sleep=lambda _: None,
        )
    assert driver.stopped


def test_cpp_sor_seeding_has_no_rest_order_ingress():
    source = (SOR_ROOT / "seed_books.py").read_text(encoding="utf-8")
    readme = (SOR_ROOT / "README.md").read_text(encoding="utf-8")

    assert "/api/v1/orders" not in source
    assert "/api/v1/orders" not in readme

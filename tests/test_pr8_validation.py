"""PR 8A acceptance tests for strict startup and order boundaries."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import asyncio

import pytest
import quickfix as fix
import yaml
from fastapi.testclient import TestClient

from src.mockx.configuration import ConfigurationError, validate_config
from src.mockx.gateway import configured_fix_session_ids
from src.mockx.models import ExecutionReport, OrderStatus, SchedulerState
from src.mockx.validation import (
    OrderNormalizer,
    OrderValidationCode,
    OrderValidationError,
    RawOrderInput,
)
from src.mockx.instruments import InstrumentCatalog
from src.mockx.main import MockExchange

from .api_helpers import make_exchange
from .test_fix_ingress_pipeline import _exchange, _new_order, _start, _stop


ROOT = Path(__file__).resolve().parents[1]


def valid_config() -> dict:
    return {
        "exchange_name": "Acceptance",
        "instruments": {
            "BTC-USDT": {
                "tick_size": "0.01",
                "lot_size": "0.001",
                "price_display_precision": 2,
                "quantity_display_precision": 3,
            }
        },
        "latency": {
            "network_ms": 0,
            "engine_ms": 0,
            "session_delays": {},
        },
        "execution": {"reject_prob": 0.0, "random_seed": 42},
        "admin_api": {
            "host": "127.0.0.1",
            "port": 8000,
            "enabled": True,
        },
        "fix": {"enabled": False, "session_participants": {}},
        "scheduler": {
            "mode": "external",
            "interval_ms": 10,
            "max_advance_ms": 86_400_000,
            "max_events_per_advance": 100_000,
        },
        "websocket": {
            "heartbeat_interval_seconds": 10.0,
            "heartbeat_stale_seconds": 30.0,
            "market_data_stale_seconds": 30.0,
            "fallback_poll_seconds": 2.0,
            "outbound_queue_capacity": 8,
            "send_timeout_seconds": 5.0,
        },
        "cors_origins": [],
        "prometheus": {"enabled": True},
        "tsdb": {"enabled": False},
    }


def set_path(config: dict, path: str, value) -> dict:
    current = config
    parts = path.split(".")
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value
    return config


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("execution.reject_prob", -0.001),
        ("execution.reject_prob", 1.001),
        ("execution.random_seed", 1.5),
        ("latency.network_ms", -1),
        ("latency.engine_ms", -1),
        ("scheduler.mode", "batch"),
        ("scheduler.interval_ms", 0),
        ("scheduler.max_advance_ms", 86_400_001),
        ("scheduler.max_events_per_advance", 0),
        ("websocket.outbound_queue_capacity", 0),
        ("websocket.send_timeout_seconds", 0),
        ("admin_api.port", 65_536),
        ("admin_api.enabled", "true"),
    ],
)
def test_invalid_configuration_bounds_fail_at_stable_path(path, value):
    with pytest.raises(ConfigurationError) as caught:
        validate_config(set_path(valid_config(), path, value), environ={})

    assert caught.value.issues[0].path == path


def test_negative_session_latency_is_rejected():
    raw = valid_config()
    raw["latency"]["session_delays"] = {"CLIENT": -1}

    with pytest.raises(ConfigurationError, match="session delay") as caught:
        validate_config(raw, environ={})

    assert caught.value.issues[0].path == "latency.session_delays"


def test_incomplete_enabled_influx_is_rejected():
    raw = valid_config()
    raw["tsdb"] = {"enabled": True, "url": "http://localhost:8086"}

    with pytest.raises(ConfigurationError, match="token, org, bucket"):
        validate_config(raw, environ={})


@pytest.mark.parametrize(
    "instruments",
    [
        {},
        {"   ": {"tick_size": "0.01", "lot_size": "0.001"}},
        {
            " btc-usdt ": {"tick_size": "0.01", "lot_size": "0.001"},
            "BTC-USDT": {"tick_size": "0.01", "lot_size": "0.001"},
        },
    ],
)
def test_empty_and_duplicate_normalized_instruments_are_rejected(instruments):
    raw = valid_config()
    raw["instruments"] = instruments

    with pytest.raises(ConfigurationError):
        validate_config(raw, environ={})


def test_obsolete_execution_and_top_level_stp_fields_are_forbidden():
    raw = valid_config()
    raw["execution"]["partial_fill_probability"] = 0.5
    raw["stp"] = {"default_policy": "ALLOW"}

    with pytest.raises(ConfigurationError) as caught:
        validate_config(raw, environ={})

    assert [issue.path for issue in caught.value.issues] == [
        "execution.partial_fill_probability",
        "stp",
    ]


def test_wildcard_cors_remains_rejected():
    raw = valid_config()
    raw["cors_origins"] = ["*"]

    with pytest.raises(ConfigurationError, match="wildcard CORS"):
        validate_config(raw, environ={})


def test_environment_overrides_merge_then_validate():
    raw = valid_config()
    config = validate_config(
        raw,
        environ={
            "INFLUXDB_ENABLED": "true",
            "INFLUXDB_URL": "http://localhost:8086",
            "INFLUXDB_TOKEN": "token",
            "INFLUXDB_ORG": "org",
            "INFLUXDB_BUCKET": "bucket",
            "INFLUXDB_FLUSH_BATCH_SIZE": "25",
            "INFLUXDB_MAX_BUFFER_RECORDS": "50",
            "INFLUXDB_MAX_BUFFER_BYTES": "4096",
            "INFLUXDB_FLUSH_INTERVAL_MS": "100",
            "INFLUXDB_RETRY_BACKOFF_MS": "250",
            "INFLUXDB_MAX_RETRY_DELAY_MS": "1000",
            "INFLUXDB_OVERFLOW_RETRY_ATTEMPTS": "4",
            "INFLUXDB_SHUTDOWN_TIMEOUT_MS": "750",
            "INFLUXDB_OVERFLOW_POLICY": "drop_oldest",
            "INFLUXDB_TIMEOUT_MS": "500",
        },
    )

    assert config.tsdb is not None
    assert config.tsdb.enabled is True
    assert config.tsdb.flush_batch_size == 25
    assert config.tsdb.max_buffer_records == 50
    assert config.tsdb.max_buffer_bytes == 4096
    assert config.tsdb.flush_interval_ms == 100
    assert config.tsdb.retry_backoff_ms == 250
    assert config.tsdb.max_retry_delay_ms == 1000
    assert config.tsdb.overflow_retry_attempts == 4
    assert config.tsdb.shutdown_timeout_ms == 750
    assert config.tsdb.overflow_policy == "drop_oldest"
    assert config.tsdb.timeout_ms == 500

    with pytest.raises(ConfigurationError) as caught:
        validate_config(
            raw,
            environ={"INFLUXDB_FLUSH_BATCH_SIZE": "not-an-integer"},
        )
    assert caught.value.issues[0].path == (
        "environment.INFLUXDB_FLUSH_BATCH_SIZE"
    )


def test_every_shipped_yaml_passes_strict_validation():
    for path in sorted((ROOT / "configs").glob("*.yml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        validate_config(raw, environ={})


@pytest.mark.parametrize(
    ("yaml_path", "fix_path"),
    [
        ("configs/mockx-local.yml", "configs/mockx1.cfg"),
        ("examples/live_exchange.yml", "examples/live_acceptor.cfg"),
        (
            "examples/live_exchange_docker.yml",
            "examples/live_acceptor.cfg",
        ),
    ],
)
def test_every_shipped_fix_session_has_exact_participant_mapping(
    yaml_path,
    fix_path,
):
    raw = yaml.safe_load((ROOT / yaml_path).read_text(encoding="utf-8"))
    config = validate_config(raw, environ={})

    assert set(config.fix.session_participants) == configured_fix_session_ids(
        str(ROOT / fix_path)
    )


def test_readme_configuration_example_is_executable():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(
        r"<!-- validated-config-example -->\s*```yaml\s*(.*?)```",
        readme,
        flags=re.DOTALL,
    )
    assert match is not None
    validate_config(yaml.safe_load(match.group(1)), environ={})


@pytest.fixture
def normalizer() -> OrderNormalizer:
    config = validate_config(valid_config(), environ={})
    return OrderNormalizer(InstrumentCatalog.from_config(config))


def raw_order(**overrides) -> RawOrderInput:
    values = {
        "session_id": "REST:test",
        "participant_id": "participant-test",
        "cl_ord_id": "ORDER-1",
        "symbol": "BTC-USDT",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "0.001",
        "price": "100.00",
        "time_in_force": "GTC",
    }
    values.update(overrides)
    return RawOrderInput(**values)


@pytest.mark.parametrize(
    ("quantity", "code"),
    [
        ("NaN", OrderValidationCode.NON_FINITE_NUMBER),
        ("Infinity", OrderValidationCode.NON_FINITE_NUMBER),
        ("-Infinity", OrderValidationCode.NON_FINITE_NUMBER),
        ("0", OrderValidationCode.NON_POSITIVE_QUANTITY),
        ("-0.001", OrderValidationCode.NON_POSITIVE_QUANTITY),
    ],
)
def test_invalid_quantities_are_deterministic(normalizer, quantity, code):
    with pytest.raises(OrderValidationError) as caught:
        normalizer.normalize(
            raw_order(quantity=quantity),
            submitted_at=datetime.now(timezone.utc),
        )
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("price", "code"),
    [
        ("NaN", OrderValidationCode.NON_FINITE_NUMBER),
        ("Infinity", OrderValidationCode.NON_FINITE_NUMBER),
        ("-Infinity", OrderValidationCode.NON_FINITE_NUMBER),
        ("0", OrderValidationCode.NON_POSITIVE_PRICE),
        ("-0.01", OrderValidationCode.NON_POSITIVE_PRICE),
    ],
)
def test_invalid_limit_prices_are_deterministic(normalizer, price, code):
    with pytest.raises(OrderValidationError) as caught:
        normalizer.normalize(
            raw_order(price=price),
            submitted_at=datetime.now(timezone.utc),
        )
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"time_in_force": "FOK"}, OrderValidationCode.UNSUPPORTED_TIF),
        ({"order_type": "STOP"}, OrderValidationCode.UNSUPPORTED_ORDER_TYPE),
        ({"symbol": "ETH-USDT"}, OrderValidationCode.UNKNOWN_SYMBOL),
        ({"participant_id": ""}, OrderValidationCode.INVALID_PARTICIPANT_ID),
        ({"cl_ord_id": " bad "}, OrderValidationCode.INVALID_CL_ORD_ID),
    ],
)
def test_unsupported_order_values_are_not_defaulted(normalizer, overrides, code):
    with pytest.raises(OrderValidationError) as caught:
        normalizer.normalize(
            raw_order(**overrides),
            submitted_at=datetime.now(timezone.utc),
        )
    assert caught.value.code == code


def test_market_order_requires_no_price_and_missing_tif_defaults_to_gtc(normalizer):
    command = normalizer.normalize(
        raw_order(order_type="MARKET", price=None, time_in_force=None),
        submitted_at=datetime.now(timezone.utc),
    )
    assert command.price_ticks is None
    assert command.tif.value == "GTC"

    with pytest.raises(OrderValidationError) as caught:
        normalizer.normalize(
            raw_order(order_type="MARKET", price="100.00"),
            submitted_at=datetime.now(timezone.utc),
        )
    assert caught.value.code == OrderValidationCode.PRICE_NOT_ALLOWED


def test_rest_shape_and_numeric_errors_share_structured_422_contract():
    exchange = make_exchange()
    with TestClient(exchange.app) as client:
        assert client.post(
            "/api/v1/session/start",
            json={"run_id": "pr8-validation"},
        ).status_code == 200
        base = {
            "session_id": "REST:test",
            "participant_id": "participant-test",
            "cl_ord_id": "REST-1",
            "symbol": "TEST",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": "1",
            "price": "100",
        }

        missing = deepcopy(base)
        missing.pop("participant_id")
        shape_response = client.post("/api/v1/orders", json=missing)
        assert shape_response.status_code == 422
        assert shape_response.json()["detail"]["code"] == (
            "REQUEST_VALIDATION_ERROR"
        )

        invalid = {**base, "quantity": "NaN"}
        numeric_response = client.post("/api/v1/orders", json=invalid)
        assert numeric_response.status_code == 422
        assert numeric_response.json()["detail"]["code"] == (
            "NON_FINITE_NUMBER"
        )
        assert exchange.engine.orders == {}
        assert exchange.engine.client_order_index == {}


def test_rest_accepts_exact_strings_and_duplicate_scope_survives_reset():
    exchange = make_exchange()
    base = {
        "session_id": "REST:one",
        "participant_id": "participant-one",
        "cl_ord_id": "REST-DUPLICATE",
        "symbol": "TEST",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "1",
        "price": "100",
    }
    with TestClient(exchange.app) as client:
        assert client.post(
            "/api/v1/session/start",
            json={"run_id": "rest-one"},
        ).status_code == 200
        accepted = client.post("/api/v1/orders", json=base)
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "PENDING_NEW"
        assert client.post("/api/v1/session/end").status_code == 200

        assert client.post(
            "/api/v1/session/start",
            json={"run_id": "rest-two"},
        ).status_code == 200
        duplicate = client.post("/api/v1/orders", json=base)
        assert duplicate.status_code == 400
        assert duplicate.json()["detail"]["code"] == "DUPLICATE_CL_ORD_ID"

        independent_session = {
            **base,
            "session_id": "REST:two",
            "participant_id": "participant-two",
        }
        assert client.post(
            "/api/v1/orders",
            json=independent_session,
        ).status_code == 202


def test_fix_startup_requires_exact_quickfix_session_mapping():
    raw = valid_config()
    raw["fix"] = {
        "enabled": True,
        "session_participants": {
            "FIX.4.4:MOCKX1->NOT-CONFIGURED": "participant"
        },
    }
    exchange = MockExchange(
        validate_config(raw, environ={}),
        str(ROOT / "configs" / "mockx1.cfg"),
    )
    exchange.fix_app.loop = object()

    with pytest.raises(
        ValueError,
        match="missing participant mappings.*unknown mapped FIX sessions",
    ):
        exchange.fix_gateway.start()


@pytest.mark.asyncio
async def test_invalid_fix_number_becomes_reject_without_scheduler_failure():
    exchange = _exchange()
    sessions, delivered = await _start(exchange, "CLIENT")
    session = sessions["CLIENT"]
    try:
        message = _new_order("BAD-NUMBER", 1, qty=1, price=100)
        message.setField(fix.StringField(38, "NaN"))
        exchange.fix_app.fromApp(message, session)
        await exchange.fix_app.wait_for_ingress()
        await asyncio.sleep(0)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        rejected = [
            report
            for report in delivered
            if isinstance(report, ExecutionReport)
            and report.cl_ord_id == "BAD-NUMBER"
        ]
        assert len(rejected) == 1
        assert rejected[0].ord_status == OrderStatus.REJECTED
        assert rejected[0].text.startswith("NON_FINITE_NUMBER:")
        assert exchange.engine.orders == {}
        assert exchange.scheduler.state == SchedulerState.RUNNING
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_fix_market_order_with_price_is_rejected_not_silently_stripped():
    exchange = _exchange()
    sessions, delivered = await _start(exchange, "CLIENT")
    session = sessions["CLIENT"]
    try:
        message = _new_order("MARKET-WITH-PRICE", 1, qty=1, price=100)
        message.setField(fix.OrdType(fix.OrdType_MARKET))
        exchange.fix_app.fromApp(message, session)
        await exchange.fix_app.wait_for_ingress()
        await asyncio.sleep(0)
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)

        rejected = [
            report
            for report in delivered
            if isinstance(report, ExecutionReport)
            and report.cl_ord_id == "MARKET-WITH-PRICE"
        ]
        assert len(rejected) == 1
        assert rejected[0].ord_status == OrderStatus.REJECTED
        assert rejected[0].text.startswith("PRICE_NOT_ALLOWED:")
        assert exchange.engine.orders == {}
        assert exchange.scheduler.state == SchedulerState.RUNNING
    finally:
        await _stop(exchange)

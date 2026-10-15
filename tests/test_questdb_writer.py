"""QuestDB adapter schema, conversion, and lifecycle tests."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from src.mockx.metrics import MetricRecord, MetricsSinkError
from src.mockx import questdb_writer as adapter


class FakeTimestampNanos:
    def __init__(self, value: int) -> None:
        self.value = value


class FakeBuffer:
    def __init__(self) -> None:
        self.rows = []

    def row(self, table, *, symbols, columns, at) -> None:
        self.rows.append((table, symbols, columns, at))


class FakeSender:
    instances = []

    def __init__(self, protocol, host, port, **options) -> None:
        self.protocol = protocol
        self.host = host
        self.port = port
        self.options = options
        self.establish_calls = 0
        self.buffers = []
        self.internal_flushes = 0
        self.close_calls = []
        self.fail_write = False
        self.fail_flush = False
        self.__class__.instances.append(self)

    def establish(self) -> None:
        self.establish_calls += 1

    def new_buffer(self):
        return FakeBuffer()

    def flush(self, buffer=None, *, transactional=False) -> None:
        if buffer is None:
            if self.fail_flush:
                raise RuntimeError("flush failed")
            self.internal_flushes += 1
            return
        if self.fail_write:
            raise RuntimeError("write failed")
        assert transactional
        self.buffers.append(buffer)

    def close(self, *, flush=True) -> None:
        self.close_calls.append(flush)


class FakeResponse:
    def __init__(self, payload=None, failure: Exception | None = None) -> None:
        self.payload = payload if payload is not None else {}
        self.failure = failure

    def raise_for_status(self) -> None:
        if self.failure is not None:
            raise self.failure

    def json(self):
        return self.payload


class FakeHTTPClient:
    instances = []

    def __init__(self, **options) -> None:
        self.options = options
        self.queries = []
        self.health_calls = []
        self.close_calls = 0
        self.fail_health = False
        self.fail_schema = False
        self.__class__.instances.append(self)

    def get(self, url, params=None):
        if params is None:
            self.health_calls.append(url)
            failure = RuntimeError("health failed") if self.fail_health else None
            return FakeResponse(failure=failure)

        query = params["query"]
        self.queries.append(query)
        if query.startswith("CREATE TABLE"):
            return FakeResponse({"ddl": "OK"})
        if "FROM tables()" in query:
            name = re.search(r"table_name = '([^']+)'", query).group(1)
            dataset = (
                [] if self.fail_schema else [[name, "timestamp", "DAY", True, True]]
            )
            return FakeResponse(
                {
                    "columns": [
                        {"name": "table_name"},
                        {"name": "designatedTimestamp"},
                        {"name": "partitionBy"},
                        {"name": "walEnabled"},
                        {"name": "dedup"},
                    ],
                    "dataset": dataset,
                }
            )
        if "table_columns(" in query:
            name = re.search(r"table_columns\('([^']+)'\)", query).group(1)
            schema = next(item for item in adapter.QUESTDB_SCHEMAS if item.name == name)
            upsert_keys = {"timestamp", "run_id", "event_sequence"}
            return FakeResponse(
                {
                    "columns": [
                        {"name": "column"},
                        {"name": "type"},
                        {"name": "designated"},
                        {"name": "upsertKey"},
                    ],
                    "dataset": [
                        [
                            column,
                            column_type,
                            column == "timestamp",
                            column in upsert_keys,
                        ]
                        for column, column_type in schema.columns
                    ],
                }
            )
        raise AssertionError(f"Unexpected SQL: {query}")

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture
def patched_adapter(monkeypatch):
    FakeSender.instances.clear()
    FakeHTTPClient.instances.clear()
    monkeypatch.setattr(
        adapter,
        "Protocol",
        SimpleNamespace(Http="http", Https="https"),
    )
    monkeypatch.setattr(adapter, "Sender", FakeSender)
    monkeypatch.setattr(adapter, "TimestampNanos", FakeTimestampNanos)
    monkeypatch.setattr(adapter.httpx, "Client", FakeHTTPClient)

    def create(**overrides):
        options = {
            "url": "http://questdb:9000",
            "health_url": "http://questdb:9003",
            "username": "mockx-user",
            "password": "mockx-password",
            "request_timeout_ms": 10_000,
        }
        options.update(overrides)
        return adapter.QuestDBMetricsSink(**options)

    return create


def metric_record(timestamp_ns: int = 1_000_000_123) -> MetricRecord:
    return MetricRecord.create(
        "exchange_events",
        {"venue_id": "TEST", "symbol": "BTC-USDT", "kind": "REPORT_CREATED"},
        {"order_id": 7, "exec_id": "E7", "price_ticks": 10000},
        run_id="questdb-run",
        event_sequence=3,
        event_timestamp_ns=timestamp_ns,
    )


def test_schema_ddl_is_explicit_wal_nanosecond_and_deduplicated():
    assert {schema.name for schema in adapter.QUESTDB_SCHEMAS} == {"exchange_events"}
    for schema in adapter.QUESTDB_SCHEMAS:
        assert "timestamp TIMESTAMP_NS" in schema.ddl
        assert "PARTITION BY DAY WAL" in schema.ddl
        assert "DEDUP UPSERT KEYS(timestamp, run_id, event_sequence)" in schema.ddl
        assert dict(schema.columns)["run_id"] == "SYMBOL"
        assert dict(schema.columns)["event_timestamp_ns"] == "LONG"

    columns = dict(adapter.QUESTDB_SCHEMAS[0].columns)
    assert columns["ord_status"] == "SYMBOL"
    assert columns["exec_id"] == "VARCHAR"
    assert columns["price_ticks"] == "LONG"
    assert columns["duration_ns"] == "LONG"
    assert "bids_json" not in columns


def test_client_uses_http_auth_timeout_and_disables_internal_batching(
    patched_adapter,
):
    patched_adapter()
    sender = FakeSender.instances[-1]
    client = FakeHTTPClient.instances[-1]

    assert len(FakeSender.instances) == 1
    assert sender.establish_calls == 1
    assert (sender.protocol, sender.host, sender.port) == (
        "http",
        "questdb",
        9000,
    )
    assert sender.options == {
        "auto_flush": False,
        "retry_timeout": 0,
        "request_timeout": 10_000,
        "username": "mockx-user",
        "password": "mockx-password",
    }
    assert client.options == {
        "timeout": 10.0,
        "auth": ("mockx-user", "mockx-password"),
    }


@pytest.mark.asyncio
async def test_health_creates_and_verifies_every_schema(patched_adapter):
    sink = patched_adapter()

    await sink.health_check()

    client = FakeHTTPClient.instances[-1]
    assert client.health_calls == ["http://questdb:9003"]
    creates = [query for query in client.queries if query.startswith("CREATE")]
    assert creates == [schema.ddl for schema in adapter.QUESTDB_SCHEMAS]


@pytest.mark.asyncio
async def test_explicit_buffer_preserves_symbols_columns_and_nanoseconds(
    patched_adapter,
):
    sink = patched_adapter()
    record = metric_record()

    await sink.write([record])

    sender = FakeSender.instances[-1]
    assert len(sender.buffers) == 1
    table, symbols, columns, timestamp = sender.buffers[0].rows[0]
    assert table == record.table
    assert symbols == record.symbol_map
    assert columns == record.column_map
    assert timestamp.value == record.timestamp_ns == 1_000_000_123


@pytest.mark.asyncio
async def test_empty_write_does_not_initialize_schema_or_flush(patched_adapter):
    sink = patched_adapter()

    await sink.write([])

    assert FakeHTTPClient.instances[-1].queries == []
    assert FakeSender.instances[-1].buffers == []


@pytest.mark.asyncio
async def test_health_schema_write_and_flush_failures_are_wrapped(
    patched_adapter,
):
    sink = patched_adapter()
    client = FakeHTTPClient.instances[-1]
    client.fail_health = True
    with pytest.raises(MetricsSinkError, match="health check failed"):
        await sink.health_check()

    client.fail_health = False
    client.fail_schema = True
    with pytest.raises(MetricsSinkError, match="health check failed"):
        await sink.health_check()

    client.fail_schema = False
    await sink.health_check()
    sender = FakeSender.instances[-1]
    sender.fail_write = True
    with pytest.raises(MetricsSinkError, match="write failed"):
        await sink.write([metric_record()])

    sender.fail_write = False
    sender.fail_flush = True
    with pytest.raises(MetricsSinkError, match="flush failed"):
        await sink.flush()


@pytest.mark.asyncio
async def test_flush_and_close_are_explicit_and_close_is_idempotent(
    patched_adapter,
):
    sink = patched_adapter()

    await sink.flush()
    await sink.close()
    await sink.close()

    sender = FakeSender.instances[-1]
    assert sender.internal_flushes == 1
    assert sender.close_calls == [False]
    assert FakeHTTPClient.instances[-1].close_calls == 1


def test_client_initialization_failure_is_wrapped(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "Protocol",
        SimpleNamespace(Http="http", Https="https"),
    )
    monkeypatch.setattr(adapter, "TimestampNanos", FakeTimestampNanos)

    def fail_sender(*args, **kwargs):
        raise RuntimeError("client failed")

    monkeypatch.setattr(adapter, "Sender", fail_sender)

    with pytest.raises(MetricsSinkError, match="initialization failed"):
        adapter.QuestDBMetricsSink(
            url="http://questdb:9000",
            health_url="http://questdb:9003",
            username=None,
            password=None,
            request_timeout_ms=10_000,
        )


def test_sender_establishment_failure_is_wrapped_and_closed(monkeypatch):
    FakeSender.instances.clear()
    monkeypatch.setattr(
        adapter,
        "Protocol",
        SimpleNamespace(Http="http", Https="https"),
    )
    monkeypatch.setattr(adapter, "TimestampNanos", FakeTimestampNanos)

    class FailingEstablishSender(FakeSender):
        def establish(self) -> None:
            super().establish()
            raise RuntimeError("establish failed")

    monkeypatch.setattr(adapter, "Sender", FailingEstablishSender)

    with pytest.raises(MetricsSinkError, match="initialization failed"):
        adapter.QuestDBMetricsSink(
            url="http://questdb:9000",
            health_url="http://questdb:9003",
            username="mockx-user",
            password="mockx-password",
            request_timeout_ms=10_000,
        )

    sender = FakeSender.instances[-1]
    assert sender.establish_calls == 1
    assert sender.close_calls == [False]


@pytest.mark.asyncio
async def test_close_failure_is_wrapped(patched_adapter):
    sink = patched_adapter()

    def fail_close(*, flush=True):
        raise RuntimeError("close failed")

    FakeSender.instances[-1].close = fail_close
    with pytest.raises(MetricsSinkError, match="close failed"):
        await sink.close()

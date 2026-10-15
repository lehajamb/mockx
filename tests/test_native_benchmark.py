"""Benchmark adapters honor the real QuestDB sink constructor contract."""

import pytest

from scripts import benchmark_native_exchange as benchmark


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_real_questdb_benchmark_passes_required_sink_settings(monkeypatch, configured):
    for name in ("QUESTDB_URL", "QUESTDB_HEALTH_URL", "QUESTDB_USERNAME",
                 "QUESTDB_PASSWORD", "QUESTDB_REQUEST_TIMEOUT_MS"):
        monkeypatch.delenv(name, raising=False)
    if configured:
        monkeypatch.setenv("QUESTDB_HEALTH_URL", "http://localhost:9001")
        monkeypatch.setenv("QUESTDB_USERNAME", "benchmark-user")
        monkeypatch.setenv("QUESTDB_PASSWORD", "benchmark-password")
        monkeypatch.setenv("QUESTDB_REQUEST_TIMEOUT_MS", "2500")
    observed = {}

    def real_sink_contract(*, url, health_url, username, password, request_timeout_ms):
        observed.update(url=url, health_url=health_url, username=username,
                        password=password, request_timeout_ms=request_timeout_ms)
        return object()

    class ExchangeConstructed(Exception):
        pass

    def stop_before_network(config, *_args, persistence_sink, **_kwargs):
        assert persistence_sink.delegate is not None
        assert config.questdb.url == observed["url"]
        assert config.questdb.health_url == observed["health_url"]
        assert config.questdb.username == observed["username"]
        assert config.questdb.password == observed["password"]
        assert config.questdb.request_timeout_ms == observed["request_timeout_ms"]
        raise ExchangeConstructed

    monkeypatch.setattr(benchmark, "QuestDBMetricsSink", real_sink_contract)
    monkeypatch.setattr(benchmark, "MockExchange", stop_before_network)
    with pytest.raises(ExchangeConstructed):
        await benchmark.measure_case(mode="native_E", depth=100, orders=100,
                                     window=100, delay_ms=0, dashboard_every=0,
                                     questdb_url="http://localhost:9000")
    assert observed == {
        "url": "http://localhost:9000",
        "health_url": "http://localhost:9001" if configured else "http://localhost:9000",
        "username": "benchmark-user" if configured else None,
        "password": "benchmark-password" if configured else None,
        "request_timeout_ms": 2500 if configured else 10_000,
    }

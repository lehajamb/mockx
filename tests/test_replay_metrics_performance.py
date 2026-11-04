"""Avoid unused persistence work while retaining exact runtime observability."""

from dataclasses import replace
from decimal import Decimal

from src.mockx.delivery import OutboundFixDeliveryQueue
from src.mockx.instruments import Instrument
from src.mockx.metrics import SIM_INBOUND_LATENCY
from src.mockx.models import (
    FixDeliveryState,
    LatencyMeasurement,
    SelfTradePrevention,
    Side,
)
from tests.exact_helpers import exact_metrics
from tests.test_metrics import make_instrumented_engine, submit


def test_disabled_persistence_keeps_counters_histograms_and_skips_unused_payloads(
    monkeypatch,
):
    engine, metrics, reports, trades = make_instrumented_engine()
    admitted, formatted = [], []
    monkeypatch.setattr(metrics, "_admit_record", lambda *args: admitted.append(args))
    for method in ("format_price", "format_quantity"):
        original = getattr(Instrument, method)

        def observe(self, value, original=original, method=method):
            formatted.append((method, value))
            return original(self, value)

        monkeypatch.setattr(Instrument, method, observe)
    submit(engine, "PASSIVE", Side.SELL, 5, 100)
    submit(engine, "AGGRESSOR", Side.BUY, 5, 100)
    metrics.record_self_trade_prevention(
        SelfTradePrevention(
            "TEST", "participant", 1, 2, 3, engine.scheduler.current_time
        )
    )
    assert metrics.observe_latency(
        LatencyMeasurement(
            1, "CLIENT", SIM_INBOUND_LATENCY, 7.0, "ms", engine.scheduler.current_time
        )
    )
    assert not formatted, "Disabled persistence must not format order/trade/STP rows"
    assert (
        not admitted
    ), "Disabled persistence must not construct discarded latency rows"
    snapshot = metrics.snapshot()
    assert snapshot.orders_submitted == 2
    assert snapshot.trades == 1
    assert snapshot.execution_reports == len(reports)
    assert Decimal(snapshot.traded_quantity) == Decimal("5")
    assert Decimal(snapshot.traded_notional) == Decimal("500")
    assert Decimal(snapshot.canceled_quantity) == Decimal("0")
    assert snapshot.average_sim_inbound_latency_ms == 7.0
    assert b"mockx_sim_inbound_latency_ms_count 1.0" in metrics.prometheus_payload()
    assert (
        metrics.registry.get_sample_value(
            "mockx_self_trade_preventions_total", {"symbol": "TEST"}
        )
        == 1
    )


def test_delivery_gauges_reuse_children_and_only_write_changed_values(monkeypatch):
    metrics = exact_metrics("DELIVERY-CACHE")
    states = (
        FixDeliveryState.PENDING,
        FixDeliveryState.DUE,
        FixDeliveryState.RETRY_PENDING,
        FixDeliveryState.DELIVERED,
        FixDeliveryState.PERMANENTLY_FAILED,
    )
    gauges = {
        "depth": metrics._prom_gauges["fix_delivery_queue_depth"],
        "age": metrics._prom_gauges["fix_delivery_oldest_pending_age_seconds"],
        **{
            state.value: metrics._prom_delivery_state_gauge.labels(state=state.value)
            for state in states
        },
    }
    writes, label_calls = [], []
    for name, gauge in gauges.items():
        original = gauge.set

        def record(value, name=name, original=original):
            writes.append((name, value))
            original(value)

        monkeypatch.setattr(gauge, "set", record)
    labels = metrics._prom_delivery_state_gauge.labels

    def record_labels(*args, **kwargs):
        label_calls.append(kwargs)
        return labels(*args, **kwargs)

    monkeypatch.setattr(metrics._prom_delivery_state_gauge, "labels", record_labels)
    empty = OutboundFixDeliveryQueue().snapshot()
    metrics.update_fix_delivery_gauges(empty)
    assert len(writes) == 7
    writes.clear()
    metrics.update_fix_delivery_gauges(empty)
    assert not writes, "Repeated state snapshots must not reset unchanged gauges"
    assert not label_calls, "Delivery gauge children are stable and can be cached"
    active = replace(
        empty,
        queue_depth=3,
        pending=2,
        due=1,
        delivered=4,
        oldest_pending_age_seconds=0.25,
    )
    metrics.update_fix_delivery_gauges(active)
    assert dict(writes) == {
        "depth": 3,
        "age": 0.25,
        "PENDING": 2,
        "DUE": 1,
        "DELIVERED": 4,
    }
    writes.clear()
    metrics.update_fix_delivery_gauges(replace(active, oldest_pending_age_seconds=0.5))
    assert writes == [("age", 0.5)]
    assert metrics.registry.get_sample_value("mockx_fix_delivery_queue_depth") == 3
    assert (
        metrics.registry.get_sample_value(
            "mockx_fix_delivery_oldest_pending_age_seconds"
        )
        == 0.5
    )
    for state, value in zip(states, (2, 1, 0, 4, 0)):
        assert (
            metrics.registry.get_sample_value(
                "mockx_fix_delivery_records", {"state": state.value}
            )
            == value
        )

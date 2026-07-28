"""API and engine integration tests for scheduler fail-stop behavior."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from src.mockx.latency import SchedulerFailedError
from src.mockx.main import MockExchange
from src.mockx.models import (
    CancelOrderCommand,
    NewOrderCommand,
    OrderStatus,
    SchedulerState,
    Side,
    TimeInForce,
)

from .api_helpers import api_config, make_exchange


def _submit_scheduled_limit_order(exchange: MockExchange) -> int:
    return exchange.engine.submit_order(
        NewOrderCommand(
            cl_ord_id="FAIL-AFTER-CLAIM",
            symbol="TEST",
            side=Side.BUY,
            qty=2.0,
            price=100.0,
            tif=TimeInForce.GTC,
            submitted_at=exchange.scheduler.current_time,
            session_id="FIX.4.4:CLIENT->MOCKX",
            session_alias="CLIENT",
        )
    )


def test_failed_advance_is_503_not_ready_observable_and_session_resettable():
    exchange = make_exchange()
    original_error = RuntimeError("API handler failed")

    def fail(_data):
        raise original_error

    with TestClient(exchange.app) as client:
        started = client.post(
            "/api/v1/session/start",
            json={"run_id": "failed-run"},
        )
        assert started.status_code == 200

        exchange.scheduler.register_handler("fail", fail)
        exchange.scheduler.schedule_event("fail", {"probe": 1}, 0)
        response = client.post(
            "/admin/advance-time",
            json={"milliseconds": 1},
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": {
                "code": "SCHEDULER_FAILED",
                "message": (
                    "Scheduler is failed; end the current simulation "
                    "session and start a new session to reset it"
                ),
            }
        }

        readiness = client.get("/readyz")
        assert readiness.status_code == 503
        assert readiness.json()["status"] == "not_ready"
        assert readiness.json()["components"]["scheduler"] == "failed"

        stats = client.get("/stats")
        assert stats.status_code == 200
        scheduler_stats = stats.json()["scheduler"]
        assert scheduler_stats["state"] == "FAILED"
        assert scheduler_stats["failure_count"] == 1
        assert scheduler_stats["max_events_per_advance"] == 100_000
        assert scheduler_stats["dead_letter"]["event_type"] == "fail"
        assert scheduler_stats["dead_letter"]["retry_attempted"] is False

        repeated = client.post(
            "/admin/advance-time",
            json={"milliseconds": 1},
        )
        assert repeated.status_code == 503

        ended = client.post("/api/v1/session/end")
        assert ended.status_code == 200
        assert (
            ended.json()["final_stats"]["scheduler"]["state"]
            == "FAILED"
        )

        restarted = client.post(
            "/api/v1/session/start",
            json={"run_id": "clean-run"},
        )
        assert restarted.status_code == 200
        reset_stats = client.get("/stats").json()["scheduler"]
        assert reset_stats["state"] == "RUNNING"
        assert reset_stats["failure_count"] == 0
        assert reset_stats["dead_letter"] is None
        assert client.get("/readyz").status_code == 200


def test_post_mutation_engine_failure_cannot_look_healthy(
    monkeypatch,
):
    exchange = make_exchange()
    later_events = []

    with TestClient(exchange.app) as client:
        assert client.post(
            "/api/v1/session/start",
            json={"run_id": "post-mutation-failure"},
        ).status_code == 200
        order_id = _submit_scheduled_limit_order(exchange)
        order = exchange.engine.orders[order_id]

        def fail_after_claim(order, _book):
            assert order.processed is True
            raise RuntimeError("matching failed after processing claim")

        monkeypatch.setattr(
            exchange.engine,
            "_process_limit_order",
            fail_after_claim,
        )
        exchange.scheduler.register_handler("later", later_events.append)
        exchange.scheduler.schedule_event("later", "must-not-run", 1)
        response = client.post(
            "/admin/advance-time",
            json={"milliseconds": 1},
        )
        stats = client.get("/stats").json()
        readiness = client.get("/readyz")
        assert exchange.scheduler.state == SchedulerState.FAILED

        order_count = len(exchange.engine.orders)
        with pytest.raises(SchedulerFailedError):
            _submit_scheduled_limit_order(exchange)
        assert len(exchange.engine.orders) == order_count

        with pytest.raises(SchedulerFailedError):
            exchange.engine.cancel_order(
                CancelOrderCommand(
                    order_id=order_id,
                    expected_version=order.version,
                    cancel_cl_ord_id="CXL-FAILED",
                    orig_cl_ord_id=order.cl_ord_id,
                    requested_at=exchange.scheduler.current_time,
                    session_id=order.session_id,
                    session_alias=order.session_alias,
                )
            )

    assert response.status_code == 503
    assert order.processed is True
    assert order.status == OrderStatus.PENDING_NEW
    assert later_events == []
    assert exchange.scheduler.get_pending_count() == 1
    assert exchange.scheduler.state == SchedulerState.STOPPED
    assert stats["scheduler"]["state"] == "FAILED"
    assert stats["scheduler"]["dead_letter"]["event_type"] == "order_process"
    assert (
        stats["scheduler"]["dead_letter"]["event_metadata"]["order_id"]
        == order_id
    )
    assert stats["reports"]["created"] == 0
    assert readiness.status_code == 503


@pytest.mark.asyncio
async def test_realtime_scheduler_freezes_after_failure_and_resumes_on_reset():
    config = api_config()
    config["scheduler"]["mode"] = "realtime"
    config["scheduler"]["interval_ms"] = 1
    exchange = MockExchange(config)
    later_events = []

    exchange.scheduler.register_handler(
        "fail",
        lambda _data: (_ for _ in ()).throw(RuntimeError("realtime boom")),
    )
    exchange.scheduler.register_handler("later", later_events.append)
    await exchange.lifecycle.start_session("realtime-failed")
    exchange.scheduler.schedule_event("fail", None, 0)
    exchange.scheduler.schedule_event("later", "frozen", 0)

    exchange.running = True
    task = asyncio.create_task(exchange._run_scheduler())
    try:
        for _ in range(100):
            if exchange.scheduler.state == SchedulerState.FAILED:
                break
            await asyncio.sleep(0.002)
        assert exchange.scheduler.state == SchedulerState.FAILED
        failed_time = exchange.scheduler.current_time

        await asyncio.sleep(0.01)
        assert later_events == []
        assert exchange.scheduler.current_time == failed_time

        await exchange.lifecycle.end_session()
        await exchange.lifecycle.start_session("realtime-recovered")
        exchange.scheduler.schedule_event("later", "after-reset", 0)
        for _ in range(100):
            if later_events:
                break
            await asyncio.sleep(0.002)
        assert later_events == ["after-reset"]
    finally:
        exchange.running = False
        await task

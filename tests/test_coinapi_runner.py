import time
from collections import Counter
from io import StringIO
import json
from types import SimpleNamespace

import pytest

from examples.coinapi_replay.runner import ClockProjection, ReplayOptions, ReplayRunner
from examples.coinapi_replay.__main__ import fix_settings
from examples.coinapi_replay.tracker import FixIntent
from tests.exact_helpers import TEST_INSTRUMENT


def probe_runner():
    runner = object.__new__(ReplayRunner)
    runner.options = ReplayOptions(probe=True)
    runner.instrument = TEST_INSTRUMENT
    runner.run_id = "test"
    runner.feed = SimpleNamespace(bids=((100, 2),), asks=((102, 3),))
    runner._probe_counter = 0
    runner._probe_next = 0
    runner._probe_cancel = None
    runner.external = {}
    runner.sent_intents = []
    runner._send = lambda intent, **kwargs: runner.sent_intents.append(intent)
    return runner


def test_probe_final_cancel_cannot_generate_another_order_pair():
    runner = probe_runner()
    passive = FixIntent(
        "NEW", "strategy_bot", "P", None, "TEST", "BUY", 1, 90, "GTC", None, None
    )
    runner._probe_cancel = (500, passive)
    runner._probe_next = 0  # Clock may have advanced beyond the next probe cycle.
    runner.external["P"] = [passive, 0]
    runner._cancel_probe()
    assert [(intent.kind, intent.orig_cl_ord_id) for intent in runner.sent_intents] == [
        ("CANCEL", "P")
    ]
    assert runner._probe_cancel is None


def test_probe_does_not_cancel_an_already_observed_terminal_order():
    runner = probe_runner()
    passive = FixIntent(
        "NEW", "strategy_bot", "P", None, "TEST", "BUY", 1, 90, "GTC", None, None
    )
    runner._probe_cancel = (500, passive)
    runner._cancel_probe()
    assert not runner.sent_intents
    assert runner._probe_cancel is None


def test_slow_replay_scales_source_and_latency_clock_together():
    clock = ClockProjection(wall_ns=10_000, simulated_ns=20_000, rate=0.25)
    assert clock.now_ns(14_000) == 21_000
    assert clock.deadline_ns(21_000) == 14_000


def test_audit_accepts_fix_intent_kind_even_when_logging_is_disabled():
    runner = object.__new__(ReplayRunner)
    runner.audit = None
    intent = FixIntent(
        "NEW", "strategy_bot", "one", None, "TEST", "BUY", 1, 90, "GTC", None, None
    )
    runner._audit("submit", intent)
    runner.audit = StringIO()
    runner._audit("submit", intent)
    record = json.loads(runner.audit.getvalue())
    assert record["event"] == "submit"
    assert record["kind"] == "NEW"
    assert record["cl_ord_id"] == "one"


@pytest.mark.parametrize("rate", [0, -1, 1.01, float("nan"), float("inf")])
def test_replay_refuses_unqualified_fast_or_invalid_rates(rate):
    with pytest.raises(ValueError):
        ReplayOptions(rate=rate)


def test_generated_fix_settings_have_distinct_owners_and_real_sending_time(tmp_path):
    roles = ["replay_buy", "replay_sell", "controller", "strategy_bot"]
    server = fix_settings(
        output=tmp_path,
        role_names=roles,
        initiator=False,
        port=4001,
        store_name="server",
    )
    client = fix_settings(
        output=tmp_path,
        role_names=roles[:2],
        initiator=True,
        port=4001,
        store_name="client",
    )
    assert all(f"TargetCompID={role}" in server for role in roles)
    assert "SenderCompID=strategy_bot" not in client
    assert "TargetCompID=MOCKX_COINAPI" in client
    assert "SocketNodelay=Y" in server
    assert "CheckLatency=N" not in server + client
    assert str(tmp_path).replace("\\", "/") in server

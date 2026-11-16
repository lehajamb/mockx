"""Actual execution reports settle external probe requests without extra acks."""

from collections import Counter
import time
from types import SimpleNamespace

import pytest

from examples.coinapi_replay.runner import ReplayRunner
from examples.coinapi_replay.tracker import FixIntent, ReplayLedger
from tests.exact_helpers import TEST_INSTRUMENT


def runner():
    result = object.__new__(ReplayRunner)
    result.audit = None
    result.report_counts = Counter()
    result.actor_fills = Counter()
    result.options = SimpleNamespace(timeout=15)
    result.ledger = ReplayLedger("TEST", TEST_INSTRUMENT)
    intent = FixIntent(
        "NEW",
        "strategy_bot",
        "original",
        None,
        "TEST",
        "BUY",
        10,
        100,
        "GTC",
        None,
        None,
    )
    result.external = {"original": [intent, 0]}
    result.pending_since = {"original": time.monotonic_ns()}
    return result


def report(
    clid="original",
    *,
    status="PARTIAL",
    kind="F",
    cum="2",
    message="ExecutionReport",
    orig=None
):
    return SimpleNamespace(
        sender_comp_id="strategy_bot",
        cl_ord_id=clid,
        orig_cl_ord_id=orig,
        status=status,
        exec_type=kind,
        cum_qty=cum,
        message_type=message,
        text="Cancel rejected",
    )


def deliver(instance, value):
    # Keep the real report handler while avoiding dataclass-only audit formatting.
    from examples.fix_support import FixReport

    instance.transport = SimpleNamespace(
        take_reports=lambda: [
            FixReport(
                message_type=value.message_type,
                sender_comp_id=value.sender_comp_id,
                cl_ord_id=value.cl_ord_id,
                orig_cl_ord_id=value.orig_cl_ord_id,
                status=value.status,
                exec_type=value.exec_type,
                leaves_qty=None,
                last_price=None,
                last_qty=None,
                text=value.text,
                cum_qty=value.cum_qty,
            )
        ]
    )
    instance._reports()


def test_first_partial_trade_settles_gtc_new_without_a_separate_new_ack():
    instance = runner()
    deliver(instance, report())
    assert "original" not in instance.pending_since
    assert instance.actor_fills["strategy_bot"] == 2
    assert "original" in instance.external
    deliver(instance, report())
    assert instance.actor_fills["strategy_bot"] == 2


@pytest.mark.parametrize("status", ["NEW", "PARTIAL"])
def test_live_cancel_rejection_is_an_immediate_failure(status):
    instance = runner()
    instance.external["cancel"] = instance.external["original"]
    instance.pending_since = {"cancel": time.monotonic_ns()}
    with pytest.raises(RuntimeError, match="cancel rejected"):
        deliver(
            instance,
            report(
                "cancel",
                status=status,
                kind=None,
                cum=None,
                message="OrderCancelReject",
                orig="original",
            ),
        )


def test_full_fill_then_cancel_reject_keeps_shared_fill_watermark():
    instance = runner()
    deliver(instance, report())
    instance.external["cancel"] = instance.external["original"]
    instance.pending_since["cancel"] = time.monotonic_ns()
    deliver(instance, report(status="FILLED", cum="10"))
    assert instance.actor_fills["strategy_bot"] == 10
    assert "cancel" in instance.external
    deliver(
        instance,
        report(
            "cancel",
            status="FILLED",
            kind=None,
            cum=None,
            message="OrderCancelReject",
            orig="original",
        ),
    )
    assert not instance.external and not instance.pending_since
    assert instance.actor_fills["strategy_bot"] == 10

"""An optional external actor takes only bounded improving midpoint actions."""

from examples.coinapi_replay.controller import (
    ControllerConfig, DepthLevel, MidController,
)


def controller(**kwargs):
    config = ControllerConfig(enabled=True, reaction_interval_ns=10,
                              max_book_age_ns=100, max_target_age_ns=100,
                              max_replay_lag_ns=20, **kwargs)
    result = MidController("TEST", config)
    result.observe_book(
        [DepthLevel(99, 2), DepthLevel(97, 3), DepthLevel(95, 1)],
        [DepthLevel(101, 2), DepthLevel(103, 4), DepthLevel(107, 1)],
        observed_ns=100,
    )
    return result


def test_consumes_complete_levels_minimizes_error_then_quantity():
    actor = controller()
    actor.observe_target(204, observed_ns=100)
    assert actor.decide(100) is None
    decision = actor.decide(110)
    # One ask level: mid 101 (error 1); two: mid 103 (error 1). Choose two lots.
    assert decision.intent.side == "BUY"
    assert decision.intent.quantity_lots == 2
    assert decision.intent.price_ticks is None  # Standard FIX market order.
    assert decision.intent.tif == "IOC"
    assert (decision.error_before, decision.error_after) == (4, 2)
    assert actor.decide(121) is None  # One in-flight action even after the rate interval.


def test_never_consumes_last_level_or_acts_without_strict_improvement():
    actor = controller()
    actor.observe_target(200, observed_ns=100)
    assert actor.decide(100) is None
    actor.observe_book([DepthLevel(99, 1)], [DepthLevel(101, 1)], observed_ns=100)
    actor.observe_target(300, observed_ns=100)
    assert actor.decide(100) is None


def test_sell_direction_and_quantity_depth_budgets():
    actor = controller(max_order_qty_lots=2, max_total_qty_lots=2, max_depth_levels=1)
    actor.observe_target(196, observed_ns=100)
    decision = actor.decide(110)
    assert (decision.intent.side, decision.intent.quantity_lots) == ("SELL", 2)
    actor.complete(decision.intent.cl_ord_id, now_ns=110)
    assert actor.decide(121) is None  # Run quantity budget is exhausted.


def test_stale_book_target_lag_and_rate_limits():
    actor = controller()
    actor.observe_target(204, observed_ns=100)
    assert actor.decide(110, replay_lag_ns=21) is None
    assert actor.decide(201) is None
    actor.observe_book([DepthLevel(99, 2), DepthLevel(97, 2)],
                       [DepthLevel(101, 2), DepthLevel(103, 2)], observed_ns=201)
    assert actor.decide(201) is None  # Target is still stale.
    actor.observe_target(204, observed_ns=201)
    first = actor.decide(201)
    actor.complete(first.intent.cl_ord_id, now_ns=201)
    assert actor.decide(210) is None
    assert actor.decide(211) is None  # No second sweep from the same pre-fill book.
    actor.observe_book([DepthLevel(99, 2), DepthLevel(97, 2)],
                       [DepthLevel(101, 2), DepthLevel(103, 2)], observed_ns=211)
    assert actor.decide(211) is None
    assert actor.decide(221) is not None


def test_disabled_stopped_and_crossed_book_are_inert():
    disabled = MidController("TEST", ControllerConfig())
    disabled.observe_book([DepthLevel(99, 1), DepthLevel(98, 1)],
                          [DepthLevel(101, 1), DepthLevel(102, 1)], observed_ns=0)
    disabled.observe_target(204, observed_ns=0)
    assert disabled.decide(0) is None
    actor = controller()
    actor.observe_target(204, observed_ns=100)
    actor.stop()
    assert actor.decide(100) is None
    assert actor.inflight_cl_ord_id is None


def test_wrong_completion_cannot_release_current_action():
    actor = controller()
    actor.observe_target(204, observed_ns=100)
    decision = actor.decide(110)
    assert not actor.complete("unrelated")
    assert actor.inflight_cl_ord_id == decision.intent.cl_ord_id
    assert actor.complete(decision.intent.cl_ord_id)


def test_empty_side_invalid_depth_and_missing_target():
    import pytest

    actor = controller()
    actor.observe_target(None, observed_ns=100)
    assert actor.decide(100) is None
    with pytest.raises(ValueError, match="uncrossed"):
        actor.observe_book([DepthLevel(101, 1)], [DepthLevel(101, 1)], observed_ns=101)
    actor.observe_book([], [DepthLevel(101, 1)], observed_ns=101)
    actor.observe_target(204, observed_ns=101)
    assert actor.decide(101) is None


def test_reaction_delay_coalesces_updates_without_postponing_deadline():
    actor = controller()
    actor.observe_target(204, observed_ns=100)
    assert actor.decide(100) is None
    actor.observe_target(206, observed_ns=105)
    actor.observe_book([DepthLevel(99, 2), DepthLevel(97, 3)],
                       [DepthLevel(101, 2), DepthLevel(103, 4), DepthLevel(107, 1)],
                       observed_ns=106)
    assert actor.decide(109) is None
    decision = actor.decide(110)
    assert decision is not None
    assert decision.target_mid_twice_ticks == 206
    assert actor.complete(decision.intent.cl_ord_id, now_ns=111)
    actor.observe_book([DepthLevel(99, 2), DepthLevel(97, 3)],
                       [DepthLevel(103, 4), DepthLevel(107, 1)], observed_ns=112)
    assert actor.decide(121) is None
    assert actor.decide(122) is not None


def test_delayed_feed_starts_reaction_at_receipt_without_refreshing_old_book():
    config = ControllerConfig(enabled=True, reaction_interval_ns=10,
                              max_book_age_ns=100, max_target_age_ns=100)
    actor = MidController("TEST", config)
    actor.observe_target(204, observed_ns=100)
    actor.observe_book([DepthLevel(99, 2), DepthLevel(97, 3)],
                       [DepthLevel(101, 2), DepthLevel(103, 4)],
                       observed_ns=100, received_ns=150)
    assert actor.decide(150) is None
    assert actor.decide(159) is None
    assert actor.decide(160) is not None

    stale = MidController("TEST", config)
    stale.observe_target(204, observed_ns=195)
    stale.observe_book([DepthLevel(99, 2), DepthLevel(97, 3)],
                       [DepthLevel(101, 2), DepthLevel(103, 4)],
                       observed_ns=100, received_ns=195)
    # The book ages out while the receipt-based reaction timer is running.
    assert stale.decide(205) is None



"""Controller consumes only observed depth and currently released targets."""

from examples.coinapi_replay.controller import (
    ControllerConfig,
    DepthLevel,
    MidController,
)


def controller():
    config = ControllerConfig(
        enabled=True,
        max_order_qty_lots=10,
        max_total_qty_lots=20,
        reaction_interval_ns=100,
        deadband_twice_ticks=2,
    )
    return MidController("TEST", config)


def observe(c):
    c.observe_book(
        [DepthLevel(99, 5), DepthLevel(98, 5)],
        [DepthLevel(101, 5), DepthLevel(104, 5), DepthLevel(106, 5)],
        0,
    )
    c.observe_target(204, 0)


def test_reaction_budget_deadband_and_one_inflight():
    c = controller()
    observe(c)
    assert c.decide(99) is None
    decision = c.decide(100)
    assert decision.intent.quantity_lots == 5
    assert decision.intent.side == "BUY"
    assert decision.intent.price_ticks == 101
    assert c.decide(200) is None
    c.complete(decision.intent.cl_ord_id)
    assert c.decide(300) is None  # need a fresh feed observation


def test_stale_or_one_sided_or_near_target_suppresses_order():
    c = controller()
    observe(c)
    c.observe_target(201, 0)
    assert c.decide(100) is None
    c.observe_target(204, 200)
    assert c.decide(10_000_000_000) is None
    c.observe_book([], [DepthLevel(101, 5)], 10_000_000_000)
    assert c.decide(10_000_000_100) is None

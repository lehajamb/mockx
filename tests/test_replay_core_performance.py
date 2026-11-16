"""Bounded command work and independent correctness checks for deep replay books."""

from unittest.mock import patch
from datetime import timedelta

from src.mockx.identifiers import RunIdentifierScope
from src.mockx.instruments import Instrument
from src.mockx.latency import LatencyScheduler
from src.mockx.metrics import (
    LatencyTracker,
    MATCH_LOOP_DURATION,
    SIM_ENGINE_LATENCY,
    SIM_END_TO_END_LATENCY,
    SIM_OUTBOUND_LATENCY,
)
import pytest

from src.mockx.models import (
    BookLevel,
    ExecutionParams,
    OrderBook,
    OrderBookInvariantError,
    OrderQueue,
    MarketDataSnapshot,
    OrderStatus,
    Side,
    TimeInForce,
)
import src.mockx.models as models
from tests.exact_helpers import exact_command, exact_engine, exact_metrics
from tests.test_order_book import cancel, submit
from tests.test_replace_core import command


def engine():
    return exact_engine(
        ExecutionParams(0, 0, reject_prob=0, rand_seed=42), LatencyScheduler()
    )


def test_commands_do_not_rescan_unrelated_orders_but_full_audit_still_agrees():
    core = engine()
    for number in range(80):
        submit(
            core, cl_ord_id=f"bid-{number}", side=Side.BUY, qty=5, price=100 - number
        )
    target = submit(core, cl_ord_id="target", side=Side.SELL, qty=7, price=101)
    with patch.object(
        OrderBook, "assert_invariants", wraps=core.books["TEST"].assert_invariants
    ) as audit:
        core.replace_order(command(core, target, qty=6))
        cancel(core, target)
        assert audit.call_count == 0
    book = core.books["TEST"]
    book.assert_invariants()
    assert core.active_order_count() == core.resting_order_count() == 80
    assert core.order_book_level_count() == 80
    assert all(
        order.status in {OrderStatus.NEW, OrderStatus.PARTIAL}
        for level in book.bids
        for order in level.orders
    )


def test_order_gauges_do_not_scan_retained_terminal_history():
    core = engine()
    resting = submit(core, cl_ord_id="keep", side=Side.BUY, qty=5, price=99)
    for number in range(30):
        order = submit(core, cl_ord_id=f"done-{number}", side=Side.BUY, qty=1, price=98)
        cancel(core, order)

    class NoScan(dict):
        def values(self):
            raise AssertionError("gauge scanned historical orders")

    core.orders = NoScan(core.orders)
    assert core.active_order_count() == core.resting_order_count() == 1
    cancel(core, resting)
    assert core.active_order_count() == core.resting_order_count() == 0


def test_unchanged_level_projections_are_reused_and_old_snapshots_stay_immutable():
    core = engine()
    changed = submit(core, cl_ord_id="changed", side=Side.BUY, qty=5, price=100)
    submit(core, cl_ord_id="untouched", side=Side.BUY, qty=3, price=99)
    before = core.get_market_data_snapshot("TEST")
    core.replace_order(command(core, changed, qty=4))
    after = core.get_market_data_snapshot("TEST")
    assert before.bids[1] is after.bids[1]
    assert before.bids[0] is not after.bids[0]
    assert before.bids[0].level_qty_lots == 5
    assert after.bids[0].level_qty_lots == 4


def test_snapshot_publication_only_projects_the_changed_level(monkeypatch):
    core = engine()
    orders = [
        submit(
            core,
            cl_ord_id=f"projection-{index}",
            side=Side.BUY,
            qty=5,
            price=100 - index,
        )
        for index in range(80)
    ]
    calls = []
    original = BookLevel.snapshot

    def observe(level):
        calls.append(level.price_ticks)
        return original(level)

    monkeypatch.setattr(BookLevel, "snapshot", observe)
    core.replace_order(command(core, orders[30], qty=4))
    core.get_market_data_snapshot("TEST")
    assert calls == [70]
    core.books["TEST"].assert_invariants()


def test_book_snapshot_does_not_sort_all_unchanged_levels_again(monkeypatch):
    core = engine()
    order = submit(core, cl_ord_id="sorted", side=Side.BUY, qty=5, price=100)
    submit(core, cl_ord_id="untouched", side=Side.BUY, qty=5, price=99)

    def no_sort(*args, **kwargs):
        raise AssertionError("book snapshot repeated full sorting validation")

    monkeypatch.setattr(models, "sorted", no_sort, raising=False)
    core.replace_order(command(core, order, qty=4))
    core.get_market_data_snapshot("TEST")


def test_public_snapshot_still_rejects_unsorted_levels():
    core = engine()
    submit(core, cl_ord_id="best", side=Side.BUY, qty=5, price=100)
    submit(core, cl_ord_id="second", side=Side.BUY, qty=5, price=99)
    snapshot = core.get_market_data_snapshot("TEST")
    with pytest.raises(ValueError, match="bids must be sorted"):
        MarketDataSnapshot(
            symbol=snapshot.symbol,
            instrument=snapshot.instrument,
            sequence=1,
            timestamp=snapshot.timestamp,
            bids=tuple(reversed(snapshot.bids)),
            asks=(),
        )
    with pytest.raises(ValueError, match="bids must be sorted"):
        type(snapshot.bids)(reversed(snapshot.bids))
    with pytest.raises(AttributeError):
        snapshot.bids.changed = True


def test_full_audit_detects_projection_neighbor_corruption():
    core = engine()
    for number in range(3):
        submit(
            core,
            cl_ord_id=f"neighbor-{number}",
            side=Side.BUY,
            qty=5,
            price=100 - number,
        )
    book = core.books["TEST"]
    projections = book._projections[Side.BUY]
    projections[0], projections[1] = projections[1], projections[0]
    with pytest.raises(
        OrderBookInvariantError, match="Immutable level projection mismatch"
    ):
        book.assert_invariants()


def test_touched_projection_checks_neighbors_before_quantity_mutation():
    core = engine()
    orders = [
        submit(
            core,
            cl_ord_id=f"neighbor-{number}",
            side=Side.BUY,
            qty=5,
            price=100 - number,
        )
        for number in range(3)
    ]
    book = core.books["TEST"]
    book._projections[Side.BUY][0] = book._projections[Side.BUY][2]
    with pytest.raises(OrderBookInvariantError, match="neighbors are not sorted"):
        book.resize_order(orders[1], 4)
    assert orders[1].order_qty_lots == book.bids[1].level_qty_lots == 5


def test_disabled_persistence_does_not_format_unused_book_payloads():
    core = engine()
    submit(core, cl_ord_id="bid", side=Side.BUY, qty=5, price=100)
    snapshot = core.get_market_data_snapshot("TEST")
    metrics = exact_metrics("TEST")
    with patch.object(
        Instrument, "format_price", side_effect=AssertionError("unused formatting")
    ):
        metrics.update_book_metrics(snapshot)


def test_latency_diagnostic_retention_is_bounded_without_losing_aggregates():
    scheduler = LatencyScheduler()
    metrics = exact_metrics("TEST")
    tracker = LatencyTracker(scheduler, metrics)
    for number in range(10_025):
        tracker.record_cpu_duration(
            MATCH_LOOP_DURATION, number, None, 1_000, scheduler.current_time
        )
    assert len(tracker.measurements) == 10_000
    assert tracker.measurements[0].order_id == 25
    assert metrics._latency_counts[MATCH_LOOP_DURATION] == 10_025


def test_middle_of_fifo_cancel_and_reduction_do_not_walk_other_orders():
    core = engine()
    orders = [
        submit(core, cl_ord_id=f"fifo-{number}", side=Side.BUY, qty=5, price=100)
        for number in range(200)
    ]
    with patch.object(OrderQueue, "__iter__", side_effect=AssertionError("FIFO scan")):
        core.replace_order(command(core, orders[100], qty=3))
        cancel(core, orders[101])
    level = core.books["TEST"].bids[0]
    assert list(level.orders) == orders[:101] + orders[102:]
    assert level.level_qty_lots == 993
    core.books["TEST"].assert_invariants()


def test_full_audit_detects_price_lookup_corruption():
    core = engine()
    submit(core, cl_ord_id="audit", side=Side.BUY, qty=5, price=100)
    book = core.books["TEST"]
    book._level_index.clear()
    with pytest.raises(OrderBookInvariantError, match="Price level lookup mismatch"):
        book.assert_invariants()


def test_missing_ordered_level_is_rejected_before_quantity_or_queue_mutation():
    core = engine()
    order = submit(core, cl_ord_id="location", side=Side.BUY, qty=5, price=100)
    book = core.books["TEST"]
    level = book.bids.pop()
    with pytest.raises(OrderBookInvariantError, match="Indexed level missing"):
        book.resize_order(order, 4)
    assert order.order_qty_lots == level.level_qty_lots == 5
    assert list(level.orders) == [order]
    assert book.is_resting(order.id)


def test_seed_is_prepared_without_mutation_and_installed_without_fake_executions():
    core = engine()
    identifiers = RunIdentifierScope(1)
    commands = tuple(
        exact_command(
            cl_ord_id=f"seed-{number}",
            symbol="TEST",
            side=side,
            order_qty_lots=5,
            price_ticks=price,
            tif=TimeInForce.GTC,
            submitted_at=core.scheduler.current_time,
            participant_id=f"seed-{side.value}",
            session_id=f"seed-{side.value}",
        )
        for number, (side, price) in enumerate(((Side.BUY, 99), (Side.SELL, 101)))
    )
    reports, trades, displayed = [], [], []
    core.on_execution_report = reports.append
    core.on_trade = trades.append
    core.on_displayed_order_event = displayed.append
    initial = core.prepare_initial_book(commands, identifiers)
    assert core.orders == {} and core.active_order_count() == 0
    core.install_identifier_scope(identifiers)
    core.install_initial_book(initial)
    core.publish_initial_book(initial)
    assert core.active_order_count() == core.resting_order_count() == 2
    assert reports == trades == []
    assert len(displayed) == 2
    assert all(order.status == OrderStatus.NEW for order in core.orders.values())
    core.books["TEST"].assert_invariants()
    core.reset_run_state()
    assert core.active_order_count() == core.resting_order_count() == 0


def test_seed_reports_measure_only_observed_outbound_latency(caplog):
    core = engine()
    seed = submit(core, cl_ord_id="seed", side=Side.BUY, qty=5, price=100)
    metrics = exact_metrics("TEST")
    tracker = LatencyTracker(core.scheduler, metrics)
    tracker.register_seeded_orders([seed])
    reports = []
    core.on_execution_report = reports.append
    submit(core, cl_ord_id="aggressor", side=Side.SELL, qty=5, price=100)
    report = next(item for item in reports if item.order_id == seed.id)
    tracker.report_created(report)
    assert tracker.report_delivered(
        report, report.timestamp + timedelta(milliseconds=7)
    )
    assert metrics._latency_counts[SIM_OUTBOUND_LATENCY] == 1
    assert metrics._latency_counts[SIM_ENGINE_LATENCY] == 0
    assert metrics._latency_counts[SIM_END_TO_END_LATENCY] == 0
    assert tracker.complete_order(report)
    tracker.cleanup_order(seed.id, run_generation=seed.run_generation)
    assert tracker.orders == {}
    assert "Latency trace missing" not in caplog.text

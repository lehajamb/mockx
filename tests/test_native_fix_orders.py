"""Native list admission and replacement through the real FIX ingress path."""

from dataclasses import fields
from datetime import datetime, timedelta
import asyncio

import pytest

from src.mockx.gateway import fix
from src.mockx.metrics import SIM_ENGINE_LATENCY, SIM_INBOUND_LATENCY
from src.mockx.models import CancelReject, ExecutionType, OrderStatus
from tests.test_fix_ingress_pipeline import _cancel, _exchange, _new_order, _start, _stop


def order_list(*, list_id="LIST-1", sequence=1, members=None, side="1", prices=None):
    members = members or [(1, "L-1", "5"), (2, "L-2", "7")]
    message = fix.Message()
    message.getHeader().setField(fix.MsgType("E"))
    message.getHeader().setField(fix.MsgSeqNum(sequence))
    for tag, value in [(66, list_id), (394, "3"), (433, "1"), (414, "3"), (68, str(len(members)))]:
        message.setField(fix.StringField(tag, value))
    for list_sequence, cl_ord_id, quantity in members:
        group = fix.Group(73, 11)
        for tag, value in [(11, cl_ord_id), (67, str(list_sequence)), (55, "TEST"),
                           (54, side), (38, quantity), (40, "2"),
                           (44, str((prices or {}).get(cl_ord_id, 100))), (59, "1")]:
            group.setField(fix.StringField(tag, value))
        message.addGroup(group)
    return message


def replace_order(cl_ord_id, orig_cl_ord_id, sequence, *, quantity="4", price="100", list_id=None):
    message = _new_order(cl_ord_id, sequence)
    message.getHeader().setField(fix.MsgType("G"))
    message.setField(fix.OrigClOrdID(orig_cl_ord_id))
    message.setField(fix.StringField(38, quantity))
    message.setField(fix.StringField(44, price))
    if list_id is not None:
        message.setField(fix.StringField(66, list_id))
    return message


@pytest.mark.asyncio
async def test_list_is_one_ingress_receipt_and_expands_in_logical_member_order():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        exchange.fix_app.fromApp(order_list(members=[(2, "SECOND", "7"), (1, "FIRST", "5")]), sessions["CLIENT"])
        exchange.fix_app.fromApp(_new_order("AFTER-LIST", 2), sessions["CLIENT"])
        barrier = await exchange.fix_app.wait_for_ingress()
        assert barrier.through_sequence == 1
        queued = [event.data for event in sorted(exchange.scheduler.event_queue)
                  if event.event_type == "new_order_received"]
        assert [member.cl_ord_id for member in queued] == ["FIRST", "SECOND", "AFTER-LIST"]
        assert [member.list_seq_no for member in queued] == [1, 2, None]
        assert exchange.metrics._fix_message_counts[("E", "received")] == 1
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert [order.cl_ord_id for order in exchange.engine.books["TEST"].bids[0].orders] == [
            "FIRST", "SECOND", "AFTER-LIST"
        ]
        assert [report.list_id for report in delivered] == ["LIST-1", "LIST-1", None]
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_bad_identifiable_member_rejects_without_blocking_valid_siblings():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        exchange.fix_app.fromApp(order_list(members=[(1, "GOOD", "5"), (2, "BAD", "0")]), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        reports = {report.cl_ord_id: report for report in delivered}
        assert reports["GOOD"].exec_type == ExecutionType.NEW
        assert reports["BAD"].exec_type == ExecutionType.REJECTED
        assert reports["BAD"].list_id == "LIST-1"
        assert [order.cl_ord_id for order in exchange.engine.orders.values()] == ["GOOD"]
        assert exchange.session_registry.has_cl_ord_id(sessions["CLIENT"].toString(), "BAD")
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "duplicate_sequence", "invalid_id", "fragmented", "staged", "reporting", "count", "limit",
])
async def test_malformed_list_claims_no_member_ids(invalid):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    rejected = []
    exchange.fix_app.send_business_reject = lambda *args, **kwargs: rejected.append(kwargs)
    try:
        members = [(1, "GOOD", "5"), (2, "NEXT", "7")]
        if invalid == "duplicate_sequence":
            members[1] = (1, "NEXT", "7")
        if invalid == "invalid_id":
            members[1] = (2, "bad id", "7")
        message = order_list(members=members)
        tag_changes = {"fragmented": (893, "N"), "staged": (433, "2"), "reporting": (414, "1"),
                       "count": (68, "3"), "limit": (68, "101")}
        if invalid in tag_changes:
            tag, value = tag_changes[invalid]
            message.setField(fix.StringField(tag, value))
        exchange.fix_app.fromApp(message, sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert len(rejected) == 1 and rejected[0]["ref_msg_type"] == "E"
        assert exchange.engine.orders == {} and delivered == []
        assert not exchange.session_registry.has_cl_ord_id(sessions["CLIENT"].toString(), "GOOD")
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_list_id_cannot_be_reused_and_duplicate_member_id_is_rejected():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    rejects = []
    exchange.fix_app.send_business_reject = lambda *args, **kwargs: rejects.append(kwargs)
    try:
        exchange.fix_app.fromApp(order_list(members=[(1, "SAME", "5"), (2, "SAME", "7")]), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert [report.exec_type for report in delivered].count(ExecutionType.NEW) == 1
        assert [report.exec_type for report in delivered].count(ExecutionType.REJECTED) == 1
        exchange.fix_app.fromApp(order_list(sequence=2), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert len(rejects) == 1 and rejects[0]["business_reject_ref_id"] == "LIST-1"
        assert len(exchange.engine.orders) == 1
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_native_replace_and_cancel_chain_for_list_member():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    session = sessions["CLIENT"]
    try:
        exchange.fix_app.fromApp(order_list(), session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        exchange.fix_app.fromApp(replace_order("R-1", "L-1", 2, list_id="LIST-1"), session)
        exchange.fix_app.fromApp(replace_order("R-2", "R-1", 3, quantity="3", list_id="LIST-1"), session)
        cancellation = _cancel("C-1", "R-2", 4, qty=3)
        cancellation.setField(fix.StringField(66, "LIST-1"))
        exchange.fix_app.fromApp(cancellation, session)
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        own = [report for report in delivered if report.cl_ord_id in {"R-1", "R-2", "C-1"}]
        assert [report.exec_type for report in own] == [
            ExecutionType.REPLACED, ExecutionType.REPLACED, ExecutionType.CANCELED
        ]
        assert all(report.list_id == "LIST-1" for report in own)
        assert next(order for order in exchange.engine.orders.values() if order.cl_ord_id == "R-2").status == OrderStatus.CANCELLED
        metrics = {measurement.metric for measurement in exchange.latency_tracker.measurements
                   if "sim_replace_" in measurement.metric}
        assert len(metrics) == 4
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_replace_resolves_fills_at_processing_time_and_preserves_latency_stages():
    exchange = _exchange(inbound_ms=2, engine_ms=3, outbound_ms=4)
    sessions, delivered = await _start(exchange)
    try:
        start = exchange.scheduler.current_time
        exchange.fix_app.fromApp(_new_order("NEW", 1, qty=8), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=10))
        exchange.fix_app.fromApp(replace_order("R-1", "NEW", 2, quantity="4"), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=14))
        order = next(iter(exchange.engine.orders.values()))
        assert order.cl_ord_id == "NEW" and order.order_qty_lots == 8
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=15))
        assert order.cl_ord_id == "R-1" and order.order_qty_lots == 4
        await exchange.lifecycle.advance_to(start + timedelta(milliseconds=19))
        assert delivered[-1].exec_type == ExecutionType.REPLACED
        values = {measurement.metric: measurement.value for measurement in exchange.latency_tracker.measurements
                  if "sim_replace_" in measurement.metric}
        assert sorted(values.values()) == [2, 3, 4, 9]
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_invalid_replace_uses_cancel_reject_without_mutating_target():
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    try:
        exchange.fix_app.fromApp(_new_order("NEW", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        exchange.fix_app.fromApp(replace_order("R-BAD", "NEW", 2, quantity="0"), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        result = delivered[-1]
        assert isinstance(result, CancelReject) and result.response_to == "2"
        assert next(iter(exchange.engine.orders.values())).cl_ord_id == "NEW"
        assert exchange.session_registry.has_cl_ord_id(sessions["CLIENT"].toString(), "R-BAD")
        assert (await exchange.metrics.flush()).successful
        created = [record for record in exchange.metrics_persistence.sink.records
                   if dict(record.symbols).get("kind") == "REPLACE_REJECTED"
                   and {**dict(record.symbols), **dict(record.columns)}.get("cl_ord_id") == "R-BAD"]
        assert len(created) == 1, "Identifiable validation rejection must be durably captured before delivery"
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_internal_commit_cancellation_cannot_leave_a_mutated_run_active(monkeypatch):
    exchange = _exchange()
    sessions, delivered = await _start(exchange)
    original = exchange.journal.admit_command
    async def cancel_after_validation(effects):
        if effects.command_type == "order_process":
            raise asyncio.CancelledError("journal dependency cancelled after validation")
        return await original(effects)
    monkeypatch.setattr(exchange.journal, "admit_command", cancel_after_validation)
    try:
        exchange.fix_app.fromApp(_new_order("VALIDATED", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        order = next(iter(exchange.engine.orders.values()))
        assert order.status == OrderStatus.NEW
        assert exchange.engine.books["TEST"].is_resting(order.id)
        assert exchange.lifecycle.state.value == "RUN_FAILED"
        assert exchange.lifecycle.run_failure is not None
        assert [report.exec_type for report in delivered] == [ExecutionType.NEW]
    finally:
        await _stop(exchange)


@pytest.mark.asyncio
async def test_individual_pipeline_and_native_list_preserve_cross_client_fills_and_timings():
    async def exercise(mode):
        exchange = _exchange(inbound_ms=2, engine_ms=3, outbound_ms=4)
        sessions, delivered = await _start(exchange, "CLIENT", "CLIENT2")
        created, trades, delivery_times = [], [], []
        original_report = exchange.engine.on_execution_report
        original_trade = exchange.engine.on_trade
        original_delivery = exchange.fix_app.on_report_delivered

        def report_created(report):
            created.append(report)
            original_report(report)

        def trade_created(trade):
            trades.append(trade)
            original_trade(trade)

        def report_delivered(report, timestamp):
            delivery_times.append((report, timestamp))
            original_delivery(report, timestamp)

        exchange.engine.on_execution_report = report_created
        exchange.engine.on_trade = trade_created
        exchange.fix_app.on_report_delivered = report_delivered
        start = exchange.scheduler.current_time
        seeds = [("SELL-FIRST", 2, 100), ("SELL-SECOND", 3, 100), ("SELL-THIRD", 7, 101)]
        try:
            if mode == "native_E":
                exchange.fix_app.fromApp(order_list(
                    members=[(index, cl_ord_id, str(qty))
                             for index, (cl_ord_id, qty, _) in enumerate(seeds, 1)],
                    side=fix.Side_SELL,
                    prices={cl_ord_id: price for cl_ord_id, _, price in seeds},
                ), sessions["CLIENT"])
            else:
                for sequence, (cl_ord_id, qty, price) in enumerate(seeds, 1):
                    exchange.fix_app.fromApp(_new_order(
                        cl_ord_id, sequence, side=fix.Side_SELL, qty=qty, price=price,
                    ), sessions["CLIENT"])
                    if mode == "individual_D":
                        # Admission barriers keep all three simulated receipts equal.
                        await exchange.fix_app.wait_for_ingress()
                        assert exchange.scheduler.current_time == start
            await exchange.fix_app.wait_for_ingress()
            await exchange.lifecycle.advance_to(start + timedelta(milliseconds=10))
            assert [report.cl_ord_id for report in delivered] == [item[0] for item in seeds]
            assert [timestamp - start for _, timestamp in delivery_times] == [
                timedelta(milliseconds=9)
            ] * 3

            exchange.fix_app.fromApp(_new_order(
                "STRATEGY-BUY", 1, qty=8, price=101,
            ), sessions["CLIENT2"])
            await exchange.fix_app.wait_for_ingress()
            await exchange.lifecycle.advance_to(start + timedelta(milliseconds=19))

            book = exchange.engine.books["TEST"]
            book.assert_invariants()
            assert not book.bids
            leftover = [(level.price_ticks, level.level_qty_lots,
                         tuple((order.cl_ord_id, order.leaves_qty_lots)
                               for order in level.orders)) for level in book.asks]
            assert leftover == [(101, 4, (("SELL-THIRD", 4),))]
            assert len(created) == 9 and delivered == created
            assert [report for report, _ in delivery_times] == created
            assert len({report.exec_id for report in created}) == 9
            assert [report.cl_ord_id for report in created] == [
                "SELL-FIRST", "SELL-SECOND", "SELL-THIRD",
                "SELL-FIRST", "STRATEGY-BUY", "SELL-SECOND", "STRATEGY-BUY",
                "SELL-THIRD", "STRATEGY-BUY",
            ]
            assert [report.exec_type for report in created] == (
                [ExecutionType.NEW] * 3 + [ExecutionType.TRADE] * 6
            )
            assert [(report.ord_status, report.cum_qty_lots, report.leaves_qty_lots,
                     report.last_price_ticks, report.last_qty_lots)
                    for report in created[3:]] == [
                (OrderStatus.FILLED, 2, 0, 100, 2),
                (OrderStatus.PARTIAL, 2, 6, 100, 2),
                (OrderStatus.FILLED, 3, 0, 100, 3),
                (OrderStatus.PARTIAL, 5, 3, 100, 3),
                (OrderStatus.PARTIAL, 3, 4, 101, 3),
                (OrderStatus.FILLED, 8, 0, 101, 3),
            ]
            assert [(trade.id, trade.passive_order_id, trade.aggressor_order_id,
                     trade.price_ticks, trade.qty_lots, trade.notional_tick_lots)
                    for trade in trades] == [
                (1, 1, 4, 100, 2, 200), (2, 2, 4, 100, 3, 300), (3, 3, 4, 101, 3, 303),
            ]
            assert [report.timestamp - start for report in created] == (
                [timedelta(milliseconds=5)] * 3 + [timedelta(milliseconds=15)] * 6
            )
            assert [timestamp - start for _, timestamp in delivery_times] == (
                [timedelta(milliseconds=9)] * 3 + [timedelta(milliseconds=19)] * 6
            )
            assert all(timestamp - report.timestamp == timedelta(milliseconds=4)
                       for report, timestamp in delivery_times)
            assert all(timestamp - report.submitted_at == timedelta(milliseconds=9)
                       for report, timestamp in delivery_times if report.cl_ord_id == "STRATEGY-BUY")
            inbound = [measurement.value for measurement in exchange.latency_tracker.measurements
                       if measurement.metric == SIM_INBOUND_LATENCY]
            engine = [measurement.value for measurement in exchange.latency_tracker.measurements
                      if measurement.metric == SIM_ENGINE_LATENCY]
            assert inbound == [2] * 4 and engine == [3] * 4

            def normalized(value, excluded):
                return tuple((field.name, getattr(value, field.name) - start
                              if isinstance(getattr(value, field.name), datetime)
                              else getattr(value, field.name))
                             for field in fields(value) if field.name not in excluded)

            # Run-scoped wire identities and ListID differ by design; all business
            # fields, logical IDs, ownership, and relative timestamps must agree.
            return (
                leftover,
                [normalized(report, {"exec_id", "exchange_order_id", "exchange_trade_id",
                                     "run_id", "list_id"}) for report in created],
                [normalized(trade, {"exchange_trade_id"}) for trade in trades],
                [timestamp - start for _, timestamp in delivery_times],
            )
        finally:
            await _stop(exchange)

    individual = await exercise("individual_D")
    assert await exercise("pipelined_D") == individual
    assert await exercise("native_E") == individual


@pytest.mark.asyncio
async def test_unknown_cancel_delivered_after_restart_keeps_original_run_uuid():
    exchange = _exchange(outbound_ms=10)
    sessions, delivered = await _start(exchange)
    original_run = exchange.lifecycle.run_id
    try:
        exchange.fix_app.fromApp(_cancel("UNKNOWN-CANCEL", "MISSING", 1), sessions["CLIENT"])
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        assert delivered == []
        assert exchange.fix_app.delivery_queue.snapshot().queue_depth == 1

        await exchange.lifecycle.end_session()
        await exchange.lifecycle.start_session("next-run")
        next_run = exchange.lifecycle.run_id
        assert next_run != original_run
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time + timedelta(milliseconds=10))
        assert len(delivered) == 1 and isinstance(delivered[0], CancelReject)
        rejection = delivered[0]
        assert rejection.cancel_cl_ord_id == "UNKNOWN-CANCEL" and rejection.order_id is None
        assert (await exchange.metrics.flush()).successful
        persisted = [{**record.symbol_map, **record.column_map}
                     for record in exchange.metrics_persistence.sink.records]
        delivery = [row for row in persisted if row.get("metric_name") == "report_delivered"
                    and row.get("exec_id") == rejection.reject_id]
        assert len(delivery) == 1
        assert delivery[0]["run_id"] == original_run
        assert rejection.run_id == original_run
        creation = [row for row in persisted if row["kind"] == "CANCEL_REJECTED"
                    and row.get("exec_id") == rejection.reject_id]
        assert len(creation) == 1 and creation[0]["run_id"] == original_run
        assert not any(row.get("exec_id") == rejection.reject_id and row["run_id"] == next_run
                       for row in persisted)
    finally:
        await _stop(exchange)

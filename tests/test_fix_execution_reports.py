"""Pure FIX mappings and per-fill execution-report semantics."""

from dataclasses import replace

import pytest

from src.mockx.fix_fields import (
    cancel_reject_fields,
    execution_report_fields,
)
from src.mockx.models import (
    CancelOrderCommand,
    CancelRejectReason,
    ExecutionType,
    Order,
    OrderStatus,
    Side,
    TimeInForce,
    create_execution_report,
)
from tests.lifecycle_helpers import NOW, cancel, make_engine, submit


def reports_for(reports, order_id, exec_type=None):
    return [
        report
        for report in reports
        if (
            report.order_id == order_id
            and (exec_type is None or report.exec_type == exec_type)
        )
    ]


def test_fix_mapping_for_new_partial_fill_cancel_and_reject():
    engine, reports, _, _ = make_engine()
    new_id = submit(
        engine,
        cl_ord_id="FIX-NEW",
        side=Side.SELL,
        qty=10,
        price=101,
    )
    partial_id = submit(
        engine,
        cl_ord_id="FIX-PARTIAL-PASSIVE",
        side=Side.SELL,
        qty=10,
        price=100,
    )
    filled_id = submit(
        engine,
        cl_ord_id="FIX-FILLED",
        side=Side.BUY,
        qty=4,
        price=100,
    )
    assert cancel(
        engine,
        new_id,
        cancel_cl_ord_id="CXL-FIX-NEW",
    ) is None

    rejected = Order(
        id=999,
        cl_ord_id="FIX-REJECTED",
        symbol="TEST",
        side=Side.BUY,
        qty=3,
        price=99,
        tif=TimeInForce.GTC,
        ts_submitted=NOW,
    )
    rejected.cancel_remaining()
    rejected.transition_to(OrderStatus.REJECTED)
    rejected.reject_reason = "test rejection"
    rejected_report = create_execution_report(
        rejected,
        exec_type=ExecutionType.REJECTED,
        timestamp=NOW,
        exec_id="G00000000-E00000001",
    )

    new_fields = execution_report_fields(
        next(
            report
            for report in reports_for(reports, new_id)
            if report.exec_type == ExecutionType.NEW
        )
    )
    partial_fields = execution_report_fields(
        reports_for(reports, partial_id, ExecutionType.TRADE)[0]
    )
    fill_fields = execution_report_fields(
        reports_for(reports, filled_id, ExecutionType.TRADE)[0]
    )
    cancel_fields = execution_report_fields(
        reports_for(reports, new_id, ExecutionType.CANCELED)[0]
    )
    reject_fields = execution_report_fields(rejected_report)

    assert (
        new_fields["ExecType"],
        new_fields["OrdStatus"],
        new_fields["CumQty"],
        new_fields["LeavesQty"],
    ) == ("0", "0", 0, 10)
    assert partial_fields["ExecType"] == "F"
    assert partial_fields["OrdStatus"] == "1"
    assert partial_fields["LastQty"] == 4
    assert partial_fields["LastPx"] == 100
    assert partial_fields["TradeID"]
    assert partial_fields["CumQty"] == 4
    assert partial_fields["LeavesQty"] == 6
    assert fill_fields["ExecType"] == "F"
    assert fill_fields["OrdStatus"] == "2"
    assert fill_fields["CumQty"] == fill_fields["OrderQty"] == 4
    assert fill_fields["LeavesQty"] == 0
    assert fill_fields["TradeID"] == partial_fields["TradeID"]
    assert cancel_fields["ExecType"] == "4"
    assert cancel_fields["OrdStatus"] == "4"
    assert cancel_fields["ClOrdID"] == "CXL-FIX-NEW"
    assert cancel_fields["OrigClOrdID"] == "FIX-NEW"
    assert cancel_fields["LeavesQty"] == 0
    assert cancel_fields["CanceledQty"] == 10
    assert reject_fields["ExecType"] == "8"
    assert reject_fields["OrdStatus"] == "8"
    assert reject_fields["LeavesQty"] == 0


def test_market_fix_report_omits_price_but_uses_fill_prices():
    engine, reports, _, _ = make_engine()
    submit(
        engine,
        cl_ord_id="FIX-MARKET-PASSIVE",
        side=Side.SELL,
        qty=5,
        price=100,
    )
    market_id = submit(
        engine,
        cl_ord_id="FIX-MARKET",
        side=Side.BUY,
        qty=5,
        price=None,
    )

    fields = execution_report_fields(
        reports_for(reports, market_id, ExecutionType.TRADE)[0]
    )
    assert "Price" not in fields
    assert fields["LastPx"] == 100
    assert fields["AvgPx"] == 100


def test_cancel_reject_mapping_uses_request_and_known_order_snapshot():
    engine, _, _, _ = make_engine()
    order_id = submit(
        engine,
        cl_ord_id="FIX-CXL-FILLED-PASSIVE",
        side=Side.SELL,
        qty=2,
        price=100,
    )
    submit(
        engine,
        cl_ord_id="FIX-CXL-FILLED-HIT",
        side=Side.BUY,
        qty=2,
        price=100,
    )
    command = CancelOrderCommand(
        cancel_cl_ord_id="CXL-TOO-LATE",
        orig_cl_ord_id="FIX-CXL-FILLED-PASSIVE",
        symbol="TEST",
        side=Side.SELL,
        qty=2,
        requested_at=NOW,
        session_id="FIX.4.4:CLIENT->MOCKX",
        session_alias="CLIENT",
    )

    rejection = engine.cancel_order(command)
    fields = cancel_reject_fields(rejection)

    assert rejection.reason == CancelRejectReason.TOO_LATE_TO_CANCEL
    assert fields["ClOrdID"] == "CXL-TOO-LATE"
    assert fields["OrigClOrdID"] == "FIX-CXL-FILLED-PASSIVE"
    assert fields["OrderID"] == "G00000000-O00000001"
    assert fields["OrdStatus"] == "2"
    assert fields["CxlRejReason"] == 0
    assert fields["CxlRejResponseTo"] == "1"
    assert "terminal state" in fields["Text"]


def test_unknown_cancel_reject_mapping_has_request_identity():
    engine, _, _, _ = make_engine()
    command = CancelOrderCommand(
        cancel_cl_ord_id="CXL-UNKNOWN-FIX",
        orig_cl_ord_id="UNKNOWN-FIX",
        symbol="TEST",
        side=Side.BUY,
        qty=1,
        requested_at=NOW,
        session_id="FIX.4.4:CLIENT->MOCKX",
        session_alias="CLIENT",
    )

    fields = cancel_reject_fields(engine.cancel_order(command))

    assert fields["ClOrdID"] == "CXL-UNKNOWN-FIX"
    assert fields["OrigClOrdID"] == "UNKNOWN-FIX"
    assert fields["OrdStatus"] == "8"
    assert fields["CxlRejReason"] == 1
    assert fields["CxlRejResponseTo"] == "1"
    assert "Order not found" in fields["Text"]
    assert "OrderID" not in fields


def test_report_order_is_passive_then_aggressor_for_each_fill():
    engine, reports, _, _ = make_engine()
    passive_ids = [
        submit(
            engine,
            cl_ord_id="ORDER-A",
            side=Side.SELL,
            qty=3,
            price=100,
        ),
        submit(
            engine,
            cl_ord_id="ORDER-B",
            side=Side.SELL,
            qty=4,
            price=100,
        ),
    ]
    report_start = len(reports)
    aggressor_id = submit(
        engine,
        cl_ord_id="ORDER-AGGRESSOR",
        side=Side.BUY,
        qty=7,
        price=100,
    )

    emitted = reports[report_start:]
    assert [report.order_id for report in emitted] == [
        passive_ids[0],
        aggressor_id,
        passive_ids[1],
        aggressor_id,
    ]
    assert all(
        report.exec_type == ExecutionType.TRADE for report in emitted
    )
    assert emitted[0].trade_id == emitted[1].trade_id
    assert emitted[2].trade_id == emitted[3].trade_id
    assert emitted[0].trade_id != emitted[2].trade_id
    assert all(
        report.timestamp == engine.scheduler.current_time
        for report in emitted
    )
    assert [report.exec_id for report in emitted] == [
        "G00000000-E00000003",
        "G00000000-E00000004",
        "G00000000-E00000005",
        "G00000000-E00000006",
    ]


def test_passive_order_reports_each_separate_fill_cumulatively():
    engine, reports, _, _ = make_engine()
    passive_id = submit(
        engine,
        cl_ord_id="PASSIVE-MULTI",
        side=Side.SELL,
        qty=10,
        price=100,
    )
    submit(
        engine,
        cl_ord_id="PASSIVE-HIT-1",
        side=Side.BUY,
        qty=3,
        price=100,
    )
    submit(
        engine,
        cl_ord_id="PASSIVE-HIT-2",
        side=Side.BUY,
        qty=2,
        price=100,
    )

    passive_reports = reports_for(
        reports,
        passive_id,
        ExecutionType.TRADE,
    )
    assert [report.last_qty for report in passive_reports] == [3, 2]
    assert [report.last_px for report in passive_reports] == [100, 100]
    assert [report.cum_qty for report in passive_reports] == [3, 5]
    assert [report.leaves_qty for report in passive_reports] == [7, 5]
    assert [report.avg_px for report in passive_reports] == [100, 100]
    assert [report.ord_status for report in passive_reports] == [
        OrderStatus.PARTIAL,
        OrderStatus.PARTIAL,
    ]


def test_execution_report_invariants_reject_impossible_snapshots():
    engine, reports, _, _ = make_engine()
    new_id = submit(
        engine,
        cl_ord_id="INVALID-NEW",
        side=Side.SELL,
        qty=10,
        price=101,
    )
    partial_id = submit(
        engine,
        cl_ord_id="INVALID-PARTIAL",
        side=Side.SELL,
        qty=10,
        price=100,
    )
    filled_id = submit(
        engine,
        cl_ord_id="INVALID-FILLED",
        side=Side.BUY,
        qty=4,
        price=100,
    )
    assert cancel(engine, new_id) is None

    new_report = reports_for(reports, new_id, ExecutionType.NEW)[0]
    partial_report = reports_for(
        reports,
        partial_id,
        ExecutionType.TRADE,
    )[0]
    filled_report = reports_for(
        reports,
        filled_id,
        ExecutionType.TRADE,
    )[0]
    cancel_report = reports_for(
        reports,
        new_id,
        ExecutionType.CANCELED,
    )[0]

    invalid_builders = [
        lambda: replace(partial_report, last_qty=0),
        lambda: replace(partial_report, last_px=None),
        lambda: replace(
            filled_report,
            cum_qty=3,
            leaves_qty=1,
        ),
        lambda: replace(
            cancel_report,
            leaves_qty=1,
            canceled_qty=9,
        ),
        lambda: replace(new_report, cum_qty=1, leaves_qty=9),
        lambda: replace(new_report, leaves_qty=9),
        lambda: replace(
            partial_report,
            last_qty=partial_report.cum_qty + 1,
        ),
        lambda: replace(
            cancel_report,
            canceled_qty=-1,
            leaves_qty=11,
        ),
        lambda: replace(
            cancel_report,
            exec_type=ExecutionType.TRADE,
        ),
        lambda: replace(
            cancel_report,
            cum_qty=cancel_report.order_qty,
            canceled_qty=0,
            avg_px=100,
        ),
    ]

    for build in invalid_builders:
        with pytest.raises(ValueError):
            build()


def test_mapped_fields_remain_immutable_after_live_order_mutation():
    engine, reports, _, _ = make_engine()
    order_id = submit(
        engine,
        cl_ord_id="FIX-SNAPSHOT",
        side=Side.SELL,
        qty=5,
        price=100,
    )
    new_report = reports_for(reports, order_id, ExecutionType.NEW)[0]
    submit(
        engine,
        cl_ord_id="FIX-SNAPSHOT-HIT",
        side=Side.BUY,
        qty=5,
        price=100,
    )

    fields = execution_report_fields(new_report)
    assert engine.orders[order_id].status == OrderStatus.FILLED
    assert fields["ExecType"] == "0"
    assert fields["OrdStatus"] == "0"
    assert fields["CumQty"] == 0
    assert fields["LeavesQty"] == 5

"""Pure FIX field preparation for immutable execution-report snapshots."""

from typing import Dict

from .models import (
    CancelReject,
    CancelRejectReason,
    ExecutionReport,
    ExecutionType,
    OrderStatus,
    Side,
    TimeInForce,
)


EXEC_TYPE_FIX_VALUES = {
    ExecutionType.NEW: "0",
    ExecutionType.TRADE: "F",
    ExecutionType.CANCELED: "4",
    ExecutionType.REJECTED: "8",
}

ORD_STATUS_FIX_VALUES = {
    OrderStatus.NEW: "0",
    OrderStatus.PARTIAL: "1",
    OrderStatus.FILLED: "2",
    OrderStatus.CANCELLED: "4",
    OrderStatus.REJECTED: "8",
}

SIDE_FIX_VALUES = {
    Side.BUY: "1",
    Side.SELL: "2",
}

TIF_FIX_VALUES = {
    TimeInForce.GTC: "1",
    TimeInForce.IOC: "3",
}

CANCEL_REJECT_REASON_FIX_VALUES = {
    CancelRejectReason.UNKNOWN_ORDER: 1,
    CancelRejectReason.TOO_LATE_TO_CANCEL: 0,
    CancelRejectReason.ALREADY_CANCELED: 0,
    CancelRejectReason.SESSION_MISMATCH: 1,
    CancelRejectReason.INVALID_REQUEST: 99,
}


def execution_report_fields(report: ExecutionReport) -> Dict[str, object]:
    """Return FIX-ready primitive values using only ``report`` fields."""
    fields: Dict[str, object] = {
        "ClOrdID": report.cl_ord_id,
        "ExecID": report.exec_id,
        "ExecType": EXEC_TYPE_FIX_VALUES[report.exec_type],
        "OrdStatus": ORD_STATUS_FIX_VALUES[report.ord_status],
        "OrderID": str(report.order_id),
        "Symbol": report.symbol,
        "Side": SIDE_FIX_VALUES[report.side],
        "LeavesQty": report.leaves_qty,
        "CumQty": report.cum_qty,
        "AvgPx": report.avg_px,
        "OrderQty": report.order_qty,
        "CanceledQty": report.canceled_qty,
        "TimeInForce": TIF_FIX_VALUES[report.tif],
    }
    if report.orig_cl_ord_id is not None:
        fields["OrigClOrdID"] = report.orig_cl_ord_id
    if report.trade_id is not None:
        fields["TradeID"] = str(report.trade_id)
    if report.order_price is not None:
        fields["Price"] = report.order_price
    if report.last_px is not None:
        fields["LastPx"] = report.last_px
    if report.last_qty is not None:
        fields["LastQty"] = report.last_qty
    text = report.text or report.reject_reason
    if text:
        fields["Text"] = text
    return fields


def cancel_reject_fields(reject: CancelReject) -> Dict[str, object]:
    """Return FIX-ready cancel-reject fields from one immutable snapshot."""
    fields: Dict[str, object] = {
        "ClOrdID": reject.cancel_cl_ord_id,
        "OrigClOrdID": reject.orig_cl_ord_id,
        "OrdStatus": (
            ORD_STATUS_FIX_VALUES[reject.ord_status]
            if reject.ord_status is not None
            else ORD_STATUS_FIX_VALUES[OrderStatus.REJECTED]
        ),
        "CxlRejReason": CANCEL_REJECT_REASON_FIX_VALUES[reject.reason],
        "CxlRejResponseTo": "1",
        "Text": reject.text,
    }
    if reject.order_id is not None:
        fields["OrderID"] = str(reject.order_id)
    return fields

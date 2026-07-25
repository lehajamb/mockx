"""Pure FIX field preparation for immutable execution-report snapshots."""

from typing import Dict

from .models import ExecutionReport, OrderStatus, Side, TimeInForce


EXEC_TYPE_FIX_VALUES = {
    "NEW": "0",
    "PARTIAL": "F",
    "FILLED": "F",
    "CANCELLED": "4",
    "REJECTED": "8",
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


def execution_report_fields(report: ExecutionReport) -> Dict[str, object]:
    """Return FIX-ready primitive values using only ``report`` fields."""
    fields: Dict[str, object] = {
        "ClOrdID": report.cl_ord_id,
        "ExecType": EXEC_TYPE_FIX_VALUES[report.exec_type],
        "OrdStatus": ORD_STATUS_FIX_VALUES[report.ord_status],
        "OrderID": str(report.order_id),
        "Symbol": report.symbol,
        "Side": SIDE_FIX_VALUES[report.side],
        "LeavesQty": report.leaves_qty,
        "CumQty": report.cum_qty,
        "AvgPx": report.avg_px,
        "OrderQty": report.order_qty,
        "TimeInForce": TIF_FIX_VALUES[report.tif],
    }
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

"""Pure FIX field preparation for immutable execution-report snapshots."""

from typing import Dict

from .models import (
    CancelReject,
    CancelRejectReason,
    ExecutionReport,
    ExecutionType,
    OrderType,
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
    OrderStatus.PENDING_NEW: "A",
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

ORDER_TYPE_FIX_VALUES = {
    OrderType.MARKET: "1",
    OrderType.LIMIT: "2",
}

ORDER_REJECT_REASON_FIX_VALUES = {
    "UNKNOWN_SYMBOL": 1,
    "DUPLICATE_CL_ORD_ID": 6,
}

CANCEL_REJECT_REASON_FIX_VALUES = {
    CancelRejectReason.UNKNOWN_ORDER: 1,
    CancelRejectReason.TOO_LATE_TO_CANCEL: 0,
    CancelRejectReason.ALREADY_CANCELED: 0,
    CancelRejectReason.SESSION_MISMATCH: 1,
    CancelRejectReason.INVALID_REQUEST: 99,
    CancelRejectReason.EXCHANGE_UNAVAILABLE: 99,
}


def execution_report_fields(report: ExecutionReport) -> Dict[str, object]:
    """Return FIX-ready primitive values using only ``report`` fields."""
    instrument = report.instrument
    fields: Dict[str, object] = {
        "ClOrdID": report.cl_ord_id,
        "ExecID": report.exec_id,
        "ExecType": EXEC_TYPE_FIX_VALUES[report.exec_type],
        "OrdStatus": ORD_STATUS_FIX_VALUES[report.ord_status],
        "OrderID": (
            report.exchange_order_id
            or f"G{report.run_generation:08d}-O{report.order_id:08d}"
        ),
        "Symbol": report.symbol,
        "Side": SIDE_FIX_VALUES[report.side],
        "LeavesQty": instrument.format_quantity(report.leaves_qty_lots),
        "CumQty": instrument.format_quantity(report.cum_qty_lots),
        "AvgPx": instrument.format_average_price(
            report.filled_notional_tick_lots,
            report.cum_qty_lots,
        ),
        "OrderQty": instrument.format_quantity(report.order_qty_lots),
        "CanceledQty": instrument.format_quantity(
            report.canceled_qty_lots
        ),
        "OrdType": ORDER_TYPE_FIX_VALUES[report.order_type],
        "TimeInForce": TIF_FIX_VALUES[report.tif],
    }
    if report.orig_cl_ord_id is not None:
        fields["OrigClOrdID"] = report.orig_cl_ord_id
    if report.trade_id is not None:
        fields["TradeID"] = (
            report.exchange_trade_id
            or f"G{report.run_generation:08d}-T{report.trade_id:08d}"
        )
    if report.order_price_ticks is not None:
        fields["Price"] = instrument.format_price(report.order_price_ticks)
    if report.last_price_ticks is not None:
        fields["LastPx"] = instrument.format_price(report.last_price_ticks)
    if report.last_qty_lots is not None:
        fields["LastQty"] = instrument.format_quantity(report.last_qty_lots)
    text = report.text or report.reject_reason
    if text:
        fields["Text"] = text
    if report.exec_type == ExecutionType.REJECTED:
        code = str(text or "").split(":", 1)[0].strip("[] ")
        fields["OrdRejReason"] = ORDER_REJECT_REASON_FIX_VALUES.get(
            code,
            99,
        )
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
        fields["OrderID"] = (
            reject.exchange_order_id
            or f"G{0:08d}-O{reject.order_id:08d}"
        )
    return fields

"""Shared QuickFIX helpers for the runnable examples."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable, Iterable, Mapping, Optional

try:
    import quickfix as fix
except ImportError:  # Keep example ``--help`` commands usable.
    fix = None


ApplicationBase = fix.Application if fix is not None else object

STATUS_NAMES = {
    "0": "NEW",
    "1": "PARTIAL",
    "2": "FILLED",
    "4": "CANCELLED",
    "8": "REJECTED",
}
TERMINAL_STATUSES = frozenset({"FILLED", "CANCELLED", "REJECTED"})


class ReportTracker:
    """Wait until every client order reaches one expected status."""

    def __init__(
        self,
        expected: Mapping[str, Iterable[str]],
    ) -> None:
        self._expected = {
            cl_ord_id: frozenset(statuses)
            for cl_ord_id, statuses in expected.items()
        }
        self._observed: dict[str, set[str]] = {
            cl_ord_id: set() for cl_ord_id in self._expected
        }
        self._condition = threading.Condition()

    @property
    def complete(self) -> bool:
        with self._condition:
            return self._complete_unlocked()

    def _complete_unlocked(self) -> bool:
        return all(
            bool(self._observed[cl_ord_id] & statuses)
            for cl_ord_id, statuses in self._expected.items()
        )

    def expect(self, cl_ord_id: str, statuses: Iterable[str]) -> None:
        expected_statuses = frozenset(statuses)
        if not expected_statuses:
            raise ValueError("expected statuses cannot be empty")
        with self._condition:
            if cl_ord_id in self._expected:
                raise ValueError(f"duplicate expected ClOrdID: {cl_ord_id}")
            self._expected[cl_ord_id] = expected_statuses
            self._observed[cl_ord_id] = set()
            self._condition.notify_all()

    def record(self, cl_ord_id: str, status: str) -> None:
        with self._condition:
            if cl_ord_id in self._observed:
                self._observed[cl_ord_id].add(status)
            self._condition.notify_all()

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(timeout, 0)
        with self._condition:
            while not self._complete_unlocked():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


@dataclass(frozen=True)
class FixReport:
    """Small immutable projection of an execution or cancel-reject message."""

    message_type: str
    sender_comp_id: str
    cl_ord_id: str
    orig_cl_ord_id: Optional[str]
    status: str
    exec_type: Optional[str]
    leaves_qty: Optional[str]
    last_price: Optional[str]
    last_qty: Optional[str]
    text: Optional[str]


def require_quickfix() -> None:
    if fix is None:
        raise RuntimeError(
            "quickfix is not installed; install requirements.txt first"
        )


def sender_comp_id(session_id) -> str:
    return session_id.getSenderCompID().getValue()


def _field_value(message, field) -> str:
    message.getField(field)
    return str(field.getValue())


def _optional_field_value(message, field) -> Optional[str]:
    if not message.isSetField(field.getField()):
        return None
    return _field_value(message, field)


class MultiSessionFIXApplication(ApplicationBase):
    """QuickFIX application that tracks several initiator sessions."""

    def __init__(self, *, print_reports: bool = False) -> None:
        super().__init__()
        self.print_reports = print_reports
        self._condition = threading.Condition()
        self._sessions: dict[str, object] = {}
        self._listeners: list[Callable[[FixReport], None]] = []

    def add_listener(self, listener: Callable[[FixReport], None]) -> None:
        with self._condition:
            self._listeners.append(listener)

    def onCreate(self, sessionID):
        return None

    def onLogon(self, sessionID):
        role = sender_comp_id(sessionID)
        with self._condition:
            self._sessions[role] = sessionID
            self._condition.notify_all()
        print(f"Logged on: {sessionID}")

    def onLogout(self, sessionID):
        role = sender_comp_id(sessionID)
        with self._condition:
            current = self._sessions.get(role)
            if current is not None and current.toString() == sessionID.toString():
                self._sessions.pop(role, None)
            self._condition.notify_all()
        print(f"Logged out: {sessionID}")

    def toAdmin(self, message, sessionID):
        return None

    def fromAdmin(self, message, sessionID):
        return None

    def toApp(self, message, sessionID):
        return None

    def wait_for_sessions(
        self,
        roles: Iterable[str],
        timeout: float,
    ) -> bool:
        expected = frozenset(roles)
        deadline = time.monotonic() + timeout
        with self._condition:
            while not expected <= self._sessions.keys():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def session(self, role: str):
        with self._condition:
            session_id = self._sessions.get(role)
        if session_id is None:
            raise RuntimeError(f"FIX session is not logged on: {role}")
        return session_id

    def fromApp(self, message, sessionID):
        report = self._parse_report(message, sessionID)
        if report is None:
            return
        if self.print_reports:
            detail = (
                f" {report.last_qty} @ {report.last_price}"
                if report.last_qty and report.last_price
                else ""
            )
            print(
                f"{report.sender_comp_id} {report.message_type} "
                f"{report.cl_ord_id}: {report.status}{detail}"
            )
        with self._condition:
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(report)

    def _parse_report(self, message, sessionID) -> Optional[FixReport]:
        msg_type = fix.MsgType()
        message.getHeader().getField(msg_type)
        value = msg_type.getValue()
        if value == fix.MsgType_ExecutionReport:
            cl_ord_id = _field_value(message, fix.ClOrdID())
            ord_status = _field_value(message, fix.OrdStatus())
            return FixReport(
                message_type="ExecutionReport",
                sender_comp_id=sender_comp_id(sessionID),
                cl_ord_id=cl_ord_id,
                orig_cl_ord_id=_optional_field_value(
                    message, fix.OrigClOrdID()
                ),
                status=STATUS_NAMES.get(ord_status, ord_status),
                exec_type=_field_value(message, fix.ExecType()),
                leaves_qty=_optional_field_value(
                    message, fix.StringField(151)
                ),
                last_price=_optional_field_value(message, fix.LastPx()),
                last_qty=_optional_field_value(message, fix.LastQty()),
                text=_optional_field_value(message, fix.Text()),
            )
        if value == fix.MsgType_OrderCancelReject:
            ord_status = _field_value(message, fix.OrdStatus())
            return FixReport(
                message_type="OrderCancelReject",
                sender_comp_id=sender_comp_id(sessionID),
                cl_ord_id=_field_value(message, fix.ClOrdID()),
                orig_cl_ord_id=_field_value(message, fix.OrigClOrdID()),
                status=STATUS_NAMES.get(ord_status, ord_status),
                exec_type=None,
                leaves_qty=None,
                last_price=None,
                last_qty=None,
                text=_optional_field_value(message, fix.Text()),
            )
        return None


def _transact_time():
    return fix.TransactTime()


def _send(message, session_id) -> None:
    if not fix.Session.sendToTarget(message, session_id):
        raise RuntimeError(f"QuickFIX rejected local submission to {session_id}")


def send_limit_order(
    session_id,
    *,
    cl_ord_id: str,
    symbol: str,
    side: str,
    quantity: str,
    price: str,
    tif: str | None = None,
) -> None:
    require_quickfix()
    message = fix.Message()
    message.getHeader().setField(fix.MsgType(fix.MsgType_NewOrderSingle))
    message.setField(fix.ClOrdID(cl_ord_id))
    message.setField(fix.Symbol(symbol))
    message.setField(fix.Side(side))
    message.setField(fix.StringField(38, quantity))
    message.setField(fix.OrdType(fix.OrdType_LIMIT))
    message.setField(fix.StringField(44, price))
    message.setField(
        fix.TimeInForce(tif or fix.TimeInForce_GOOD_TILL_CANCEL)
    )
    message.setField(_transact_time())
    _send(message, session_id)


def send_market_order(
    session_id,
    *,
    cl_ord_id: str,
    symbol: str,
    side: str,
    quantity: str,
) -> None:
    require_quickfix()
    message = fix.Message()
    message.getHeader().setField(fix.MsgType(fix.MsgType_NewOrderSingle))
    message.setField(fix.ClOrdID(cl_ord_id))
    message.setField(fix.Symbol(symbol))
    message.setField(fix.Side(side))
    message.setField(fix.StringField(38, quantity))
    message.setField(fix.OrdType(fix.OrdType_MARKET))
    message.setField(
        fix.TimeInForce(fix.TimeInForce_IMMEDIATE_OR_CANCEL)
    )
    message.setField(_transact_time())
    _send(message, session_id)


def send_cancel(
    session_id,
    *,
    cl_ord_id: str,
    orig_cl_ord_id: str,
    symbol: str,
    side: str,
    quantity: str,
) -> None:
    require_quickfix()
    message = fix.Message()
    message.getHeader().setField(
        fix.MsgType(fix.MsgType_OrderCancelRequest)
    )
    message.setField(fix.ClOrdID(cl_ord_id))
    message.setField(fix.OrigClOrdID(orig_cl_ord_id))
    message.setField(fix.Symbol(symbol))
    message.setField(fix.Side(side))
    message.setField(fix.StringField(38, quantity))
    message.setField(_transact_time())
    _send(message, session_id)

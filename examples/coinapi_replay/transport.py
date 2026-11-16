"""Real QuickFIX transport with bounded callback storage and explicit readiness."""

from __future__ import annotations

import asyncio
from collections import deque
import threading
import time
from pathlib import Path

from examples.fix_support import (
    MultiSessionFIXApplication,
    fix,
    require_quickfix,
    send_cancel,
    send_limit_order,
    send_replace,
)


class ReplayFIXApplication(MultiSessionFIXApplication):
    def __init__(self, *, capacity: int = 8192):
        super().__init__()
        if capacity <= 0:
            raise ValueError("FIX callback capacity must be positive")
        self.capacity = capacity
        self.reports = deque()
        self.error: str | None = None
        self.stopping = False
        self.connected = set()
        self._fences: dict[str, threading.Event] = {}
        self._fence_counter = 0
        self.add_listener(self._receive)

    def _fail(self, reason):
        """Latch the first bounded diagnostic and wake startup/drain waiters."""
        with self._condition:
            if self.error is None:
                self.error = str(reason)[:1024]
            for event in self._fences.values():
                event.set()
            self._condition.notify_all()

    def _protocol_reject(self, message, message_type):
        def field(tag):
            return message.getField(tag) if message.isSetField(tag) else "?"

        self._fail(
            f"FIX protocol rejection 35={message_type}, RefSeqNum={field(45)}, "
            f"RefMsgType={field(372)}: {field(58)}"
        )

    def _receive(self, report):
        with self._condition:
            if len(self.reports) >= self.capacity:
                self._fail("FIX report callback queue overflow")
            else:
                self.reports.append(report)

    def fromApp(self, message, sessionID):
        # Never let a Python exception escape a native QuickFIX callback.
        try:
            if message.getHeader().getField(35) == "j":
                self._protocol_reject(message, "j")
                return
            super().fromApp(message, sessionID)
        except Exception as exc:
            self._fail(f"Invalid FIX report: {exc}")

    def onLogon(self, sessionID):
        super().onLogon(sessionID)
        with self._condition:
            self.connected.add(sessionID.getSenderCompID().getValue())

    def onLogout(self, sessionID):
        role = sessionID.getSenderCompID().getValue()
        with self._condition:
            if role in self.connected and not self.stopping:
                self._fail(f"FIX session disconnected: {role}")
        super().onLogout(sessionID)

    def fromAdmin(self, message, sessionID):
        try:
            message_type = message.getHeader().getField(35)
            if message_type == "3":
                self._protocol_reject(message, message_type)
            elif message_type == "5" and not self.stopping:
                reason = (
                    message.getField(58)
                    if message.isSetField(58)
                    else "No reason supplied"
                )
                self._fail(f"FIX peer Logout: {reason}")
            elif message_type == "0" and message.isSetField(112):
                with self._condition:
                    event = self._fences.get(message.getField(112))
                    if event is not None:
                        event.set()
        except Exception as exc:
            self._fail(f"Invalid FIX administrative message: {exc}")

    def wait_for_sessions(self, roles, timeout):
        expected = frozenset(roles)
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self.error:
                    raise RuntimeError(self.error)
                if expected <= self._sessions.keys():
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    def take_reports(self):
        with self._condition:
            if self.error:
                raise RuntimeError(self.error)
            reports = tuple(self.reports)
            self.reports.clear()
            return reports

    def fence(self, roles, timeout):
        deadline = time.monotonic() + timeout
        pending = []
        try:
            for role in roles:
                with self._condition:
                    if self.error:
                        raise RuntimeError(self.error)
                    self._fence_counter += 1
                    key = f"REPLAY-END-{self._fence_counter}"
                    event = threading.Event()
                    self._fences[key] = event
                    pending.append((key, event))
                message = fix.Message()
                message.getHeader().setField(fix.MsgType("1"))
                message.setField(fix.TestReqID(key))
                if not fix.Session.sendToTarget(message, self.session(role)):
                    raise RuntimeError(f"FIX fence send failed: {role}")
            for _, event in pending:
                if not event.wait(max(0, deadline - time.monotonic())):
                    raise TimeoutError(
                        "Timed out waiting for FIX final heartbeat fence"
                    )
                with self._condition:
                    if self.error:
                        raise RuntimeError(self.error)
        finally:
            with self._condition:
                for key, _ in pending:
                    self._fences.pop(key, None)


class FixTransport:
    def __init__(self, config: str | Path, instrument, roles, *, capacity=8192):
        require_quickfix()
        self.instrument = instrument
        self.roles = tuple(roles)
        self.app = ReplayFIXApplication(capacity=capacity)
        # Retain every SWIG dependency for the full native initiator lifetime.
        self.settings = fix.SessionSettings(str(config))
        self.store = fix.FileStoreFactory(self.settings)
        self.log = fix.FileLogFactory(self.settings)
        self.initiator = fix.SocketInitiator(
            self.app,
            self.store,
            self.settings,
            self.log,
        )
        self.started = False

    async def start(self, timeout=15):
        self.initiator.start()
        self.started = True
        if not await asyncio.to_thread(self.app.wait_for_sessions, self.roles, timeout):
            raise TimeoutError("Timed out waiting for replay FIX logons")

    def send(self, intent):
        session = self.app.session(intent.role)
        kwargs = dict(
            cl_ord_id=intent.cl_ord_id,
            symbol=intent.symbol,
            side="1" if intent.side == "BUY" else "2",
            quantity=self.instrument.format_quantity(intent.quantity_lots),
        )
        if intent.kind == "CANCEL":
            send_cancel(session, orig_cl_ord_id=intent.orig_cl_ord_id, **kwargs)
        elif intent.kind == "REPLACE":
            send_replace(
                session,
                orig_cl_ord_id=intent.orig_cl_ord_id,
                price=self.instrument.format_price(intent.price_ticks),
                **kwargs,
            )
        else:
            send_limit_order(
                session,
                price=self.instrument.format_price(intent.price_ticks),
                tif="3" if intent.tif == "IOC" else "1",
                **kwargs,
            )

    def take_reports(self):
        return self.app.take_reports()

    async def fence(self, timeout=15):
        await asyncio.to_thread(self.app.fence, self.roles, timeout)

    async def stop(self):
        self.app.stopping = True
        if self.started:
            await asyncio.to_thread(self.initiator.stop)
            self.started = False

"""Real QuickFIX transport shared by historical flow and midpoint control."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable
import uuid

from examples.fix_support import (
    FixReport, MultiSessionFIXApplication, fix, require_quickfix,
    send_cancel, send_limit_order, send_market_order, send_replace, sender_comp_id,
)
from src.mockx.instruments import Instrument

from .tracker import FixIntent


class _ReplayFIXApplication(MultiSessionFIXApplication):
    def __init__(self, heartbeat: Callable[[str, str], None]):
        super().__init__()
        self._heartbeat = heartbeat

    def fromAdmin(self, message, sessionID):
        super().fromAdmin(message, sessionID)
        message_type = fix.MsgType()
        message.getHeader().getField(message_type)
        if message_type.getValue() == "0" and message.isSetField(112):
            request_id = fix.TestReqID()
            message.getField(request_id)
            self._heartbeat(sender_comp_id(sessionID), request_id.getValue())


class NativeFixTransport:
    """Keep native factories alive and marshal callbacks onto the runner loop."""

    def __init__(self, settings_path: str | Path, instrument: Instrument):
        require_quickfix()
        self.instrument = instrument
        self.application = _ReplayFIXApplication(self._on_heartbeat)
        self.settings = fix.SessionSettings(str(settings_path))
        self.store = fix.FileStoreFactory(self.settings)
        self.logs = fix.FileLogFactory(self.settings)
        self.initiator = fix.SocketInitiator(
            self.application, self.store, self.settings, self.logs,
        )
        self.started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._receive: Callable[[FixReport], None] | None = None
        self._roles: tuple[str, ...] = ()
        self._drain_waiters: dict[tuple[str, str], asyncio.Future] = {}
        self.application.add_listener(self._on_report)

    def _on_report(self, report: FixReport) -> None:
        if self._loop is not None and not self._loop.is_closed() and self._receive:
            self._loop.call_soon_threadsafe(self._receive, report)

    def _on_heartbeat(self, role: str, request_id: str) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._complete_drain, role, request_id)

    def _complete_drain(self, role: str, request_id: str) -> None:
        waiter = self._drain_waiters.get((role, request_id))
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def start(self, receive: Callable[[FixReport], None],
                    roles: list[str], timeout: float) -> dict[str, str]:
        self._loop = asyncio.get_running_loop()
        self._receive = receive
        self.initiator.start()
        self.started = True
        connected = await asyncio.to_thread(
            self.application.wait_for_sessions, roles, timeout,
        )
        if not connected:
            raise TimeoutError(f"FIX logon timed out for {roles}")
        sessions = {}
        for role in roles:
            session = self.application.session(role)
            # The administrative API uses the acceptor's SessionID orientation.
            if session.getSessionQualifier():
                raise ValueError("Replay examples require unqualified FIX sessions")
            sessions[role] = (
                f"{session.getBeginString().getValue()}:"
                f"{session.getTargetCompID().getValue()}->"
                f"{session.getSenderCompID().getValue()}"
            )
        self._roles = tuple(roles)
        return sessions

    async def drain(self, timeout: float) -> None:
        """Wait for earlier FIX reports after the server delivery queue is empty.

        Each heartbeat follows its session's preceding application messages.
        Both callbacks enter the same asyncio loop in native callback order, so
        completion also waits for the runner to apply those earlier reports.
        The caller must keep new order submission quiescent during this barrier.
        """
        if not self.started or self._loop is None or not self._roles:
            raise RuntimeError("FIX transport must be started before draining")
        if self._drain_waiters:
            raise RuntimeError("A FIX drain is already in progress")
        try:
            for role in self._roles:
                request_id = f"REPLAY-DRAIN-{uuid.uuid4().hex}"
                self._drain_waiters[role, request_id] = self._loop.create_future()
                message = fix.Message()
                message.getHeader().setField(fix.MsgType("1"))
                message.setField(fix.TestReqID(request_id))
                if not fix.Session.sendToTarget(message, self.application.session(role)):
                    raise RuntimeError(f"FIX TestRequest could not be sent for {role}")
            await asyncio.wait_for(
                asyncio.gather(*self._drain_waiters.values()), timeout,
            )
        finally:
            for waiter in self._drain_waiters.values():
                if not waiter.done():
                    waiter.cancel()
            self._drain_waiters.clear()

    async def send(self, intent: FixIntent) -> None:
        session = self.application.session(intent.role)
        common = dict(
            cl_ord_id=intent.cl_ord_id, symbol=intent.symbol,
            side="1" if intent.side == "BUY" else "2",
            quantity=self.instrument.format_quantity(intent.quantity_lots),
        )
        if intent.kind == "CANCEL":
            send_cancel(session, **common, orig_cl_ord_id=intent.orig_cl_ord_id)
        elif intent.kind == "REPLACE":
            send_replace(session, **common, orig_cl_ord_id=intent.orig_cl_ord_id,
                         price=self.instrument.format_price(intent.price_ticks))
        elif intent.kind == "NEW" and intent.price_ticks is None:
            send_market_order(session, **common)
        elif intent.kind == "NEW":
            send_limit_order(session, **common,
                             price=self.instrument.format_price(intent.price_ticks),
                             tif="3" if intent.tif == "IOC" else "1")
        else:
            raise ValueError(f"Unsupported FIX intent: {intent.kind}")

    async def close(self) -> None:
        try:
            if self.started:
                self.started = False
                await asyncio.wait_for(asyncio.to_thread(self.initiator.stop, True), 10)
        finally:
            # QuickFIX unregisters its process-wide SessionIDs when the native
            # initiator is destroyed, not just stopped. The application's bound
            # report listener retains this transport, so do not rely on GC to
            # free it before the next replay creates the same FIX sessions.
            self.initiator = None
            self._receive = None
            self._loop = None
            self._roles = ()
            for waiter in self._drain_waiters.values():
                waiter.cancel()
            self._drain_waiters.clear()

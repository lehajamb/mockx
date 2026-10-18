"""Native QuickFIX wire integration for cancel/replace with dictionary validation."""

import asyncio
from pathlib import Path
import socket

import pytest

from examples.fix_support import (
    MultiSessionFIXApplication,
    send_cancel,
    send_limit_order,
    send_replace,
)
from src.mockx.configuration import validate_config
from src.mockx.gateway import fix
from src.mockx.main import MockExchange
from .api_helpers import api_config


class _WireClient(MultiSessionFIXApplication):
    """Keep immutable wire fields while reusing the examples' session handling."""

    def __init__(self):
        super().__init__()
        self.reports = []

    def fromApp(self, message, sessionID):
        if message.getHeader().getField(35) == "8":
            fields = {
                tag: message.getField(tag)
                for tag in (11, 37, 41, 38, 150, 39, 151, 32, 1003)
                if message.isSetField(tag)
            }
            with self._condition:
                self.reports.append(fields)
                self._condition.notify_all()
        super().fromApp(message, sessionID)

    def wait_for_report(self, cl_ord_id, exec_type, timeout=10):
        def matching():
            return next((report for report in self.reports
                         if report[11] == cl_ord_id and report[150] == exec_type), None)

        with self._condition:
            if not self._condition.wait_for(lambda: matching() is not None, timeout):
                raise AssertionError(
                    f"No wire report for {cl_ord_id}/{exec_type}; received {self.reports}"
                )
            return matching()


def _settings(tmp_path, *, acceptor, port):
    role = "acceptor" if acceptor else "initiator"
    dictionary = (Path(__file__).resolve().parents[1] / "configs" / "FIX44.xml").as_posix()
    store = tmp_path / f"{role}-store"
    logs = tmp_path / f"{role}-logs"
    store.mkdir()
    logs.mkdir()
    lines = [
        "[DEFAULT]",
        f"ConnectionType={role}",
        f"SenderCompID={'MOCKX_REPLACE_WIRE' if acceptor else 'REPLACE_WIRE_CLIENT'}",
        f"TargetCompID={'REPLACE_WIRE_CLIENT' if acceptor else 'MOCKX_REPLACE_WIRE'}",
        "StartTime=00:00:00", "EndTime=00:00:00",
        f"FileStorePath={store.as_posix()}", f"FileLogPath={logs.as_posix()}",
        "UseDataDictionary=Y", f"DataDictionary={dictionary}",
        "ValidateFieldsOutOfOrder=Y", "ValidateFieldsHaveValues=Y",
        "ResetOnLogon=Y", "ResetOnLogout=Y", "ResetOnDisconnect=Y",
        "HeartBtInt=30", "LogonTimeout=5", "LogoutTimeout=1",
        "ReconnectInterval=1",
    ]
    if acceptor:
        lines.append(f"SocketAcceptPort={port}")
    else:
        lines.extend(["SocketConnectHost=127.0.0.1", f"SocketConnectPort={port}"])
    lines.extend(["[SESSION]", "BeginString=FIX.4.4", ""])
    lines.extend([
        "[SESSION]", "BeginString=FIX.4.4",
        ("TargetCompID=REPLACE_WIRE_PASSIVE" if acceptor
         else "SenderCompID=REPLACE_WIRE_PASSIVE"),
        "",
    ])
    path = tmp_path / f"{role}.cfg"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_replace_round_trip_through_native_quickfix_dictionary(tmp_path):
    """D -> G reduction -> F must survive real FIX44 dictionary and socket I/O."""
    if fix is None:
        pytest.skip("Native QuickFIX is required for the wire integration")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    acceptor_path = _settings(tmp_path, acceptor=True, port=port)
    initiator_path = _settings(tmp_path, acceptor=False, port=port)
    config = api_config(fix_enabled=True)
    config["fix"]["session_participants"] = {
        "FIX.4.4:MOCKX_REPLACE_WIRE->REPLACE_WIRE_CLIENT": "wire-participant",
        "FIX.4.4:MOCKX_REPLACE_WIRE->REPLACE_WIRE_PASSIVE": "wire-passive-participant",
    }
    config["scheduler"]["mode"] = "realtime"
    exchange = MockExchange(validate_config(config, environ={}), str(acceptor_path))
    client = _WireClient()
    settings = fix.SessionSettings(str(initiator_path))
    # Keep native factories alive until their initiator has stopped.
    store_factory = fix.FileStoreFactory(settings)
    log_factory = fix.FileLogFactory(settings)
    initiator = fix.SocketInitiator(client, store_factory, settings, log_factory)
    started = False
    try:
        await exchange.start_components()
        await exchange.lifecycle.start_session("native-fix-replace-wire")
        initiator.start()
        started = True
        assert await asyncio.wait_for(
            asyncio.to_thread(client.wait_for_sessions, ["REPLACE_WIRE_CLIENT", "REPLACE_WIRE_PASSIVE"], 10), 12
        ), "The real QuickFIX initiator did not log on"
        session = client.session("REPLACE_WIRE_CLIENT")
        send_limit_order(session, cl_ord_id="WIRE-N", symbol="TEST", side="1",
                         quantity="10", price="100")
        new = await asyncio.wait_for(asyncio.to_thread(client.wait_for_report, "WIRE-N", "0"), 12)
        send_replace(session, cl_ord_id="WIRE-R", orig_cl_ord_id="WIRE-N",
                     symbol="TEST", side="1", quantity="6", price="100")
        replaced = await asyncio.wait_for(asyncio.to_thread(client.wait_for_report, "WIRE-R", "5"), 12)
        assert replaced[37] == new[37]
        assert replaced[41] == "WIRE-N"
        assert replaced[39] == "0"
        assert replaced[38] == replaced[151] == "6.000"
        send_cancel(session, cl_ord_id="WIRE-C", orig_cl_ord_id="WIRE-R",
                    symbol="TEST", side="1", quantity="6")
        canceled = await asyncio.wait_for(asyncio.to_thread(client.wait_for_report, "WIRE-C", "4"), 12)
        assert canceled[37] == new[37]
        assert canceled[41] == "WIRE-R"
        assert canceled[39] == "4" and canceled[151] == "0.000"
        assert [report[150] for report in client.reports] == ["0", "5", "4"]

        # A repricing replacement crosses third-party displayed liquidity.
        passive_session = client.session("REPLACE_WIRE_PASSIVE")
        send_limit_order(passive_session, cl_ord_id="WIRE-PASSIVE", symbol="TEST",
                         side="2", quantity="4", price="101")
        await asyncio.wait_for(asyncio.to_thread(client.wait_for_report, "WIRE-PASSIVE", "0"), 12)
        send_limit_order(session, cl_ord_id="WIRE-CROSS-N", symbol="TEST", side="1",
                         quantity="6", price="99")
        cross_new = await asyncio.wait_for(asyncio.to_thread(client.wait_for_report, "WIRE-CROSS-N", "0"), 12)
        send_replace(session, cl_ord_id="WIRE-CROSS-R", orig_cl_ord_id="WIRE-CROSS-N",
                     symbol="TEST", side="1", quantity="6", price="102")
        cross_ack = await asyncio.wait_for(asyncio.to_thread(client.wait_for_report, "WIRE-CROSS-R", "5"), 12)
        cross_fill = await asyncio.wait_for(asyncio.to_thread(client.wait_for_report, "WIRE-CROSS-R", "F"), 12)
        passive_fill = await asyncio.wait_for(asyncio.to_thread(client.wait_for_report, "WIRE-PASSIVE", "F"), 12)
        assert cross_new[37] == cross_ack[37] == cross_fill[37]
        assert cross_ack[41] == "WIRE-CROSS-N"
        assert cross_fill[39] == "1" and cross_fill[151] == "2.000"
        assert cross_fill[32] == "4.000"
        assert cross_fill[1003] == passive_fill[1003]
        assert cross_fill[1003].startswith("G00000001-T")
        assert [report[150] for report in client.reports
                if report[11] == "WIRE-CROSS-R"] == ["5", "F"]
        exchange.engine.books["TEST"].assert_invariants()
    finally:
        if started:
            await asyncio.wait_for(asyncio.to_thread(initiator.stop, True), 10)
        await exchange.stop_components()

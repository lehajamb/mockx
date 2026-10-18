"""Opt-in two-venue end-to-end test for the C++ smart order router."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import socket
import sys
import tempfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from src.mockx.configuration import validate_config  # noqa: E402
from src.mockx.main import MockExchange  # noqa: E402
from src.mockx.models import OrderStatus, Side  # noqa: E402
from tests.test_itch_integration import itch_config, submit  # noqa: E402


def available_ports(sock_type: socket.SocketKind, count: int) -> list[int]:
    probes: list[socket.socket] = []
    try:
        for _ in range(count):
            probe = socket.socket(socket.AF_INET, sock_type)
            probe.bind(("127.0.0.1", 0))
            probes.append(probe)
        return [int(probe.getsockname()[1]) for probe in probes]
    finally:
        for probe in probes:
            probe.close()


def acceptor_config(
    root: Path,
    *,
    venue: str,
    client: str,
    port: int,
) -> Path:
    store = root / f"{venue}-server-store"
    log = root / f"{venue}-server-log"
    store.mkdir()
    log.mkdir()
    path = root / f"{venue}-acceptor.cfg"
    path.write_text(
        f"""[DEFAULT]
ConnectionType=acceptor
ReconnectInterval=1
SenderCompID={venue}
SocketAcceptPort={port}
StartTime=00:00:00
EndTime=00:00:00
UseDataDictionary=Y
DataDictionary={REPOSITORY_ROOT.joinpath('configs/FIX44.xml').as_posix()}
FileStorePath={store.as_posix()}
FileLogPath={log.as_posix()}
HeartBtInt=2
ResetOnLogon=Y
ResetOnLogout=Y
ResetOnDisconnect=Y
RefreshOnLogon=Y
ValidateFieldsOutOfOrder=N
ValidateFieldsHaveValues=N
ValidateUserDefinedFields=N

[SESSION]
BeginString=FIX.4.4
TargetCompID={client}
""",
        encoding="utf-8",
    )
    return path


def exchange_config(
    *,
    name: str,
    client: str,
    multicast_group: str,
    multicast_port: int,
    glimpse_port: int,
    mold_prefix: str,
    password: str,
) -> dict:
    raw = itch_config()
    raw["exchange_name"] = name
    raw["latency"] = {"network_ms": 0, "engine_ms": 1}
    raw["scheduler"] = {
        "mode": "realtime",
        "interval_ms": 1,
        "max_advance_ms": 86_400_000,
        "max_events_per_advance": 100_000,
    }
    raw["fix"] = {
        "enabled": True,
        "session_participants": {f"FIX.4.4:{name}->{client}": "sor-demo"},
    }
    raw["moldudp64"].update(
        {
            "multicast_group": multicast_group,
            "multicast_port": multicast_port,
            "session_prefix": mold_prefix,
        }
    )
    raw["glimpse"] = {"enabled": True}
    raw["soupbintcp"] = {
        "host": "127.0.0.1",
        "port": glimpse_port,
        "session_prefix": f"G{mold_prefix[-1]}",
        "credentials": {"GLIMP": password},
    }
    return raw


def initiator_config(
    root: Path,
    *,
    a_fix: int,
    b_fix: int,
    a_mold: int,
    b_mold: int,
    a_glimpse: int,
    b_glimpse: int,
) -> Path:
    store = root / "client-store"
    log = root / "client-log"
    store.mkdir()
    log.mkdir()
    path = root / "sor.cfg"
    path.write_text(
        f"""[DEFAULT]
ConnectionType=initiator
ReconnectInterval=1
StartTime=00:00:00
EndTime=00:00:00
UseDataDictionary=Y
DataDictionary={REPOSITORY_ROOT.joinpath('configs/FIX44.xml').as_posix()}
FileStorePath={store.as_posix()}
FileLogPath={log.as_posix()}
HeartBtInt=2
ResetOnLogon=Y
ResetOnLogout=Y
ResetOnDisconnect=Y
RefreshOnLogon=Y
ValidateFieldsOutOfOrder=N
ValidateFieldsHaveValues=N
ValidateUserDefinedFields=N

[SESSION]
BeginString=FIX.4.4
SenderCompID=SOR_A
TargetCompID=MOCKX_A
SocketConnectHost=127.0.0.1
SocketConnectPort={a_fix}
VenueID=A
VenuePriority=0
ItchGroup=239.192.20.1
ItchPort={a_mold}
ItchInterface=0.0.0.0
MoldSessionPrefix=MA
GlimpseHost=127.0.0.1
GlimpsePort={a_glimpse}
GlimpseUsername=GLIMP
GlimpsePasswordEnv=MOCKX_SOR_A_GLIMPSE_PASSWORD

[SESSION]
BeginString=FIX.4.4
SenderCompID=SOR_B
TargetCompID=MOCKX_B
SocketConnectHost=127.0.0.1
SocketConnectPort={b_fix}
VenueID=B
VenuePriority=1
ItchGroup=239.192.20.2
ItchPort={b_mold}
ItchInterface=0.0.0.0
MoldSessionPrefix=MB
GlimpseHost=127.0.0.1
GlimpsePort={b_glimpse}
GlimpseUsername=GLIMP
GlimpsePasswordEnv=MOCKX_SOR_B_GLIMPSE_PASSWORD
""",
        encoding="utf-8",
    )
    return path


async def run(client: Path) -> None:
    if not client.is_file():
        raise AssertionError(f"C++ SOR does not exist: {client}")

    with tempfile.TemporaryDirectory(prefix="mockx-sor-") as directory:
        root = Path(directory)
        a_fix, b_fix, a_glimpse, b_glimpse = available_ports(
            socket.SOCK_STREAM, 4
        )
        a_mold, b_mold = available_ports(socket.SOCK_DGRAM, 2)

        a_acceptor = acceptor_config(
            root, venue="MOCKX_A", client="SOR_A", port=a_fix
        )
        b_acceptor = acceptor_config(
            root, venue="MOCKX_B", client="SOR_B", port=b_fix
        )
        venue_a = MockExchange(
            validate_config(
                exchange_config(
                    name="MOCKX_A",
                    client="SOR_A",
                    multicast_group="239.192.20.1",
                    multicast_port=a_mold,
                    glimpse_port=a_glimpse,
                    mold_prefix="MA",
                    password="SECRET_A",
                ),
                environ={},
            ),
            str(a_acceptor),
        )
        venue_b = MockExchange(
            validate_config(
                exchange_config(
                    name="MOCKX_B",
                    client="SOR_B",
                    multicast_group="239.192.20.2",
                    multicast_port=b_mold,
                    glimpse_port=b_glimpse,
                    mold_prefix="MB",
                    password="SECRET_B",
                ),
                environ={},
            ),
            str(b_acceptor),
        )

        await venue_a.start_components()
        await venue_b.start_components()
        process: asyncio.subprocess.Process | None = None
        try:
            await venue_a.lifecycle.start_session("sor-a")
            await venue_b.lifecycle.start_session("sor-b")
            submit(venue_a, "A-100", Side.SELL, 2, 100, "passive-a")
            submit(venue_a, "A-102", Side.SELL, 9, 102, "passive-a")
            submit(venue_b, "B-99", Side.SELL, 3, 99, "passive-b")
            submit(venue_b, "B-100", Side.SELL, 1, 100, "passive-b")

            config = initiator_config(
                root,
                a_fix=a_fix,
                b_fix=b_fix,
                a_mold=a_mold,
                b_mold=b_mold,
                a_glimpse=a_glimpse,
                b_glimpse=b_glimpse,
            )
            environment = os.environ.copy()
            environment["MOCKX_SOR_A_GLIMPSE_PASSWORD"] = "SECRET_A"
            environment["MOCKX_SOR_B_GLIMPSE_PASSWORD"] = "SECRET_B"
            process = await asyncio.create_subprocess_exec(
                str(client),
                "--config",
                str(config),
                "--parent-id",
                "LIVE",
                "--symbol",
                "TEST",
                "--side",
                "BUY",
                "--quantity-lots",
                "7",
                "--lot-size",
                "1",
                "--limit-price",
                "100",
                "--sync-timeout-ms",
                "8000",
                "--report-timeout-ms",
                "8000",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                cwd=REPOSITORY_ROOT,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
            rendered = stdout.decode(errors="replace")
            errors = stderr.decode(errors="replace")
            if process.returncode != 0:
                raise AssertionError(
                    f"SOR exited {process.returncode}\nstdout:\n{rendered}\nstderr:\n{errors}"
                )
            assert "venue=B quantity_lots=4" in rendered
            assert "venue=A quantity_lots=2" in rendered
            assert "planned_lots=6" in rendered
            assert "initial_unrouted_lots=1" in rendered
            assert "filled_lots=6" in rendered
            assert "total_remainder_lots=1" in rendered
            assert "notional=597.0000" in rendered
            assert "vwap=99.5000" in rendered

            a_children = [
                order
                for order in venue_a.engine.orders.values()
                if order.participant_id == "sor-demo"
            ]
            b_children = [
                order
                for order in venue_b.engine.orders.values()
                if order.participant_id == "sor-demo"
            ]
            assert len(a_children) == len(b_children) == 1
            assert a_children[0].filled_qty_lots == 2
            assert b_children[0].filled_qty_lots == 4
            assert a_children[0].status == OrderStatus.FILLED
            assert b_children[0].status == OrderStatus.FILLED
            assert not venue_a.engine.books["TEST"].is_resting(a_children[0].id)
            assert not venue_b.engine.books["TEST"].is_resting(b_children[0].id)
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
            await venue_a.stop_components()
            await venue_b.stop_components()
            # Native factories hold their log files until the acceptors are
            # destroyed. Release them before TemporaryDirectory cleans up on
            # Windows; stopping transport threads alone leaves these handles.
            for venue in (venue_a, venue_b):
                venue.fix_gateway.fix_acceptor = None
                venue.fix_gateway.fix_store_factory = None
                venue.fix_gateway.fix_log_factory = None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: live_smoke.py PATH_TO_MOCKX_SOR", file=sys.stderr)
        return 2
    asyncio.run(run(Path(sys.argv[1]).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

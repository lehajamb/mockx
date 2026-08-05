"""Opt-in end-to-end smoke test for the C++ GLIMPSE/ITCH client."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import socket
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from src.mockx.configuration import validate_config  # noqa: E402
from src.mockx.main import MockExchange  # noqa: E402
from src.mockx.models import Side  # noqa: E402
from tests.test_itch_integration import itch_config, submit  # noqa: E402


def available_port(sock_type: socket.SocketKind) -> int:
    with socket.socket(socket.AF_INET, sock_type) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def wait_for_glimpse(exchange: MockExchange) -> None:
    for _ in range(100):
        if exchange.glimpse_service.diagnostics()["completed_sessions"] >= 1:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("C++ client did not complete its GLIMPSE snapshot")


async def run(client: Path) -> None:
    if not client.is_file():
        raise AssertionError(f"C++ client does not exist: {client}")

    raw = itch_config()
    raw["moldudp64"]["multicast_port"] = available_port(socket.SOCK_DGRAM)
    raw["glimpse"] = {"enabled": True}
    raw["soupbintcp"] = {
        "host": "127.0.0.1",
        "port": available_port(socket.SOCK_STREAM),
        "session_prefix": "GS",
        "credentials": {"GLIMP": "SECRET"},
    }
    exchange = MockExchange(validate_config(raw, environ={}))
    process: asyncio.subprocess.Process | None = None
    await exchange.start_components()
    try:
        await exchange.lifecycle.start_session("cpp-live-smoke")
        submit(exchange, "B1", Side.BUY, 3, 99, "BIDDER")

        environment = os.environ.copy()
        environment["MOCKX_GLIMPSE_PASSWORD"] = "SECRET"
        process = await asyncio.create_subprocess_exec(
            str(client),
            "--itch-group",
            raw["moldudp64"]["multicast_group"],
            "--itch-port",
            str(raw["moldudp64"]["multicast_port"]),
            "--glimpse-host",
            raw["soupbintcp"]["host"],
            "--glimpse-port",
            str(raw["soupbintcp"]["port"]),
            "--symbol",
            "TEST",
            "--refresh-ms",
            "250",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        await wait_for_glimpse(exchange)
        submit(exchange, "S1", Side.SELL, 2, 101, "ASKER")
        await asyncio.sleep(0.1)
        await exchange.lifecycle.end_session()

        communicate = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate), timeout=8)
        except TimeoutError as error:
            process.terminate()
            stdout, stderr = await communicate
            raise AssertionError(
                "client did not stop after Mold end-of-session\n"
                f"stdout:\n{stdout.decode(errors='replace')}\n"
                f"stderr:\n{stderr.decode(errors='replace')}"
            ) from error
        if process.returncode != 0:
            raise AssertionError(
                f"client exited {process.returncode}: {stderr.decode(errors='replace')}"
            )
        rendered = stdout.decode(errors="replace")
        assert "TEST" in rendered
        assert "END_OF_SESSION" in rendered
        assert "next_seq=" in rendered
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            await process.wait()
        await exchange.stop_components()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: live_smoke.py PATH_TO_MOCKX_MARKET_FEED", file=sys.stderr)
        return 2
    asyncio.run(run(Path(sys.argv[1]).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

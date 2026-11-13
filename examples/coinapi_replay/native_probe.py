"""Qualify real replay feeds with the existing C++ GLIMPSE/ITCH client.

This wrapper relays bytes and reads the client's display. The C++ client remains
the sole decoder, sequence recovery implementation, and reconstructed order book.
One deliberately dropped UDP data packet exercises fresh-GLIMPSE recovery.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import time

from src.mockx.configuration import load_config
from src.mockx.instruments import Instrument
from src.mockx.soupbintcp_transport import encode_packet, read_packet


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def parse_frame(text: str, symbol: str) -> dict:
    """Read one bounded console snapshot; wire prices remain integer Price(4)."""
    lines = _ANSI.sub("", text).splitlines()
    header = re.search(r"state=(\w+)\s+session=(\S+)\s+next_seq=(\d+)", lines[0])
    if header is None:
        raise ValueError("Malformed C++ feed display header")
    result = dict(state=header[1], session=header[2], next_sequence=int(header[3]))
    result.update(
        {key: int(value) for key, value in re.findall(r"(\w+)=(\d+)", lines[1])}
    )
    result.update(bids=[], asks=[])
    for index, line in enumerate(lines):
        if not line.startswith(symbol + "  trading_state="):
            continue
        for row in lines[index + 2 :]:
            if not row.strip():
                break
            for field, side in ((row[:37], "bids"), (row[41:], "asks")):
                values = field.split()
                if not values:
                    continue
                if len(values) != 3:
                    raise ValueError("Malformed C++ feed depth row")
                whole, fraction = values[0].split(".")
                if len(fraction) != 4:
                    raise ValueError("Expected exact ITCH Price(4) display")
                result[side].append(
                    (int(whole) * 10_000 + int(fraction), int(values[1]))
                )
        break
    return result


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _Relay(asyncio.DatagramProtocol):
    def __init__(self, owner):
        self.owner = owner

    def datagram_received(self, data, _address):
        self.owner._relay_packet(data)

    def error_received(self, error):
        self.owner.error = error


class NativeProbe:
    def __init__(self, config, instrument, *, executable, timeout=15.0, levels=10):
        if not config.itch.enabled or not config.glimpse.enabled:
            raise ValueError("Native qualification requires ITCH and GLIMPSE")
        self.config, self.instrument = config, instrument
        self.executable = Path(executable).resolve()
        self.timeout, self.levels = timeout, levels
        self.process = self.udp = self.server = self.sender = None
        self.tasks = set()
        self.stderr = deque(maxlen=32)
        self.latest = None
        self.snapshot_cuts = []
        self.error = None
        self.source_gaps = self.forwarded = self.planned_drops = 0
        self._upstream_first = None
        self._upstream_next = None
        self._drop_next = False
        self._drop_from_sequence = 0
        self.dropped_sequence = None
        self._closing = False
        self._close_task = None
        self._relay_port = _free_udp_port()

    def _task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _relay_packet(self, data):
        if self._closing:
            return
        try:
            if len(data) < 20:
                raise ValueError("Truncated upstream MoldUDP64 header")
            sequence, count = struct.unpack(">QH", data[10:20])
            if self._upstream_first is None:
                self._upstream_first = sequence
            if self._upstream_next is not None and sequence > self._upstream_next:
                self.source_gaps += 1
            end = sequence + (count if count != 0xFFFF else 0)
            self._upstream_next = max(self._upstream_next or end, end)
            if (
                self._drop_next
                and count not in (0, 0xFFFF)
                and end > self._drop_from_sequence
            ):
                self._drop_next = False
                self.planned_drops += 1
                self.dropped_sequence = sequence
                return
            self.sender.sendto(data, ("127.0.0.1", self._relay_port))
            self.forwarded += 1
        except Exception as exc:
            self.error = exc

    async def _soup_connection(self, reader, writer):
        """Pass Soup packets unchanged, recording only the native snapshot cut."""
        remote_writer = None
        upstream = None
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            if self._closing:
                return
            remote_reader, remote_writer = await asyncio.open_connection(
                self.config.soupbintcp.host,
                self.config.soupbintcp.port,
            )

            async def copy_login():
                while data := await reader.read(65536):
                    remote_writer.write(data)
                    await remote_writer.drain()

            upstream = asyncio.create_task(copy_login())
            while True:
                packet = await read_packet(remote_reader)
                if packet.packet_type == b"S" and packet.payload[:1] == b"G":
                    if len(packet.payload) != 21:
                        raise ValueError("Malformed GLIMPSE snapshot cut")
                    self.snapshot_cuts.append(int(packet.payload[1:]))
                    if len(self.snapshot_cuts) > 16:
                        raise RuntimeError(
                            "Native qualification exceeded its recovery limit"
                        )
                writer.write(encode_packet(packet.packet_type, packet.payload))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
        finally:
            if upstream is not None:
                upstream.cancel()
                await asyncio.gather(upstream, return_exceptions=True)
            for stream in (writer, remote_writer):
                if stream is not None:
                    stream.close()
                    try:
                        await stream.wait_closed()
                    except ConnectionError:
                        pass
            self.tasks.discard(task)

    async def _read_stdout(self):
        block = []
        try:
            while line := await self.process.stdout.readline():
                text = line.decode("utf-8", errors="strict")
                if "MockX GLIMPSE/ITCH market feed" in text:
                    block = []
                block.append(text)
                if len(block) > 256:
                    raise RuntimeError("Native display exceeded its bounded frame size")
                if text.startswith("Ctrl+C to stop"):
                    self.latest = parse_frame("".join(block), self.instrument.symbol)
                    block = []
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc

    async def _read_stderr(self):
        while line := await self.process.stderr.readline():
            self.stderr.append(line.decode("utf-8", errors="replace").strip())

    def check(self):
        if self.error is not None:
            raise RuntimeError(f"Native probe failed: {self.error}") from self.error
        if self.source_gaps:
            raise RuntimeError("Unexpected packet loss before the qualification relay")
        if self.latest:
            if self.latest["ring_drops"] or self.latest["gaps"] > self.planned_drops:
                raise RuntimeError(f"Unexpected native continuity loss: {self.latest}")
        if self.process is not None and self.process.returncode not in (None, 0):
            raise RuntimeError(
                f"C++ native client exited {self.process.returncode}: {'; '.join(self.stderr)}"
            )

    async def _wait(self, predicate):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.check()
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise TimeoutError(f"Native qualification timed out: {self.result()}")

    async def start(self):
        if not self.executable.is_file():
            raise FileNotFoundError(
                f"Build examples/cpp_market_feed first: {self.executable}"
            )
        if self.process is not None:
            raise RuntimeError("NativeProbe instances may be started only once")
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server = await asyncio.start_server(self._soup_connection, "127.0.0.1", 0)
        soup_port = self.server.sockets[0].getsockname()[1]
        mold = self.config.moldudp64
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            sock.bind(("0.0.0.0", mold.multicast_port))
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(mold.multicast_group)
                + socket.inet_aton(mold.interface_address),
            )
            sock.setblocking(False)
            self.udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _Relay(self), sock=sock
            )
        except BaseException:
            sock.close()
            await self.close()
            raise
        username, password = next(iter(self.config.soupbintcp.credentials.items()))
        environment = dict(os.environ, MOCKX_GLIMPSE_PASSWORD=password)
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(self.executable),
                "--itch-group",
                "239.192.255.254",
                "--itch-port",
                str(self._relay_port),
                "--session-prefix",
                mold.session_prefix,
                "--glimpse-host",
                "127.0.0.1",
                "--glimpse-port",
                str(soup_port),
                "--username",
                username,
                "--symbol",
                self.instrument.symbol,
                "--levels",
                str(self.levels),
                "--refresh-ms",
                "10",
                "--datagram-bytes",
                str(max(2048, mold.max_payload_bytes)),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                **(
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if os.name == "nt"
                    else {}
                ),
            )
            self._task(self._read_stdout())
            self._task(self._read_stderr())
            await self._wait(
                lambda: self.latest
                and self.latest["state"] == "LIVE"
                and self.snapshot_cuts
                and self.latest["next_sequence"] >= self.snapshot_cuts[-1]
            )
        except BaseException:
            await self.close()
            raise

    async def request_recovery(self):
        """Drop exactly one future data packet and require a fresh coherent cut."""
        if not self.latest or self.latest["state"] != "LIVE" or self._drop_next:
            raise RuntimeError(
                "A recovery probe requires one live, idle qualification client"
            )
        previous = len(self.snapshot_cuts)
        expected = self.latest["resyncs"] + 1
        # UDP can still contain bootstrap packets queued before the GLIMPSE
        # cut. Dropping those is harmless and would never exercise recovery.
        self._drop_from_sequence = max(
            self.latest["next_sequence"], self.snapshot_cuts[-1]
        )
        self._drop_next = True
        await self._wait(
            lambda: self.latest
            and self.latest["state"] == "LIVE"
            and self.latest["resyncs"] >= expected
            and len(self.snapshot_cuts) > previous
            and self.latest["next_sequence"] >= self.snapshot_cuts[-1]
        )

    async def wait_for_sequence(self, next_sequence):
        await self._wait(
            lambda: self.latest
            and self.latest["state"] in {"LIVE", "END_OF_SESSION"}
            and self.latest["next_sequence"] >= next_sequence
        )

    async def wait_for_end(self):
        await self._wait(
            lambda: self.latest and self.latest["state"] == "END_OF_SESSION"
        )
        await asyncio.wait_for(self.process.wait(), self.timeout)
        self.check()

    def assert_depth(self, bids, asks):
        """Compare a quiescent engine/REST cut in native instrument ticks/lots."""
        self.check()
        if self.latest is None:
            raise AssertionError("Native feed has not published a book")
        for name, expected in (("bids", bids), ("asks", asks)):
            actual = [
                (
                    self.instrument.price_to_ticks(
                        f"{price // 10000}.{price % 10000:04d}"
                    ),
                    quantity,
                )
                for price, quantity in self.latest[name]
            ]
            if actual != list(expected)[: self.levels]:
                raise AssertionError(
                    f"Native {name} differ: {actual} != {list(expected)[:self.levels]}"
                )

    def result(self):
        return {
            **(self.latest or {}),
            "snapshot_cuts": list(self.snapshot_cuts),
            "planned_drops": self.planned_drops,
            "source_gaps": self.source_gaps,
            "forwarded_packets": self.forwarded,
            "upstream_first_sequence": self._upstream_first,
            "upstream_next_sequence": self._upstream_next,
            "dropped_sequence": self.dropped_sequence,
        }

    async def close(self):
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close_resources())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            # Finish the owned cleanup before propagating caller interruption.
            await asyncio.shield(self._close_task)
            raise

    async def _close_resources(self):
        self._closing = True
        if self.udp is not None:
            self.udp.close()
        if self.server is not None:
            self.server.close()
        try:
            if self.process is not None and self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
            tasks = tuple(self.tasks)
            for task in tasks:
                task.cancel()
            # Relay task finalizers close both upstream and downstream writers.
            # Server.wait_closed waits for those connections on current Python.
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.process is not None and self.process.returncode is None:
                try:
                    await asyncio.wait_for(self.process.wait(), self.timeout)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await asyncio.wait_for(self.process.wait(), self.timeout)
            if self.server is not None:
                await asyncio.wait_for(self.server.wait_closed(), self.timeout)
        finally:
            if self.sender is not None:
                self.sender.close()


async def _main(args):
    config = load_config(args.config, environ={})
    instrument = Instrument.from_config(args.symbol, config.instruments[args.symbol])
    probe = NativeProbe(
        config, instrument, executable=args.client, timeout=args.timeout
    )
    try:
        await probe.start()
        print("Native GLIMPSE/ITCH observer LIVE", flush=True)
        if args.recover_after is not None:
            await asyncio.sleep(args.recover_after)
            await probe.request_recovery()
            print("Native gap recovered through fresh GLIMPSE", flush=True)
        await probe.wait_for_end()
        print(json.dumps(probe.result(), indent=2), flush=True)
    finally:
        await probe.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--recover-after", type=float)
    parser.add_argument("--timeout", type=float, default=30.0)
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()

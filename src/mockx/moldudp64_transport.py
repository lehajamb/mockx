"""Downstream-only MoldUDP64 packetization and IPv4 multicast transport."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import logging
import socket
import struct
from typing import Callable, Optional


logger = logging.getLogger(__name__)

MOLD_HEADER_SIZE = 20
MESSAGE_LENGTH_SIZE = 2
END_OF_SESSION_COUNT = 0xFFFF
MAX_DATA_MESSAGE_COUNT = 0xFFFE
MAX_MESSAGE_SIZE = 0xFFFF


@dataclass(frozen=True, slots=True)
class MoldUDP64Config:
    """Immutable configuration for exactly one downstream session."""

    multicast_group: str
    multicast_port: int
    session_name: str
    heartbeat_interval_ms: int = 1_000
    max_payload_bytes: int = 1_400
    interface_address: str = "0.0.0.0"
    ttl: int = 1

    def __post_init__(self) -> None:
        try:
            session = self.session_name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Session name must contain only ASCII") from exc
        if not session or len(session) > 10:
            raise ValueError("Session name must contain 1-10 ASCII bytes")
        try:
            group = ipaddress.ip_address(self.multicast_group)
        except ValueError as exc:
            raise ValueError(
                "multicast_group must be an IPv4 multicast address"
            ) from exc
        if group.version != 4 or not group.is_multicast:
            raise ValueError("multicast_group must be an IPv4 multicast address")
        try:
            interface = ipaddress.ip_address(self.interface_address)
        except ValueError as exc:
            raise ValueError("interface_address must be an IPv4 address") from exc
        if interface.version != 4 or interface.is_multicast:
            raise ValueError("interface_address must be a non-multicast IPv4 address")
        if not 1 <= self.multicast_port <= 65535:
            raise ValueError("multicast_port must be 1-65535")
        if not 100 <= self.heartbeat_interval_ms <= 60_000:
            raise ValueError("heartbeat_interval_ms must be 100-60000")
        if not 512 <= self.max_payload_bytes <= 65_507:
            raise ValueError("max_payload_bytes must be 512-65507")
        if not 0 <= self.ttl <= 255:
            raise ValueError("ttl must be 0-255")

    @property
    def session_bytes(self) -> bytes:
        return self.session_name.encode("ascii").ljust(10, b" ")


@dataclass(frozen=True, slots=True)
class MoldSendResult:
    messages_sent: int
    packets_sent: int
    next_sequence_number: int


class _MoldUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "MoldUDP64Transport") -> None:
        self.owner = owner

    def connection_made(self, transport) -> None:
        self.owner._transport = transport

    def error_received(self, exc: Exception) -> None:
        self.owner._report_error(exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self.owner._transport = None
        if exc is not None and self.owner.is_active:
            self.owner._report_error(exc)


class MoldUDP64Transport:
    """Own one downstream multicast socket and its message sequence."""

    def __init__(
        self,
        config: MoldUDP64Config,
        *,
        on_error: Optional[Callable[[BaseException], None]] = None,
        socket_factory: Callable[..., socket.socket] = socket.socket,
    ) -> None:
        self._config = config
        self._on_error = on_error
        self._socket_factory = socket_factory
        self._next_sequence = 1
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._session_active = False

    @property
    def sequence_number(self) -> int:
        """Return the next ITCH message sequence expected on the wire."""
        return self._next_sequence

    @property
    def is_active(self) -> bool:
        return self._session_active

    @property
    def config(self) -> MoldUDP64Config:
        return self._config

    @property
    def max_payload_bytes(self) -> int:
        return self._config.max_payload_bytes

    async def start(self) -> None:
        if self._session_active or self._transport is not None:
            raise RuntimeError("MoldUDP64 session is already active")
        self._next_sequence = 1
        udp_socket = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp_socket.setblocking(False)
            udp_socket.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_TTL,
                self._config.ttl,
            )
            udp_socket.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self._config.interface_address),
            )
            udp_socket.bind((self._config.interface_address, 0))
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _MoldUDPProtocol(self),
                sock=udp_socket,
            )
            self._transport = transport
            self._session_active = True
        except Exception:
            udp_socket.close()
            raise

    def stop(self) -> None:
        self._session_active = False
        transport = self._transport
        self._transport = None
        if transport is not None:
            transport.close()

    def _report_error(self, exc: BaseException) -> None:
        logger.error("MoldUDP64 socket failure: %s", exc)
        callback = self._on_error
        if callback is not None:
            try:
                callback(exc)
            except Exception:
                logger.exception("MoldUDP64 error callback failed")

    @staticmethod
    def _validate_sequence(sequence_number: int) -> int:
        if not isinstance(sequence_number, int) or isinstance(sequence_number, bool):
            raise ValueError("sequence_number must be an integer")
        if not 1 <= sequence_number <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError(
                "sequence_number must fit a positive unsigned 64-bit integer"
            )
        return sequence_number

    def pack_packet(
        self,
        messages: list[bytes],
        *,
        sequence_number: Optional[int] = None,
        end_of_session: bool = False,
    ) -> bytes:
        sequence = self._validate_sequence(
            self._next_sequence if sequence_number is None else sequence_number
        )
        if end_of_session:
            if messages:
                raise ValueError("End-of-session packet cannot contain messages")
            message_count = END_OF_SESSION_COUNT
            payload = b""
        elif not messages:
            message_count = 0
            payload = b""
        else:
            if len(messages) > MAX_DATA_MESSAGE_COUNT:
                raise ValueError("MoldUDP64 data message count exceeds 65534")
            blocks = []
            for message in messages:
                if not isinstance(message, bytes):
                    raise TypeError("MoldUDP64 messages must be bytes")
                if len(message) > MAX_MESSAGE_SIZE:
                    raise ValueError(
                        f"ITCH message too large: {len(message)} > {MAX_MESSAGE_SIZE}"
                    )
                blocks.append(struct.pack(">H", len(message)) + message)
            message_count = len(messages)
            payload = b"".join(blocks)
        packet = (
            self._config.session_bytes
            + struct.pack(">QH", sequence, message_count)
            + payload
        )
        if len(packet) > self._config.max_payload_bytes:
            raise ValueError(
                f"MoldUDP64 packet exceeds max payload: {len(packet)} > "
                f"{self._config.max_payload_bytes}"
            )
        return packet

    def heartbeat_packet(self) -> bytes:
        return self.pack_packet([])

    def end_of_session_packet(self) -> bytes:
        return self.pack_packet([], end_of_session=True)

    def _send_packet(self, packet: bytes, message_count: int) -> None:
        if not self._session_active or self._transport is None:
            raise RuntimeError("MoldUDP64 session is not active")
        next_sequence = self._next_sequence + message_count
        if next_sequence > 0xFFFFFFFFFFFFFFFF:
            raise OverflowError("MoldUDP64 sequence number exhausted")
        try:
            self._transport.sendto(
                packet,
                (self._config.multicast_group, self._config.multicast_port),
            )
        except Exception as exc:
            self._report_error(exc)
            raise
        if message_count:
            self._next_sequence = next_sequence

    def _packet_batches(self, messages: list[bytes]) -> list[list[bytes]]:
        batches: list[list[bytes]] = []
        current: list[bytes] = []
        current_size = MOLD_HEADER_SIZE
        for message in messages:
            if not isinstance(message, bytes):
                raise TypeError("MoldUDP64 messages must be bytes")
            block_size = MESSAGE_LENGTH_SIZE + len(message)
            if len(message) > MAX_MESSAGE_SIZE:
                raise ValueError(
                    f"ITCH message too large: {len(message)} > {MAX_MESSAGE_SIZE}"
                )
            if MOLD_HEADER_SIZE + block_size > self._config.max_payload_bytes:
                raise ValueError("Single ITCH message does not fit max_payload_bytes")
            if current and (
                current_size + block_size > self._config.max_payload_bytes
                or len(current) == MAX_DATA_MESSAGE_COUNT
            ):
                batches.append(current)
                current = []
                current_size = MOLD_HEADER_SIZE
            current.append(message)
            current_size += block_size
        if current:
            batches.append(current)
        return batches

    def send_messages(self, messages: list[bytes]) -> MoldSendResult:
        if not messages:
            return MoldSendResult(0, 0, self._next_sequence)
        batches = self._packet_batches(messages)
        sent = 0
        for batch in batches:
            packet = self.pack_packet(batch)
            self._send_packet(packet, len(batch))
            sent += len(batch)
        return MoldSendResult(sent, len(batches), self._next_sequence)

    def send_heartbeat(self) -> bool:
        if not self._session_active:
            return False
        self._send_packet(self.heartbeat_packet(), 0)
        return True

    def send_end_of_session(self) -> bool:
        if not self._session_active:
            return False
        self._send_packet(self.end_of_session_packet(), 0)
        return True

    def available_message_bytes(self) -> int:
        return self._config.max_payload_bytes - MOLD_HEADER_SIZE

    def max_message_blocks(self, message_sizes: list[int]) -> int:
        remaining = self.available_message_bytes()
        count = 0
        for size in message_sizes:
            block_size = size + MESSAGE_LENGTH_SIZE
            if block_size > remaining or count == MAX_DATA_MESSAGE_COUNT:
                break
            remaining -= block_size
            count += 1
        return count

    def diagnostics(self) -> dict[str, object]:
        return {
            "session_name": self._config.session_name,
            "multicast_group": self._config.multicast_group,
            "multicast_port": self._config.multicast_port,
            "next_sequence_number": self._next_sequence,
            "session_active": self._session_active,
        }

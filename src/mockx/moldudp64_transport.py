"""MoldUDP64 transport layer for outbound ITCH message delivery.

Implements the MoldUDP64 protocol specification (moldudp64.pdf):
- Downstream Packet Header: Session(10) + Sequence Number(8) + Message Count(2)
- Message Block: Message Length(2) + Message Data(variable)
- Heartbeat: Message Count = 0
- End of Session: Message Count = 0xFFFF

The transport is integrated with the deterministic scheduler — all timing
(heartbeat cadence, session boundaries) is driven by the LatencyScheduler,
not by wall-clock time.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MoldUDP64 constants
# ---------------------------------------------------------------------------
_MOLD_UDP_HEADER_SIZE = 20  # 10 (Session) + 8 (SeqNum) + 2 (MsgCount)
_MESSAGE_BLOCK_LENGTH_SIZE = 2
_END_OF_SESSION_COUNT = 0xFFFF
_HEARTBEAT_COUNT = 0
_MAX_MESSAGE_SIZE = 65535  # Per MoldUDP64 spec: messages up to 64KB

# Safeguard: maximum downstream packet size we will produce
_DEFAULT_MAX_PAYLOAD = 1400  # Conservative default for Ethernet


@dataclass(frozen=True, slots=True)
class MoldUDP64Config:
    """Immutable MoldUDP64 transport configuration."""

    multicast_group: str
    multicast_port: int
    session_name: str  # 10-byte ASCII session identifier
    heartbeat_interval_ms: int = 1_000
    max_payload_bytes: int = _DEFAULT_MAX_PAYLOAD
    interface_address: str = "0.0.0.0"
    ttl: int = 1

    def __post_init__(self) -> None:
        if len(self.session_name.encode("ascii")) > 10:
            raise ValueError("Session name must be at most 10 ASCII bytes")
        if not 1 <= self.multicast_port <= 65535:
            raise ValueError("multicast_port must be 1-65535")
        if not 100 <= self.heartbeat_interval_ms <= 60_000:
            raise ValueError(
                "heartbeat_interval_ms must be 100-60000"
            )
        if not 512 <= self.max_payload_bytes <= 65507:
            raise ValueError(
                "max_payload_bytes must be 512-65507"
            )
        if not 0 <= self.ttl <= 255:
            raise ValueError("ttl must be 0-255")

    @property
    def session_bytes(self) -> bytes:
        """Return the 10-byte session name padded with spaces."""
        return self.session_name.encode("ascii").ljust(10, b" ")


class MoldUDP64Transport:
    """MoldUDP64 outbound transport for one session.

    Sequence numbers are per-session, starting at 1.  The transport
    manages a UDP multicast socket and provides methods for:
      - packing ITCH message blocks into downstream packets
      - sending heartbeats (0 message count)
      - sending End of Session (0xFFFF message count)
    """

    def __init__(
        self,
        config: MoldUDP64Config,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._config = config
        self._loop = loop or asyncio.get_event_loop()
        self._seq_num: int = 0
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._session_active: bool = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def sequence_number(self) -> int:
        return self._seq_num

    @property
    def is_active(self) -> bool:
        return self._session_active

    @property
    def config(self) -> MoldUDP64Config:
        return self._config

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the UDP multicast socket and begin the session."""
        if self._session_active:
            raise RuntimeError("MoldUDP64 session is already active")
        if self._transport is not None:
            raise RuntimeError(
                "MoldUDP64 transport exists; stop() must be called first"
            )

        self._seq_num = 1

        class _MoldUDPProtocol(asyncio.DatagramProtocol):
            def __init__(self, owner: MoldUDP64Transport):
                self._owner = owner

            def connection_made(self, transport):
                self._owner._transport = transport

            def error_received(self, exc):
                logger.warning("MoldUDP64 socket error: %s", exc)

            def connection_lost(self, exc):
                logger.info(
                    "MoldUDP64 socket closed%s",
                    f": {exc}" if exc else "",
                )
                self._owner._transport = None

        try:
            transport_future = self._loop.create_datagram_endpoint(
                lambda: _MoldUDPProtocol(self),
                local_addr=(self._config.interface_address, 0),
                family=0,  # Let the OS decide IPv4/IPv6 from the group address
            )
            await transport_future
        except Exception:
            logger.exception("Failed to create MoldUDP64 datagram endpoint")
            raise

        self._session_active = True
        logger.info(
            "MoldUDP64 session %s started (seq=1, group=%s:%d)",
            self._config.session_name,
            self._config.multicast_group,
            self._config.multicast_port,
        )

    def stop(self) -> None:
        """Close the UDP socket and end the session."""
        self._session_active = False
        if self._transport:
            self._transport.close()
            self._transport = None

    # ------------------------------------------------------------------
    # Packet building
    # ------------------------------------------------------------------

    def pack_packet(
        self,
        messages: list[bytes],
        end_of_session: bool = False,
    ) -> bytes:
        """Pack one or more ITCH messages into a MoldUDP64 downstream packet.

        Args:
            messages: List of ITCH binary message blocks (each is raw bytes).
            end_of_session: If True, set Message Count to 0xFFFF.

        Returns:
            Complete MoldUDP64 downstream packet ready for UDP transmission.
        """
        if end_of_session:
            msg_count = _END_OF_SESSION_COUNT
            payload = b""
        elif not messages:
            # Heartbeat
            msg_count = _HEARTBEAT_COUNT
            payload = b""
        else:
            msg_count = len(messages)
            blocks: list[bytes] = []
            for msg in messages:
                if len(msg) > _MAX_MESSAGE_SIZE:
                    raise ValueError(
                        f"ITCH message too large: {len(msg)} > {_MAX_MESSAGE_SIZE}"
                    )
                blocks.append(
                    struct.pack(">H", len(msg)) + msg
                )
            payload = b"".join(blocks)

        header = (
            self._config.session_bytes
            + struct.pack(">QH", self._seq_num, msg_count)
        )

        assert len(header) == _MOLD_UDP_HEADER_SIZE
        return header + payload

    def heartbeat_packet(self) -> bytes:
        """Build a heartbeat downstream packet (Message Count = 0)."""
        return self.pack_packet([])

    def end_of_session_packet(self) -> bytes:
        """Build an End of Session downstream packet (Message Count = 0xFFFF)."""
        return self.pack_packet([], end_of_session=True)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send_packet(self, packet: bytes) -> None:
        """Transmit a downstream packet to the multicast group.

        Increments the sequence number.  Must only be called after start().
        """
        if not self._session_active:
            raise RuntimeError("MoldUDP64 session is not active")
        if self._transport is None:
            raise RuntimeError("MoldUDP64 transport is not connected")

        if len(packet) > self._config.max_payload_bytes:
            logger.warning(
                "MoldUDP64 packet exceeds max payload (%d > %d)",
                len(packet),
                self._config.max_payload_bytes,
            )

        self._transport.sendto(
            packet,
            (self._config.multicast_group, self._config.multicast_port),
        )
        self._seq_num += 1

    def send_heartbeat(self) -> None:
        """Send a single heartbeat packet."""
        if not self._session_active:
            return
        self.send_packet(self.heartbeat_packet())

    def send_end_of_session(self) -> None:
        """Send an End of Session packet."""
        if not self._session_active:
            return
        self.send_packet(self.end_of_session_packet())

    def send_messages(self, messages: list[bytes]) -> None:
        """Send one downstream packet containing the given ITCH messages."""
        if not messages:
            return
        self.send_packet(self.pack_packet(messages))

    # ------------------------------------------------------------------
    # Maximum payload helpers
    # ------------------------------------------------------------------

    @property
    def max_payload_bytes(self) -> int:
        return self._config.max_payload_bytes

    def available_message_bytes(self) -> int:
        """Return the max total message data bytes that fit in one packet."""
        return (
            self._config.max_payload_bytes
            - _MOLD_UDP_HEADER_SIZE
        )

    def max_message_blocks(
        self,
        message_sizes: list[int],
    ) -> int:
        """Return how many messages (by size) fit in one packet.

        Each message block adds 2 bytes for the Message Length field.
        """
        remaining = self.available_message_bytes()
        count = 0
        for size in message_sizes:
            block_size = size + _MESSAGE_BLOCK_LENGTH_SIZE
            if block_size > remaining:
                break
            remaining -= block_size
            count += 1
        return count

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def diagnostics(self) -> dict:
        return {
            "session_name": self._config.session_name,
            "multicast_group": self._config.multicast_group,
            "multicast_port": self._config.multicast_port,
            "sequence_number": self._seq_num,
            "session_active": self._session_active,
        }
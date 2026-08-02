"""SoupBinTCP 4.1 logical-packet framing and fixed-field codecs."""

from __future__ import annotations

from dataclasses import dataclass
import struct


PACKET_DEBUG = b"+"
PACKET_LOGIN_ACCEPTED = b"A"
PACKET_LOGIN_REJECTED = b"J"
PACKET_SEQUENCED_DATA = b"S"
PACKET_UNSEQUENCED_DATA = b"U"
PACKET_SERVER_HEARTBEAT = b"H"
PACKET_END_SESSION = b"Z"
PACKET_LOGIN_REQUEST = b"L"
PACKET_CLIENT_HEARTBEAT = b"R"
PACKET_LOGOUT_REQUEST = b"O"

LOGIN_REJECT_NOT_AUTHORIZED = b"A"
LOGIN_REJECT_SESSION_UNAVAILABLE = b"S"

MAX_PACKET_LENGTH = 0xFFFF
LOGIN_REQUEST_PAYLOAD_LENGTH = 51


class SoupProtocolError(ValueError):
    """Raised when bytes do not form a valid SoupBinTCP logical packet."""


@dataclass(frozen=True, slots=True)
class SoupPacket:
    packet_type: bytes
    payload: bytes = b""

    def __post_init__(self) -> None:
        if not isinstance(self.packet_type, bytes) or len(self.packet_type) != 1:
            raise ValueError("Soup packet_type must be exactly one byte")
        if not isinstance(self.payload, bytes):
            raise ValueError("Soup packet payload must be bytes")


@dataclass(frozen=True, slots=True)
class LoginRequest:
    username: str
    password: str
    requested_session: str
    requested_sequence: int
    heartbeat_timeout_ms: int


def _ascii(value: str, field_name: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    try:
        return value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must contain only ASCII") from exc


def _right_padded(value: str, width: int, field_name: str, *, nonempty: bool) -> bytes:
    encoded = _ascii(value, field_name)
    if nonempty and not encoded:
        raise ValueError(f"{field_name} cannot be empty")
    if len(encoded) > width:
        raise ValueError(f"{field_name} exceeds {width} ASCII bytes")
    if any(byte < 0x21 or byte > 0x7E for byte in encoded):
        raise ValueError(f"{field_name} must use printable non-space ASCII")
    return encoded.ljust(width, b" ")


def _left_padded(value: str, width: int, field_name: str) -> bytes:
    encoded = _ascii(value, field_name)
    if len(encoded) > width:
        raise ValueError(f"{field_name} exceeds {width} ASCII bytes")
    if encoded and not encoded.isalnum():
        raise ValueError(f"{field_name} must be ASCII alphanumeric")
    return encoded.rjust(width, b" ")


def _numeric(value: int, width: int, field_name: str) -> bytes:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    encoded = str(value).encode("ascii")
    if len(encoded) > width:
        raise ValueError(f"{field_name} exceeds {width} decimal digits")
    return encoded.rjust(width, b" ")


def _decode_ascii(value: bytes, field_name: str, *, strip_left: bool = False) -> str:
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SoupProtocolError(f"{field_name} must contain only ASCII") from exc
    return decoded.lstrip(" ") if strip_left else decoded.rstrip(" ")


def _decode_credential(value: bytes, field_name: str) -> str:
    decoded = _decode_ascii(value, field_name)
    if not decoded or " " in decoded:
        raise SoupProtocolError(f"{field_name} must be right-space-padded")
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in decoded):
        raise SoupProtocolError(f"{field_name} must use printable non-space ASCII")
    return decoded


def _decode_session(value: bytes) -> str:
    decoded = _decode_ascii(value, "requested session", strip_left=True)
    if decoded and (" " in decoded or not decoded.isalnum()):
        raise SoupProtocolError(
            "requested session must be left-space-padded ASCII alphanumeric"
        )
    return decoded


def _decode_numeric(value: bytes, field_name: str) -> int:
    try:
        raw = value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SoupProtocolError(f"{field_name} must contain only ASCII") from exc
    decoded = raw.lstrip(" ")
    if not decoded or " " in decoded:
        raise SoupProtocolError(f"{field_name} must be left-space-padded ASCII digits")
    if not decoded.isdecimal():
        raise SoupProtocolError(f"{field_name} must be space-padded ASCII digits")
    return int(decoded)


def encode_packet(packet_type: bytes, payload: bytes = b"") -> bytes:
    if not isinstance(packet_type, bytes) or len(packet_type) != 1:
        raise ValueError("Soup packet_type must be exactly one byte")
    if not isinstance(payload, bytes):
        raise ValueError("Soup payload must be bytes")
    packet_length = 1 + len(payload)
    if packet_length > MAX_PACKET_LENGTH:
        raise ValueError("Soup packet exceeds the unsigned 16-bit length field")
    return struct.pack(">H", packet_length) + packet_type + payload


def encode_login_request(
    username: str,
    password: str,
    requested_session: str = "",
    requested_sequence: int = 1,
    heartbeat_timeout_ms: int = 30_000,
) -> bytes:
    if not 1 <= heartbeat_timeout_ms <= 99_999:
        raise ValueError("heartbeat_timeout_ms must be between 1 and 99999")
    payload = (
        _right_padded(username, 6, "username", nonempty=True)
        + _right_padded(password, 10, "password", nonempty=True)
        + _left_padded(requested_session, 10, "requested session")
        + _numeric(requested_sequence, 20, "requested sequence")
        + _numeric(heartbeat_timeout_ms, 5, "heartbeat timeout")
    )
    return encode_packet(PACKET_LOGIN_REQUEST, payload)


def parse_login_request(packet: SoupPacket) -> LoginRequest:
    if packet.packet_type != PACKET_LOGIN_REQUEST:
        raise SoupProtocolError("first non-debug packet must be a Login Request")
    if len(packet.payload) != LOGIN_REQUEST_PAYLOAD_LENGTH:
        raise SoupProtocolError("Login Request payload must be exactly 51 bytes")
    username = _decode_credential(packet.payload[0:6], "username")
    password = _decode_credential(packet.payload[6:16], "password")
    session = _decode_session(packet.payload[16:26])
    sequence = _decode_numeric(packet.payload[26:46], "requested sequence")
    heartbeat = _decode_numeric(packet.payload[46:51], "heartbeat timeout")
    if not 1 <= heartbeat <= 99_999:
        raise SoupProtocolError("heartbeat timeout must be between 1 and 99999")
    return LoginRequest(username, password, session, sequence, heartbeat)


def encode_login_accepted(session: str, next_sequence: int) -> bytes:
    if not session:
        raise ValueError("accepted session cannot be empty")
    if (
        not isinstance(next_sequence, int)
        or isinstance(next_sequence, bool)
        or next_sequence < 1
    ):
        raise ValueError("accepted next sequence must be positive")
    payload = _left_padded(session, 10, "session") + _numeric(
        next_sequence, 20, "next sequence"
    )
    return encode_packet(PACKET_LOGIN_ACCEPTED, payload)


def encode_login_rejected(reason: bytes) -> bytes:
    if reason not in {LOGIN_REJECT_NOT_AUTHORIZED, LOGIN_REJECT_SESSION_UNAVAILABLE}:
        raise ValueError("invalid Soup login rejection reason")
    return encode_packet(PACKET_LOGIN_REJECTED, reason)


def encode_sequenced_data(message: bytes) -> bytes:
    return encode_packet(PACKET_SEQUENCED_DATA, message)


def encode_unsequenced_data(message: bytes) -> bytes:
    return encode_packet(PACKET_UNSEQUENCED_DATA, message)


def encode_debug(text: str) -> bytes:
    return encode_packet(PACKET_DEBUG, _ascii(text, "debug text"))


def encode_server_heartbeat() -> bytes:
    return encode_packet(PACKET_SERVER_HEARTBEAT)


def encode_client_heartbeat() -> bytes:
    return encode_packet(PACKET_CLIENT_HEARTBEAT)


def encode_logout_request() -> bytes:
    return encode_packet(PACKET_LOGOUT_REQUEST)


def encode_end_session() -> bytes:
    return encode_packet(PACKET_END_SESSION)


class SoupPacketParser:
    """Incrementally decode Soup logical packets across arbitrary TCP chunks."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> tuple[SoupPacket, ...]:
        if not isinstance(data, bytes):
            raise TypeError("Soup parser input must be bytes")
        self._buffer.extend(data)
        packets: list[SoupPacket] = []
        while len(self._buffer) >= 2:
            packet_length = struct.unpack(">H", self._buffer[:2])[0]
            if packet_length < 1:
                raise SoupProtocolError("Soup packet length must be at least 1")
            wire_length = 2 + packet_length
            if len(self._buffer) < wire_length:
                break
            packet_type = bytes(self._buffer[2:3])
            payload = bytes(self._buffer[3:wire_length])
            del self._buffer[:wire_length]
            packets.append(SoupPacket(packet_type, payload))
        return tuple(packets)

    def finish(self) -> None:
        if self._buffer:
            raise SoupProtocolError("truncated SoupBinTCP logical packet")


async def read_packet(reader: object) -> SoupPacket:
    """Read exactly one logical packet from an asyncio StreamReader-like object."""

    prefix = await reader.readexactly(2)
    packet_length = struct.unpack(">H", prefix)[0]
    if packet_length < 1:
        raise SoupProtocolError("Soup packet length must be at least 1")
    body = await reader.readexactly(packet_length)
    return SoupPacket(body[:1], body[1:])

"""SoupBinTCP 4.1 wire framing and login contract tests."""

import struct

import pytest

from src.mockx.soupbintcp_transport import (
    LOGIN_REJECT_NOT_AUTHORIZED,
    LOGIN_REJECT_SESSION_UNAVAILABLE,
    PACKET_CLIENT_HEARTBEAT,
    PACKET_END_SESSION,
    PACKET_LOGIN_ACCEPTED,
    PACKET_LOGIN_REJECTED,
    PACKET_LOGIN_REQUEST,
    PACKET_LOGOUT_REQUEST,
    PACKET_SEQUENCED_DATA,
    PACKET_SERVER_HEARTBEAT,
    LoginRequest,
    SoupPacketParser,
    SoupProtocolError,
    encode_client_heartbeat,
    encode_end_session,
    encode_login_accepted,
    encode_login_rejected,
    encode_login_request,
    encode_logout_request,
    encode_sequenced_data,
    encode_server_heartbeat,
    parse_login_request,
)


def test_login_request_exact_layout_and_round_trip():
    wire = encode_login_request(
        "GLIMP",
        "SECRET",
        requested_session="GS00000001",
        requested_sequence=42,
        heartbeat_timeout_ms=3_000,
    )

    assert len(wire) == 54
    assert struct.unpack(">H", wire[:2])[0] == 52
    assert wire[2:3] == PACKET_LOGIN_REQUEST
    assert wire[3:9] == b"GLIMP "
    assert wire[9:19] == b"SECRET    "
    assert wire[19:29] == b"GS00000001"
    assert wire[29:49] == b"                  42"
    assert wire[49:54] == b" 3000"

    packet = SoupPacketParser().feed(wire)[0]
    assert parse_login_request(packet) == LoginRequest(
        username="GLIMP",
        password="SECRET",
        requested_session="GS00000001",
        requested_sequence=42,
        heartbeat_timeout_ms=3_000,
    )


def test_login_responses_have_spec_lengths_and_padding():
    accepted = encode_login_accepted("GS1", 9)
    assert len(accepted) == 33
    assert struct.unpack(">H", accepted[:2])[0] == 31
    assert accepted[2:3] == PACKET_LOGIN_ACCEPTED
    assert accepted[3:13] == b"       GS1"
    assert accepted[13:33] == b"                   9"

    for reason in (
        LOGIN_REJECT_NOT_AUTHORIZED,
        LOGIN_REJECT_SESSION_UNAVAILABLE,
    ):
        rejected = encode_login_rejected(reason)
        assert rejected == b"\x00\x02" + PACKET_LOGIN_REJECTED + reason

    with pytest.raises(ValueError, match="session cannot be empty"):
        encode_login_accepted("", 1)
    with pytest.raises(ValueError, match="must be positive"):
        encode_login_accepted("GS00000001", 0)


def test_data_and_control_packet_vectors():
    assert encode_sequenced_data(b"ABC") == b"\x00\x04" + PACKET_SEQUENCED_DATA + b"ABC"
    assert encode_server_heartbeat() == b"\x00\x01" + PACKET_SERVER_HEARTBEAT
    assert encode_client_heartbeat() == b"\x00\x01" + PACKET_CLIENT_HEARTBEAT
    assert encode_logout_request() == b"\x00\x01" + PACKET_LOGOUT_REQUEST
    assert encode_end_session() == b"\x00\x01" + PACKET_END_SESSION


def test_parser_handles_byte_fragmentation_and_packet_coalescing():
    wires = (
        encode_server_heartbeat(),
        encode_sequenced_data(b"first"),
        encode_sequenced_data(b"second"),
    )
    parser = SoupPacketParser()
    packets = []
    for byte in b"".join(wires):
        packets.extend(parser.feed(bytes([byte])))
    parser.finish()

    assert [(packet.packet_type, packet.payload) for packet in packets] == [
        (PACKET_SERVER_HEARTBEAT, b""),
        (PACKET_SEQUENCED_DATA, b"first"),
        (PACKET_SEQUENCED_DATA, b"second"),
    ]


def test_parser_rejects_zero_length_and_truncated_packet():
    parser = SoupPacketParser()
    with pytest.raises(SoupProtocolError, match="at least 1"):
        parser.feed(b"\x00\x00")

    parser = SoupPacketParser()
    assert parser.feed(b"\x00\x04SAB") == ()
    with pytest.raises(SoupProtocolError, match="truncated"):
        parser.finish()


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("username", "TOOLONG", "username"),
        ("username", "GL IMP", "printable non-space"),
        ("password", "TOO-LONG-PASSWORD", "password"),
        ("requested_session", "TOO-LONG-SESSION", "session"),
    ],
)
def test_login_encoder_rejects_invalid_fixed_width_ascii(field, value, match):
    values = {
        "username": "GLIMP",
        "password": "SECRET",
        "requested_session": "",
        "requested_sequence": 1,
        "heartbeat_timeout_ms": 3_000,
    }
    values[field] = value
    with pytest.raises(ValueError, match=match):
        encode_login_request(**values)


def test_login_parser_requires_exact_shape_and_numeric_fields():
    good = bytearray(encode_login_request("GLIMP", "SECRET"))
    good[29:49] = b"not-a-number        "
    packet = SoupPacketParser().feed(bytes(good))[0]
    with pytest.raises(SoupProtocolError, match="sequence"):
        parse_login_request(packet)

    wrong_padding = bytearray(encode_login_request("GLIMP", "SECRET"))
    wrong_padding[29:49] = b"1" + (b" " * 19)
    packet = SoupPacketParser().feed(bytes(wrong_padding))[0]
    with pytest.raises(SoupProtocolError, match="left-space-padded"):
        parse_login_request(packet)

    wrong_credential_padding = bytearray(encode_login_request("GLIMP", "SECRET"))
    wrong_credential_padding[3:9] = b" GLIMP"
    packet = SoupPacketParser().feed(bytes(wrong_credential_padding))[0]
    with pytest.raises(SoupProtocolError, match="right-space-padded"):
        parse_login_request(packet)

    wrong_session_padding = bytearray(
        encode_login_request("GLIMP", "SECRET", "GS00000001")
    )
    wrong_session_padding[19:29] = b"GS1       "
    packet = SoupPacketParser().feed(bytes(wrong_session_padding))[0]
    with pytest.raises(SoupProtocolError, match="left-space-padded"):
        parse_login_request(packet)

    truncated_payload = SoupPacketParser().feed(b"\x00\x02LX")[0]
    with pytest.raises(SoupProtocolError, match="exactly 51"):
        parse_login_request(truncated_payload)

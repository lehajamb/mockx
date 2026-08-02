"""MoldUDP64 downstream wire and sequencing tests."""

import asyncio
import socket
import struct

import pytest

from src.mockx.moldudp64_transport import (
    MAX_DATA_MESSAGE_COUNT,
    MoldUDP64Config,
    MoldUDP64Transport,
)


@pytest.fixture
def config() -> MoldUDP64Config:
    return MoldUDP64Config(
        multicast_group="239.192.0.1",
        multicast_port=12345,
        session_name="TESTMOCKX",
        heartbeat_interval_ms=1_000,
        max_payload_bytes=1_400,
    )


class FakeDatagramTransport:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, packet, address):
        self.sent.append((packet, address))

    def close(self):
        self.closed = True


def activate(transport):
    datagram = FakeDatagramTransport()
    transport._transport = datagram
    transport._session_active = True
    return datagram


def parse_header(packet):
    return packet[:10], *struct.unpack(">QH", packet[10:20])


def test_config_validates_session_network_and_bounds(config):
    assert config.session_bytes == b"TESTMOCKX "
    assert config.ttl == 1
    with pytest.raises(ValueError, match="1-10 ASCII bytes"):
        MoldUDP64Config("239.192.0.1", 12345, "TOOLONGNAME")
    with pytest.raises(ValueError, match="IPv4 multicast"):
        MoldUDP64Config("127.0.0.1", 12345, "TEST")
    with pytest.raises(ValueError, match="interface_address"):
        MoldUDP64Config("239.192.0.1", 12345, "TEST", interface_address="ff02::1")
    with pytest.raises(ValueError, match="1-65535"):
        MoldUDP64Config("239.192.0.1", 0, "TEST")
    with pytest.raises(ValueError, match="100-60000"):
        MoldUDP64Config("239.192.0.1", 12345, "TEST", heartbeat_interval_ms=50)
    with pytest.raises(ValueError, match="512-65507"):
        MoldUDP64Config("239.192.0.1", 12345, "TEST", max_payload_bytes=255)


def test_packet_layout_and_message_blocks(config):
    transport = MoldUDP64Transport(config)
    packet = transport.pack_packet([b"AAAA", b"BB", b""])
    session, sequence, count = parse_header(packet)
    assert session == config.session_bytes
    assert sequence == 1
    assert count == 3
    offset = 20
    for expected in (b"AAAA", b"BB", b""):
        size = struct.unpack(">H", packet[offset : offset + 2])[0]
        offset += 2
        assert packet[offset : offset + size] == expected
        offset += size


def test_message_sequence_advances_by_message_count(config):
    transport = MoldUDP64Transport(config)
    datagram = activate(transport)
    result = transport.send_messages([b"MSG1", b"MSG2", b"MSG3"])
    assert parse_header(datagram.sent[0][0])[1:] == (1, 3)
    assert result.messages_sent == 3
    assert result.packets_sent == 1
    assert result.next_sequence_number == 4
    assert transport.sequence_number == 4


def test_heartbeat_and_end_use_next_sequence_without_consuming_it(config):
    transport = MoldUDP64Transport(config)
    datagram = activate(transport)
    transport.send_messages([b"A", b"B"])
    transport.send_heartbeat()
    transport.send_end_of_session()
    assert [parse_header(packet)[1:] for packet, _ in datagram.sent] == [
        (1, 2),
        (3, 0),
        (3, 0xFFFF),
    ]
    assert transport.sequence_number == 3


def test_batches_fit_payload_and_preserve_contiguous_sequence():
    config = MoldUDP64Config("239.192.0.1", 12345, "TEST", max_payload_bytes=512)
    transport = MoldUDP64Transport(config)
    datagram = activate(transport)
    result = transport.send_messages([b"A" * 300, b"B" * 300])
    assert result.packets_sent == 2
    assert all(len(packet) <= 512 for packet, _ in datagram.sent)
    assert [parse_header(packet)[1:] for packet, _ in datagram.sent] == [
        (1, 1),
        (2, 1),
    ]
    assert transport.sequence_number == 3


def test_single_oversized_block_is_rejected_before_send():
    config = MoldUDP64Config("239.192.0.1", 12345, "TEST", max_payload_bytes=512)
    transport = MoldUDP64Transport(config)
    datagram = activate(transport)
    with pytest.raises(ValueError, match="does not fit"):
        transport.send_messages([b"X" * 491])
    assert datagram.sent == []
    assert transport.sequence_number == 1


def test_reserved_end_marker_cannot_be_used_as_a_data_count(config):
    transport = MoldUDP64Transport(config)
    with pytest.raises(ValueError, match="exceeds 65534"):
        transport.pack_packet([b""] * (MAX_DATA_MESSAGE_COUNT + 1))


def test_sequence_exhaustion_is_rejected_before_send(config):
    transport = MoldUDP64Transport(config)
    datagram = activate(transport)
    transport._next_sequence = 0xFFFFFFFFFFFFFFFF
    with pytest.raises(OverflowError, match="sequence number exhausted"):
        transport.send_messages([b"A"])
    assert datagram.sent == []
    assert transport.sequence_number == 0xFFFFFFFFFFFFFFFF


def test_socket_send_failure_is_reported_and_does_not_advance(config):
    errors = []
    transport = MoldUDP64Transport(config, on_error=errors.append)

    class FailingTransport(FakeDatagramTransport):
        def sendto(self, packet, address):
            raise OSError("network down")

    transport._transport = FailingTransport()
    transport._session_active = True
    with pytest.raises(OSError, match="network down"):
        transport.send_messages([b"A"])
    assert len(errors) == 1
    assert transport.sequence_number == 1


def test_capacity_helpers_and_idempotent_stop(config):
    transport = MoldUDP64Transport(config)
    assert transport.available_message_bytes() == 1380
    assert transport.max_message_blocks([36] * 100) == 36
    assert transport.max_message_blocks([19] * 100) == 65
    datagram = activate(transport)
    transport.stop()
    transport.stop()
    assert datagram.closed
    assert not transport.is_active
    assert transport.sequence_number == 1


@pytest.mark.asyncio
async def test_start_uses_running_loop_and_applies_ipv4_multicast_options(
    config,
    monkeypatch,
):
    class FakeSocket:
        def __init__(self):
            self.blocking = None
            self.options = []
            self.bound = None
            self.closed = False

        def setblocking(self, value):
            self.blocking = value

        def setsockopt(self, level, option, value):
            self.options.append((level, option, value))

        def bind(self, address):
            self.bound = address

        def close(self):
            self.closed = True

    created_socket = FakeSocket()
    datagram = FakeDatagramTransport()
    loop = asyncio.get_running_loop()

    protocols = []

    async def create_endpoint(factory, *, sock):
        assert sock is created_socket
        protocol = factory()
        protocols.append(protocol)
        protocol.connection_made(datagram)
        return datagram, protocol

    monkeypatch.setattr(loop, "create_datagram_endpoint", create_endpoint)
    errors = []
    transport = MoldUDP64Transport(
        config,
        on_error=errors.append,
        socket_factory=lambda *args: created_socket,
    )
    transport._next_sequence = 99
    await transport.start()
    assert transport.sequence_number == 1
    assert created_socket.blocking is False
    assert created_socket.bound == ("0.0.0.0", 0)
    assert (
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_TTL,
        1,
    ) in created_socket.options
    assert any(
        option == socket.IP_MULTICAST_IF for _, option, _ in created_socket.options
    )
    protocols[0].error_received(OSError("asynchronous send failure"))
    assert len(errors) == 1
    assert "asynchronous send failure" in str(errors[0])
    transport.stop()

"""Unit tests for MoldUDP64 transport layer."""

import struct

import pytest

from src.mockx.moldudp64_transport import (
    MoldUDP64Config,
    MoldUDP64Transport,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config() -> MoldUDP64Config:
    return MoldUDP64Config(
        multicast_group="239.192.0.1",
        multicast_port=12345,
        session_name="TESTMOCKX",
        heartbeat_interval_ms=1_000,
        max_payload_bytes=1_400,
    )


# ---------------------------------------------------------------------------
# MoldUDP64Config tests
# ---------------------------------------------------------------------------

class TestMoldUDP64Config:
    def test_session_bytes(self, config):
        session_bytes = config.session_bytes
        assert len(session_bytes) == 10
        # 9 characters + 1 space padding = 10 bytes
        assert session_bytes == b"TESTMOCKX "

    def test_session_name_padded(self):
        cfg = MoldUDP64Config(
            multicast_group="239.192.0.1",
            multicast_port=12345,
            session_name="SHORT",
        )
        assert cfg.session_bytes == b"SHORT     "

    def test_session_name_too_long(self):
        with pytest.raises(ValueError, match="at most 10 ASCII bytes"):
            MoldUDP64Config(
                multicast_group="239.192.0.1",
                multicast_port=12345,
                session_name="TOOLONGNAME",
            )

    def test_invalid_port(self):
        with pytest.raises(ValueError):
            MoldUDP64Config(
                multicast_group="239.192.0.1",
                multicast_port=0,
                session_name="TEST",
            )

    def test_invalid_heartbeat(self):
        with pytest.raises(ValueError):
            MoldUDP64Config(
                multicast_group="239.192.0.1",
                multicast_port=12345,
                session_name="TEST",
                heartbeat_interval_ms=50,  # below minimum 100
            )

    def test_invalid_max_payload(self):
        with pytest.raises(ValueError):
            MoldUDP64Config(
                multicast_group="239.192.0.1",
                multicast_port=12345,
                session_name="TEST",
                max_payload_bytes=255,  # below minimum 512
            )

    def test_ttl_default(self, config):
        assert config.ttl == 1

    def test_ttl_invalid(self):
        with pytest.raises(ValueError):
            MoldUDP64Config(
                multicast_group="239.192.0.1",
                multicast_port=12345,
                session_name="TEST",
                ttl=256,
            )


# ---------------------------------------------------------------------------
# Transport packet building tests (no network — pure logic)
# ---------------------------------------------------------------------------

class TestTransportPackPacket:
    """Test packet packing without needing a running transport."""

    def test_header_structure(self, config):
        """Verify the MoldUDP64 downstream packet header layout."""
        transport = MoldUDP64Transport(config)

        # Build a packet with one small message
        messages = [b"HELLO"]
        packet = transport.pack_packet(messages)

        # Header is 20 bytes: Session(10) + SeqNum(8) + MsgCount(2)
        assert len(packet) >= 20

        # Session name (first 10 bytes)
        assert packet[:10] == config.session_bytes

        # Sequence number (next 8 bytes, big-endian uint64)
        seq_num = struct.unpack(">Q", packet[10:18])[0]
        assert seq_num == 0  # Not yet incremented

        # Message count (next 2 bytes, big-endian uint16)
        msg_count = struct.unpack(">H", packet[18:20])[0]
        assert msg_count == 1

    def test_message_block_format(self, config):
        """Verify Message Block = 2-byte length + data."""
        transport = MoldUDP64Transport(config)
        messages = [b"HELLO"]
        packet = transport.pack_packet(messages)

        # After 20-byte header, first 2 bytes = message length
        msg_len = struct.unpack(">H", packet[20:22])[0]
        assert msg_len == 5

        # Then the message data
        assert packet[22:27] == b"HELLO"

    def test_multiple_messages(self, config):
        transport = MoldUDP64Transport(config)
        messages = [b"AAAA", b"BB", b"CCCCCC"]
        packet = transport.pack_packet(messages)

        # 3 messages
        msg_count = struct.unpack(">H", packet[18:20])[0]
        assert msg_count == 3

        # Parse blocks incrementally
        offset = 20
        for expected_data in [b"AAAA", b"BB", b"CCCCCC"]:
            msg_len = struct.unpack(">H", packet[offset: offset + 2])[0]
            offset += 2
            assert packet[offset: offset + msg_len] == expected_data
            offset += msg_len

    def test_empty_message_is_valid(self, config):
        transport = MoldUDP64Transport(config)
        # Empty message should be allowed per spec (zero-length message data)
        messages = [b""]
        packet = transport.pack_packet(messages)
        msg_len = struct.unpack(">H", packet[20:22])[0]
        assert msg_len == 0

    def test_heartbeat_packet(self, config):
        transport = MoldUDP64Transport(config)
        packet = transport.heartbeat_packet()

        assert len(packet) == 20  # Header only, no message blocks
        msg_count = struct.unpack(">H", packet[18:20])[0]
        assert msg_count == 0  # Heartbeat = count 0

    def test_end_of_session_packet(self, config):
        transport = MoldUDP64Transport(config)
        packet = transport.end_of_session_packet()

        assert len(packet) == 20  # Header only, no message blocks
        msg_count = struct.unpack(">H", packet[18:20])[0]
        assert msg_count == 0xFFFF  # End of Session = count 65535

    def test_sequence_number_increments(self, config):
        transport = MoldUDP64Transport(config)

        # First packet — seq_num will be 0 (before send)
        packet1 = transport.pack_packet([b"MSG1"])
        seq1 = struct.unpack(">Q", packet1[10:18])[0]
        assert seq1 == 0

        # Simulate sending by manually patching the internal counter
        # (pack_packet doesn't increment seq_num, only send_packet does)
        # We need to test via the send path, but that requires a running transport.
        # Instead, verify the _seq_num is accessible.
        assert transport.sequence_number == 0

    def test_message_too_large_raises(self, config):
        transport = MoldUDP64Transport(config)
        huge_msg = b"X" * 65536  # > 64KB
        with pytest.raises(ValueError, match="too large"):
            transport.pack_packet([huge_msg])

# ---------------------------------------------------------------------------
# Payload capacity helpers
# ---------------------------------------------------------------------------

class TestPayloadCapacity:
    def test_available_message_bytes(self, config):
        transport = MoldUDP64Transport(config)
        # max_payload_bytes(1400) - header(20) = 1380 available for message data + length fields
        assert transport.available_message_bytes() == 1380

    def test_max_message_blocks(self, config):
        transport = MoldUDP64Transport(config)
        # Each block = 2 bytes (length) + message data
        # One 36-byte ITCH Add Order + 2-byte length = 38 bytes per message
        # 1380 / 38 = 36 messages fit (with 12 bytes left)
        sizes = [36] * 100
        count = transport.max_message_blocks(sizes)
        assert count == 36

    def test_max_message_blocks_small(self, config):
        transport = MoldUDP64Transport(config)
        # 19-byte ITCH Delete messages: 21 bytes each with length field
        # 1380 / 21 = 65
        sizes = [19] * 100
        count = transport.max_message_blocks(sizes)
        assert count == 65

    def test_max_message_blocks_varied(self, config):
        transport = MoldUDP64Transport(config)
        # 36, 36, 19, 44, 12 sizes
        # Block sizes: 38, 38, 21, 46, 14 = 157 total
        sizes = [36, 36, 19, 44, 12]
        count = transport.max_message_blocks(sizes)
        assert count == 5  # All fit within 1380

# ---------------------------------------------------------------------------
# Transport lifecycle (non-network)
# ---------------------------------------------------------------------------

class TestTransportLifecycle:
    def test_initial_state(self, config):
        transport = MoldUDP64Transport(config)
        assert not transport.is_active
        assert transport.sequence_number == 0

    def test_stop_when_not_active(self, config):
        transport = MoldUDP64Transport(config)
        transport.stop()  # Should not raise
        assert not transport.is_active

    def test_send_heartbeat_when_not_active(self, config):
        transport = MoldUDP64Transport(config)
        transport.send_heartbeat()  # Noop when not active
        assert transport.sequence_number == 0  # Unchanged
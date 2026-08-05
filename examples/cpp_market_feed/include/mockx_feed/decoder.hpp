#pragma once

#include "mockx_feed/order_book.hpp"
#include "mockx_feed/protocol.hpp"

#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <string>

namespace mockx::feed {

enum class DecodeContext { glimpse, live_itch };

class MessageDecoder {
public:
    // Returns the G marker for a GLIMPSE End of Snapshot, otherwise zero.
    [[nodiscard]] std::uint64_t apply(
        std::span<std::byte> message,
        OrderBook& book,
        DecodeContext context) const;
};

[[nodiscard]] bool is_end_of_messages(std::span<std::byte> message);

struct MoldPacketInfo {
    std::string session;
    std::uint64_t first_sequence{};
    std::uint16_t message_count{};

    [[nodiscard]] bool heartbeat() const noexcept { return message_count == 0; }
    [[nodiscard]] bool end_of_session() const noexcept {
        return message_count == 0xffffU;
    }
};

inline MoldPacketInfo inspect_mold_header(std::span<std::byte> packet) {
    if (packet.size() < sizeof(MoldHeaderWire)) {
        throw ProtocolError("Mold packet is shorter than its header");
    }
    auto* header = overlay<MoldHeaderWire>(packet.data());
    MoldPacketInfo info{
        trim_ascii(header->session),
        load_be(header->sequence),
        static_cast<std::uint16_t>(load_be(header->message_count))};
    if (info.session.empty() || info.first_sequence == 0) {
        throw ProtocolError("Mold session and sequence must be non-empty");
    }
    return info;
}

template <typename Callback>
MoldPacketInfo parse_mold_packet(std::span<std::byte> packet, Callback&& callback) {
    const auto info = inspect_mold_header(packet);
    if (info.heartbeat() || info.end_of_session()) {
        if (packet.size() != sizeof(MoldHeaderWire)) {
            throw ProtocolError("Mold control packet contains an unexpected payload");
        }
        return info;
    }

    std::size_t offset = sizeof(MoldHeaderWire);
    for (std::uint32_t index = 0; index < info.message_count; ++index) {
        if (packet.size() - offset < 2) {
            throw ProtocolError("Mold message length field is truncated");
        }
        const auto length = static_cast<std::size_t>(
            (std::to_integer<std::uint8_t>(packet[offset]) << 8U)
            | std::to_integer<std::uint8_t>(packet[offset + 1]));
        offset += 2;
        if (length == 0 || length > packet.size() - offset) {
            throw ProtocolError("Mold message payload is truncated or empty");
        }
        offset += length;
    }
    if (offset != packet.size()) {
        throw ProtocolError("Mold packet has trailing bytes");
    }
    if (info.first_sequence
        > std::numeric_limits<std::uint64_t>::max() - info.message_count) {
        throw ProtocolError("Mold sequence range overflows uint64");
    }

    offset = sizeof(MoldHeaderWire);
    for (std::uint32_t index = 0; index < info.message_count; ++index) {
        const auto length = static_cast<std::size_t>(
            (std::to_integer<std::uint8_t>(packet[offset]) << 8U)
            | std::to_integer<std::uint8_t>(packet[offset + 1]));
        offset += 2;
        callback(
            info.first_sequence + index,
            packet.subspan(offset, length));
        offset += length;
    }
    return info;
}

} // namespace mockx::feed

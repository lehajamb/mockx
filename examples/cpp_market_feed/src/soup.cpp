#include "mockx_feed/soup.hpp"

#include "mockx_feed/protocol.hpp"

#include <array>
#include <string>

namespace mockx::feed {
namespace {

void append_fixed(
    std::vector<std::byte>& wire,
    std::string_view value,
    std::size_t width,
    bool left_pad,
    bool numeric = false) {
    if (value.empty() && !left_pad) {
        throw ProtocolError("required Soup field is empty");
    }
    if (value.size() > width) {
        throw ProtocolError("Soup fixed-width field is too long");
    }
    for (const char ch : value) {
        const auto byte = static_cast<unsigned char>(ch);
        if ((numeric && (ch < '0' || ch > '9'))
            || (!numeric && (byte < 0x21U || byte > 0x7eU))) {
            throw ProtocolError("Soup field contains invalid ASCII");
        }
    }
    const auto padding = width - value.size();
    if (left_pad) {
        wire.insert(wire.end(), padding, std::byte{' '});
    }
    for (const char ch : value) {
        wire.push_back(static_cast<std::byte>(static_cast<unsigned char>(ch)));
    }
    if (!left_pad) {
        wire.insert(wire.end(), padding, std::byte{' '});
    }
}

} // namespace

std::vector<std::byte> encode_soup_login_request(
    std::string_view username,
    std::string_view password,
    std::string_view requested_session,
    std::uint64_t requested_sequence,
    std::uint32_t heartbeat_timeout_ms) {
    if (requested_sequence == 0) {
        throw ProtocolError("Soup requested sequence must be positive");
    }
    if (heartbeat_timeout_ms == 0 || heartbeat_timeout_ms > 99'999) {
        throw ProtocolError("Soup heartbeat timeout must be 1-99999 ms");
    }
    std::vector<std::byte> wire;
    wire.reserve(54);
    wire.push_back(std::byte{0});
    wire.push_back(std::byte{52});
    wire.push_back(std::byte{'L'});
    append_fixed(wire, username, 6, false);
    append_fixed(wire, password, 10, false);
    append_fixed(wire, requested_session, 10, true);
    append_fixed(wire, std::to_string(requested_sequence), 20, true, true);
    append_fixed(wire, std::to_string(heartbeat_timeout_ms), 5, true, true);
    return wire;
}

std::array<std::byte, 3> encode_soup_control(char type) {
    if (type != 'R' && type != 'O') {
        throw ProtocolError("unsupported client Soup control packet");
    }
    return {std::byte{0}, std::byte{1}, static_cast<std::byte>(type)};
}

SoupPacket read_soup_packet(TcpSocket& socket) {
    std::array<std::byte, 2> prefix{};
    socket.read_exact(prefix);
    const auto length = static_cast<std::size_t>(
        (std::to_integer<std::uint8_t>(prefix[0]) << 8U)
        | std::to_integer<std::uint8_t>(prefix[1]));
    if (length == 0) {
        throw ProtocolError("Soup packet length must include a packet type");
    }
    std::vector<std::byte> body(length);
    socket.read_exact(body);
    SoupPacket packet;
    packet.type = static_cast<char>(std::to_integer<unsigned char>(body.front()));
    packet.payload.assign(body.begin() + 1, body.end());
    return packet;
}

std::string decode_soup_session(std::span<const std::byte> field) {
    if (field.size() != 10) {
        throw ProtocolError("Soup session field must be 10 bytes");
    }
    std::array<char, 10> value{};
    for (std::size_t index = 0; index < value.size(); ++index) {
        value[index] = static_cast<char>(std::to_integer<unsigned char>(field[index]));
    }
    return trim_ascii(value);
}

std::uint64_t decode_soup_decimal(std::span<const std::byte> field) {
    std::string value;
    value.reserve(field.size());
    for (const auto byte : field) {
        value.push_back(static_cast<char>(std::to_integer<unsigned char>(byte)));
    }
    return parse_padded_decimal(value);
}

} // namespace mockx::feed

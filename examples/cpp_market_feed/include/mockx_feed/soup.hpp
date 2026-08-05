#pragma once

#include "mockx_feed/net.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace mockx::feed {

struct SoupPacket {
    char type{};
    std::vector<std::byte> payload;
};

[[nodiscard]] std::vector<std::byte> encode_soup_login_request(
    std::string_view username,
    std::string_view password,
    std::string_view requested_session,
    std::uint64_t requested_sequence,
    std::uint32_t heartbeat_timeout_ms);
[[nodiscard]] std::array<std::byte, 3> encode_soup_control(char type);
[[nodiscard]] SoupPacket read_soup_packet(TcpSocket& socket);
[[nodiscard]] std::string decode_soup_session(std::span<const std::byte> field);
[[nodiscard]] std::uint64_t decode_soup_decimal(std::span<const std::byte> field);

} // namespace mockx::feed

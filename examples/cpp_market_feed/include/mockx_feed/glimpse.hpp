#pragma once

#include "mockx_feed/decoder.hpp"
#include "mockx_feed/net.hpp"
#include "mockx_feed/order_book.hpp"

#include <cstdint>
#include <stdexcept>
#include <string>

namespace mockx::feed {

class AuthenticationError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class GlimpseUnavailable : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct GlimpseConfig {
    std::string host{"127.0.0.1"};
    std::uint16_t port{18001};
    std::string username{"GLIMP"};
    std::string password;
    std::uint32_t heartbeat_timeout_ms{30'000};
    std::uint32_t transport_retries{3};
};

struct GlimpseSnapshot {
    OrderBook book;
    std::uint64_t next_itch_sequence{1};
};

class GlimpseClient {
public:
    explicit GlimpseClient(GlimpseConfig config) : config_(std::move(config)) {}

    [[nodiscard]] GlimpseSnapshot capture() const;

private:
    GlimpseConfig config_;
};

} // namespace mockx::feed

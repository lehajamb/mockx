#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <set>
#include <stdexcept>
#include <string>

namespace mockx::feed {

class CommandLineError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct AppConfig {
    std::string itch_group{"239.192.0.1"};
    std::uint16_t itch_port{18000};
    std::string interface_address{"0.0.0.0"};
    std::string session_prefix{"MX"};
    std::string glimpse_host{"127.0.0.1"};
    std::uint16_t glimpse_port{18001};
    std::string username{"GLIMP"};
    std::string password;
    std::set<std::string> symbols;
    std::size_t levels{10};
    std::uint32_t refresh_ms{250};
    std::size_t ring_capacity{4096};
    std::size_t datagram_bytes{2048};
    std::uint32_t resync_retries{3};
};

struct CommandLine {
    AppConfig config;
    bool help{};
    bool version{};
};

[[nodiscard]] CommandLine parse_command_line(int argc, char** argv);
[[nodiscard]] std::string usage();
int run_market_feed(const AppConfig& config, std::atomic<bool>& stop);

} // namespace mockx::feed

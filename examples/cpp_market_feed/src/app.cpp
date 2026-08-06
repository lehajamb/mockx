#include "mockx_feed/app.hpp"

#include "mockx_feed/feed_handler.hpp"
#include "mockx_feed/renderer.hpp"
#include "mockx_feed/spsc_ring.hpp"

#include <algorithm>
#include <charconv>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <string_view>
#include <thread>

namespace mockx::feed {
namespace {

template <typename T>
T parse_unsigned(std::string_view text, std::string_view option) {
    std::uint64_t value = 0;
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (error != std::errc{} || end != text.data() + text.size()
        || value > std::numeric_limits<T>::max()) {
        throw CommandLineError(std::string(option) + " requires an unsigned integer");
    }
    return static_cast<T>(value);
}

std::string require_value(int& index, int argc, char** argv, std::string_view option) {
    if (++index >= argc) {
        throw CommandLineError(std::string(option) + " requires a value");
    }
    return argv[index];
}

void validate_config(const AppConfig& config) {
    if (config.itch_port == 0 || config.glimpse_port == 0) {
        throw CommandLineError("ports must be between 1 and 65535");
    }
    if (config.session_prefix.empty() || config.session_prefix.size() > 2
        || !std::all_of(
            config.session_prefix.begin(),
            config.session_prefix.end(),
            [](unsigned char ch) { return ch >= 0x21U && ch <= 0x7eU; })) {
        throw CommandLineError("--session-prefix must be 1-2 printable ASCII bytes");
    }
    if (config.username.empty() || config.username.size() > 6) {
        throw CommandLineError("--username must contain 1-6 bytes");
    }
    if (config.password.empty() || config.password.size() > 10) {
        throw CommandLineError(
            "provide a 1-10 byte --password or MOCKX_GLIMPSE_PASSWORD");
    }
    const auto printable_credential = [](unsigned char ch) {
        return ch >= 0x21U && ch <= 0x7eU;
    };
    if (!std::all_of(
            config.username.begin(), config.username.end(), printable_credential)
        || !std::all_of(
            config.password.begin(), config.password.end(), printable_credential)) {
        throw CommandLineError("Soup credentials must use printable non-space ASCII");
    }
    if (config.levels == 0 || config.refresh_ms == 0) {
        throw CommandLineError("--levels and --refresh-ms must be positive");
    }
    if (!is_power_of_two(config.ring_capacity)) {
        throw CommandLineError("--ring-capacity must be a power of two and at least 2");
    }
    if (config.datagram_bytes < 512 || config.datagram_bytes > 65'507) {
        throw CommandLineError("--datagram-bytes must be between 512 and 65507");
    }
}

std::string state_name(FeedState state) {
    switch (state) {
    case FeedState::snapshotting:
        return "SNAPSHOTTING";
    case FeedState::recovering:
        return "RECOVERING";
    case FeedState::live:
        return "LIVE";
    case FeedState::end_of_session:
        return "END_OF_SESSION";
    case FeedState::stopped:
        return "STOPPED";
    }
    return "UNKNOWN";
}

class ConsoleObserver final : public FeedObserver {
public:
    ConsoleObserver(ObjectRing<DisplaySnapshot>& ring, const AppConfig& config)
        : ring_(ring), config_(config) {}

    [[nodiscard]] bool publish(
        const FeedStatus& status,
        const OrderBook& book) override {
        const auto now = std::chrono::steady_clock::now();
        if (status.state == FeedState::live && now < next_render_) {
            return true;
        }
        DisplaySnapshot snapshot;
        snapshot.connection_state = state_name(status.state);
        snapshot.mold_session = status.mold_session;
        snapshot.next_sequence = status.next_sequence;
        snapshot.counters = status.counters;
        snapshot.instruments = book.display(config_.levels, config_.symbols);
        (void)ring_.try_push(std::move(snapshot));
        if (status.state == FeedState::live) {
            next_render_ = now + std::chrono::milliseconds(config_.refresh_ms);
        }
        return true;
    }

private:
    ObjectRing<DisplaySnapshot>& ring_;
    const AppConfig& config_;
    std::chrono::steady_clock::time_point next_render_{};
};

} // namespace

CommandLine parse_command_line(int argc, char** argv) {
    CommandLine result;
    for (int index = 1; index < argc; ++index) {
        const std::string_view option(argv[index]);
        if (option == "--help" || option == "-h") {
            result.help = true;
        } else if (option == "--version") {
            result.version = true;
        } else if (option == "--itch-group") {
            result.config.itch_group = require_value(index, argc, argv, option);
        } else if (option == "--itch-port") {
            result.config.itch_port = parse_unsigned<std::uint16_t>(
                require_value(index, argc, argv, option), option);
        } else if (option == "--interface") {
            result.config.interface_address = require_value(index, argc, argv, option);
        } else if (option == "--session-prefix") {
            result.config.session_prefix = require_value(index, argc, argv, option);
        } else if (option == "--glimpse-host") {
            result.config.glimpse_host = require_value(index, argc, argv, option);
        } else if (option == "--glimpse-port") {
            result.config.glimpse_port = parse_unsigned<std::uint16_t>(
                require_value(index, argc, argv, option), option);
        } else if (option == "--username") {
            result.config.username = require_value(index, argc, argv, option);
        } else if (option == "--password") {
            result.config.password = require_value(index, argc, argv, option);
        } else if (option == "--symbol") {
            result.config.symbols.insert(require_value(index, argc, argv, option));
        } else if (option == "--levels") {
            result.config.levels = parse_unsigned<std::size_t>(
                require_value(index, argc, argv, option), option);
        } else if (option == "--refresh-ms") {
            result.config.refresh_ms = parse_unsigned<std::uint32_t>(
                require_value(index, argc, argv, option), option);
        } else if (option == "--ring-capacity") {
            result.config.ring_capacity = parse_unsigned<std::size_t>(
                require_value(index, argc, argv, option), option);
        } else if (option == "--datagram-bytes") {
            result.config.datagram_bytes = parse_unsigned<std::size_t>(
                require_value(index, argc, argv, option), option);
        } else if (option == "--resync-retries") {
            result.config.resync_retries = parse_unsigned<std::uint32_t>(
                require_value(index, argc, argv, option), option);
        } else {
            throw CommandLineError("unknown option: " + std::string(option));
        }
    }

    if (result.config.password.empty()) {
        if (const char* password = std::getenv("MOCKX_GLIMPSE_PASSWORD")) {
            result.config.password = password;
        }
    }
    if (!result.help && !result.version) {
        validate_config(result.config);
    }
    return result;
}

std::string usage() {
    return R"(mockx_market_feed 0.1.0

Usage: mockx_market_feed [options]

  --itch-group ADDRESS       MoldUDP64 multicast group (239.192.0.1)
  --itch-port PORT           MoldUDP64 multicast port (18000)
  --interface ADDRESS        Local IPv4 multicast interface (0.0.0.0)
  --session-prefix PREFIX    Expected MockX Mold prefix (MX)
  --glimpse-host HOST        GLIMPSE SoupBinTCP host (127.0.0.1)
  --glimpse-port PORT        GLIMPSE SoupBinTCP port (18001)
  --username USER            Soup username (GLIMP)
  --password PASSWORD        Soup password; or MOCKX_GLIMPSE_PASSWORD
  --symbol SYMBOL            Display one symbol; repeat to select more
  --levels COUNT             Displayed price levels per side (10)
  --refresh-ms MS            Console refresh interval (250)
  --ring-capacity COUNT      Power-of-two ingress slots (4096)
  --datagram-bytes BYTES     Bytes per ingress slot (2048)
  --resync-retries COUNT     GLIMPSE recovery attempts (3)
  --help                     Show this help
  --version                  Show the version
)";
}

int run_market_feed(const AppConfig& config, std::atomic<bool>& stop) {
    ObjectRing<DisplaySnapshot> display_ring(8);
    ConsoleObserver observer(display_ring, config);
    std::thread renderer_thread(
        [&display_ring, &stop] { renderer_loop(display_ring, stop); });
    try {
        const auto result = FeedHandler(config).run(stop, observer);
        if (renderer_thread.joinable()) {
            renderer_thread.join();
        }
        return result;
    } catch (...) {
        stop.store(true, std::memory_order_release);
        if (renderer_thread.joinable()) {
            renderer_thread.join();
        }
        throw;
    }
}

} // namespace mockx::feed

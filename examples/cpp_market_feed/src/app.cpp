#include "mockx_feed/app.hpp"

#include "mockx_feed/decoder.hpp"
#include "mockx_feed/glimpse.hpp"
#include "mockx_feed/net.hpp"
#include "mockx_feed/order_book.hpp"
#include "mockx_feed/renderer.hpp"
#include "mockx_feed/spsc_ring.hpp"

#include <algorithm>
#include <charconv>
#include <chrono>
#include <cstdlib>
#include <exception>
#include <iostream>
#include <limits>
#include <mutex>
#include <optional>
#include <span>
#include <string_view>
#include <thread>
#include <vector>

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

class UdpReceiver {
public:
    UdpReceiver(
        DatagramRing& ring,
        const AppConfig& config,
        const std::atomic<bool>& stop)
        : ring_(ring), stop_(stop), socket_(
              config.itch_group, config.itch_port, config.interface_address),
          discard_(config.datagram_bytes) {}

    void run() noexcept {
        try {
            while (!stop_.load(std::memory_order_acquire)) {
                auto slot = ring_.try_reserve();
                if (!slot) {
                    const auto result = socket_.receive(discard_);
                    if (result.status != ReceiveStatus::timeout) {
                        ring_drops_.fetch_add(1, std::memory_order_relaxed);
                        continuity_epoch_.fetch_add(1, std::memory_order_release);
                    }
                    continue;
                }
                const auto result = socket_.receive(
                    std::span<std::byte>(slot->data, slot->capacity));
                if (result.status == ReceiveStatus::timeout) {
                    continue;
                }
                if (result.status == ReceiveStatus::truncated) {
                    ring_drops_.fetch_add(1, std::memory_order_relaxed);
                    continuity_epoch_.fetch_add(1, std::memory_order_release);
                    continue;
                }
                ring_.commit(*slot, result.length);
            }
        } catch (const std::exception& error) {
            {
                std::lock_guard lock(error_mutex_);
                error_ = error.what();
            }
            failed_.store(true, std::memory_order_release);
        }
    }

    [[nodiscard]] std::uint64_t continuity_epoch() const noexcept {
        return continuity_epoch_.load(std::memory_order_acquire);
    }
    [[nodiscard]] std::uint64_t ring_drops() const noexcept {
        return ring_drops_.load(std::memory_order_relaxed);
    }
    [[nodiscard]] bool failed() const noexcept {
        return failed_.load(std::memory_order_acquire);
    }
    [[nodiscard]] std::string error() const {
        std::lock_guard lock(error_mutex_);
        return error_;
    }

private:
    DatagramRing& ring_;
    const std::atomic<bool>& stop_;
    UdpMulticastSocket socket_;
    std::vector<std::byte> discard_;
    std::atomic<std::uint64_t> continuity_epoch_{0};
    std::atomic<std::uint64_t> ring_drops_{0};
    std::atomic<bool> failed_{false};
    mutable std::mutex error_mutex_;
    std::string error_;
};

class RecoveryRequired : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

void publish_display(
    ObjectRing<DisplaySnapshot>& display_ring,
    const OrderBook& book,
    const AppConfig& config,
    std::string state,
    const std::string& session,
    std::uint64_t expected_sequence,
    const FeedCounters& counters) {
    DisplaySnapshot snapshot;
    snapshot.connection_state = std::move(state);
    snapshot.mold_session = session;
    snapshot.next_sequence = expected_sequence;
    snapshot.counters = counters;
    snapshot.instruments = book.display(config.levels, config.symbols);
    (void)display_ring.try_push(std::move(snapshot));
}

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
    NetworkRuntime network;
    DatagramRing ingress(config.ring_capacity, config.datagram_bytes);
    ObjectRing<DisplaySnapshot> display_ring(8);
    UdpReceiver receiver(ingress, config, stop);
    std::thread receiver_thread;
    std::thread renderer_thread;
    try {
        receiver_thread = std::thread([&receiver] { receiver.run(); });
        renderer_thread = std::thread(
            [&display_ring, &stop] { renderer_loop(display_ring, stop); });
    } catch (...) {
        stop.store(true, std::memory_order_release);
        if (receiver_thread.joinable()) {
            receiver_thread.join();
        }
        throw;
    }

    auto finish = [&](int code) {
        stop.store(true, std::memory_order_release);
        if (receiver_thread.joinable()) {
            receiver_thread.join();
        }
        if (renderer_thread.joinable()) {
            renderer_thread.join();
        }
        return code;
    };

    GlimpseClient glimpse(GlimpseConfig{
        config.glimpse_host,
        config.glimpse_port,
        config.username,
        config.password});
    MessageDecoder decoder;
    OrderBook book;
    FeedCounters counters;
    std::string active_session;
    std::uint64_t expected_sequence = 1;
    std::uint64_t accepted_epoch = receiver.continuity_epoch();
    bool synchronized_once = false;
    bool recovery_needed = true;
    bool awaiting_stitch = false;
    std::uint32_t failed_stitches = 0;
    auto next_render = std::chrono::steady_clock::now();

    auto request_recovery = [&](std::string_view reason) {
        ++counters.gaps;
        if (awaiting_stitch) {
            ++failed_stitches;
        } else {
            failed_stitches = 0;
        }
        if (failed_stitches > config.resync_retries) {
            std::cerr << "GLIMPSE stitching retries exhausted after " << reason << '\n';
            return false;
        }
        recovery_needed = true;
        return true;
    };

    try {
        while (!stop.load(std::memory_order_acquire)) {
            if (receiver.failed()) {
                std::cerr << "UDP receiver failed: " << receiver.error() << '\n';
                return finish(4);
            }

            if (recovery_needed) {
                bool recovered = false;
                for (std::uint32_t attempt = 0;
                     attempt <= config.resync_retries && !stop.load(std::memory_order_acquire);
                     ++attempt) {
                    counters.ring_drops = receiver.ring_drops();
                    publish_display(
                        display_ring,
                        book,
                        config,
                        synchronized_once ? "RECOVERING" : "SNAPSHOTTING",
                        active_session,
                        expected_sequence,
                        counters);
                    const auto epoch_before = receiver.continuity_epoch();
                    try {
                        auto snapshot = glimpse.capture();
                        const auto epoch_after = receiver.continuity_epoch();
                        if (epoch_after != epoch_before) {
                            throw GlimpseUnavailable(
                                "ingress ring lost continuity during GLIMPSE capture");
                        }
                        book = std::move(snapshot.book);
                        expected_sequence = snapshot.next_itch_sequence;
                        accepted_epoch = epoch_after;
                        if (synchronized_once) {
                            ++counters.resyncs;
                        }
                        synchronized_once = true;
                        recovered = true;
                        recovery_needed = false;
                        awaiting_stitch = true;
                        break;
                    } catch (const AuthenticationError&) {
                        throw;
                    } catch (const UnsupportedMessage&) {
                        throw;
                    } catch (const GlimpseUnavailable& error) {
                        if (attempt == config.resync_retries) {
                            std::cerr << error.what() << '\n';
                            break;
                        }
                        const auto backoff = std::chrono::milliseconds(
                            100U << std::min<std::uint32_t>(attempt, 5));
                        std::this_thread::sleep_for(backoff);
                    } catch (const ProtocolError& error) {
                        if (attempt == config.resync_retries) {
                            std::cerr << "invalid GLIMPSE snapshot: " << error.what() << '\n';
                            break;
                        }
                    }
                }
                if (!recovered) {
                    if (stop.load(std::memory_order_acquire)) {
                        return finish(0);
                    }
                    return finish(5);
                }
                next_render = std::chrono::steady_clock::now();
            }

            if (receiver.continuity_epoch() != accepted_epoch) {
                accepted_epoch = receiver.continuity_epoch();
                if (!request_recovery("an ingress-ring continuity loss")) {
                    return finish(5);
                }
                continue;
            }

            auto slot = ingress.try_peek();
            if (!slot) {
                awaiting_stitch = false;
                failed_stitches = 0;
                const auto now = std::chrono::steady_clock::now();
                if (now >= next_render) {
                    counters.ring_drops = receiver.ring_drops();
                    publish_display(
                        display_ring,
                        book,
                        config,
                        "LIVE",
                        active_session,
                        expected_sequence,
                        counters);
                    next_render = now + std::chrono::milliseconds(config.refresh_ms);
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
                continue;
            }

            bool end_of_session = false;
            bool end_of_messages = false;
            try {
                auto packet = std::span<std::byte>(slot->data, slot->length);
                const auto header = inspect_mold_header(packet);
                if (!header.session.starts_with(config.session_prefix)) {
                    ingress.release(*slot);
                    continue;
                }
                if (active_session.empty()) {
                    active_session = header.session;
                } else if (active_session != header.session) {
                    active_session = header.session;
                    throw RecoveryRequired("new Mold session observed");
                }
                if (header.first_sequence > expected_sequence) {
                    throw RecoveryRequired("forward Mold sequence gap");
                }

                const auto info = parse_mold_packet(
                    packet,
                    [&](std::uint64_t sequence, std::span<std::byte> message) {
                        if (sequence < expected_sequence) {
                            return;
                        }
                        if (sequence != expected_sequence) {
                            throw RecoveryRequired("non-contiguous ITCH message sequence");
                        }
                        (void)decoder.apply(message, book, DecodeContext::live_itch);
                        end_of_messages = end_of_messages || is_end_of_messages(message);
                        ++expected_sequence;
                        ++counters.messages;
                    });
                ++counters.packets;
                if ((info.heartbeat() || info.end_of_session())
                    && info.first_sequence > expected_sequence) {
                    throw RecoveryRequired("Mold control packet exposes a sequence gap");
                }
                end_of_session = (
                    info.end_of_session() && info.first_sequence == expected_sequence)
                    || end_of_messages;
                ingress.release(*slot);
                awaiting_stitch = false;
                failed_stitches = 0;
            } catch (const UnsupportedMessage&) {
                ingress.release(*slot);
                throw;
            } catch (const RecoveryRequired& error) {
                ingress.release(*slot);
                if (!request_recovery(error.what())) {
                    return finish(5);
                }
                continue;
            } catch (const ProtocolError& error) {
                ingress.release(*slot);
                if (!request_recovery(error.what())) {
                    return finish(5);
                }
                continue;
            }

            if (end_of_session) {
                publish_display(
                    display_ring,
                    book,
                    config,
                    "END_OF_SESSION",
                    active_session,
                    expected_sequence,
                    counters);
                return finish(0);
            }
        }
        publish_display(
            display_ring,
            book,
            config,
            "STOPPED",
            active_session,
            expected_sequence,
            counters);
        return finish(0);
    } catch (const AuthenticationError& error) {
        std::cerr << error.what() << '\n';
        return finish(3);
    } catch (const UnsupportedMessage& error) {
        std::cerr << error.what() << '\n';
        return finish(5);
    } catch (const NetworkError& error) {
        std::cerr << error.what() << '\n';
        return finish(4);
    } catch (const std::exception& error) {
        std::cerr << "market feed failed: " << error.what() << '\n';
        return finish(5);
    }
}

} // namespace mockx::feed

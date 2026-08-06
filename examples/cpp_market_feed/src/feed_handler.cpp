#include "mockx_feed/feed_handler.hpp"

#include "mockx_feed/decoder.hpp"
#include "mockx_feed/glimpse.hpp"
#include "mockx_feed/net.hpp"
#include "mockx_feed/spsc_ring.hpp"

#include <algorithm>
#include <chrono>
#include <exception>
#include <iostream>
#include <mutex>
#include <span>
#include <stdexcept>
#include <thread>
#include <vector>

namespace mockx::feed {
namespace {

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

} // namespace

bool fill_depth_snapshot(
    DepthSnapshot& output,
    const FeedStatus& status,
    const OrderBook& book,
    std::string_view symbol,
    std::size_t depth) {
    if (depth == 0 || depth > max_depth_levels) {
        throw std::invalid_argument("depth must be between 1 and 16");
    }
    output = DepthSnapshot{};
    output.state = status.state;
    output.next_sequence = status.next_sequence;
    output.counters = status.counters;
    std::size_t bids = 0;
    std::size_t asks = 0;
    output.symbol_present = book.copy_depth(
        symbol,
        std::span<DisplayLevel>(output.bids).first(depth),
        bids,
        std::span<DisplayLevel>(output.asks).first(depth),
        asks,
        output.trading_state);
    output.bid_count = static_cast<std::uint8_t>(bids);
    output.ask_count = static_cast<std::uint8_t>(asks);
    return output.symbol_present;
}

int FeedHandler::run(std::atomic<bool>& stop, FeedObserver& observer) const {
    NetworkRuntime network;
    DatagramRing ingress(config_.ring_capacity, config_.datagram_bytes);
    UdpReceiver receiver(ingress, config_, stop);
    std::thread receiver_thread;
    try {
        receiver_thread = std::thread([&receiver] { receiver.run(); });
    } catch (...) {
        stop.store(true, std::memory_order_release);
        throw;
    }

    auto finish = [&](int code) {
        stop.store(true, std::memory_order_release);
        if (receiver_thread.joinable()) {
            receiver_thread.join();
        }
        return code;
    };

    GlimpseClient glimpse(GlimpseConfig{
        config_.glimpse_host,
        config_.glimpse_port,
        config_.username,
        config_.password});
    MessageDecoder decoder;
    OrderBook book;
    FeedCounters counters;
    std::string active_session;
    std::uint64_t expected_sequence = 1;
    std::uint64_t accepted_epoch = receiver.continuity_epoch();
    bool synchronized_once = false;
    bool recovery_needed = true;
    bool awaiting_stitch = false;
    bool live_published_since_change = false;
    std::uint32_t failed_stitches = 0;

    auto publish = [&](FeedState state) {
        counters.ring_drops = receiver.ring_drops();
        return observer.publish(
            FeedStatus{state, active_session, expected_sequence, counters}, book);
    };

    auto request_recovery = [&](std::string_view reason) {
        ++counters.gaps;
        if (awaiting_stitch) {
            ++failed_stitches;
        } else {
            failed_stitches = 0;
        }
        if (failed_stitches > config_.resync_retries) {
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
                     attempt <= config_.resync_retries
                     && !stop.load(std::memory_order_acquire);
                     ++attempt) {
                    if (!publish(
                            synchronized_once ? FeedState::recovering
                                              : FeedState::snapshotting)) {
                        std::cerr << "feed observer rejected a snapshot\n";
                        return finish(5);
                    }
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
                        live_published_since_change = false;
                        break;
                    } catch (const AuthenticationError&) {
                        throw;
                    } catch (const UnsupportedMessage&) {
                        throw;
                    } catch (const GlimpseUnavailable& error) {
                        if (attempt == config_.resync_retries) {
                            std::cerr << error.what() << '\n';
                            break;
                        }
                        const auto backoff = std::chrono::milliseconds(
                            100U << std::min<std::uint32_t>(attempt, 5));
                        std::this_thread::sleep_for(backoff);
                    } catch (const ProtocolError& error) {
                        if (attempt == config_.resync_retries) {
                            std::cerr << "invalid GLIMPSE snapshot: " << error.what()
                                      << '\n';
                            break;
                        }
                    }
                }
                if (!recovered) {
                    return finish(stop.load(std::memory_order_acquire) ? 0 : 5);
                }
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
                if (!live_published_since_change && !publish(FeedState::live)) {
                    std::cerr << "feed observer rejected a snapshot\n";
                    return finish(5);
                }
                live_published_since_change = true;
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
                continue;
            }

            bool end_of_session = false;
            bool end_of_messages = false;
            try {
                auto packet = std::span<std::byte>(slot->data, slot->length);
                const auto header = inspect_mold_header(packet);
                if (!header.session.starts_with(config_.session_prefix)) {
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
                            throw RecoveryRequired(
                                "non-contiguous ITCH message sequence");
                        }
                        (void)decoder.apply(message, book, DecodeContext::live_itch);
                        end_of_messages = end_of_messages || is_end_of_messages(message);
                        ++expected_sequence;
                        ++counters.messages;
                    });
                ++counters.packets;
                if ((info.heartbeat() || info.end_of_session())
                    && info.first_sequence > expected_sequence) {
                    throw RecoveryRequired(
                        "Mold control packet exposes a sequence gap");
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
                if (!publish(FeedState::end_of_session)) {
                    std::cerr << "feed observer rejected a snapshot\n";
                    return finish(5);
                }
                return finish(0);
            }
            if (!publish(FeedState::live)) {
                std::cerr << "feed observer rejected a snapshot\n";
                return finish(5);
            }
            live_published_since_change = true;
        }
        (void)publish(FeedState::stopped);
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

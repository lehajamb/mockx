#include "mockx_sor/app.hpp"

#include "mockx_feed/feed_handler.hpp"
#include "mockx_feed/spsc_ring.hpp"
#include "mockx_sor/config.hpp"
#include "mockx_sor/fix_gateway.hpp"

#include <quickfix/FileLog.h>
#include <quickfix/FileStore.h>
#include <quickfix/ThreadedSocketInitiator.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cctype>
#include <iostream>
#include <limits>
#include <set>
#include <stdexcept>
#include <string_view>
#include <thread>
#include <utility>

namespace mockx::sor {
namespace {

template <typename T>
T parse_unsigned(std::string_view text, std::string_view option) {
    std::uint64_t value = 0;
    const auto [end, error] = std::from_chars(
        text.data(), text.data() + text.size(), value);
    if (error != std::errc{} || end != text.data() + text.size()
        || value == 0 || value > std::numeric_limits<T>::max()) {
        throw CommandLineError(std::string(option) + " requires a positive integer");
    }
    return static_cast<T>(value);
}

std::string require_value(int& index, int argc, char** argv, std::string_view option) {
    if (++index >= argc) {
        throw CommandLineError(std::string(option) + " requires a value");
    }
    return argv[index];
}

bool visible_ascii(std::string_view value) {
    return !value.empty() && std::all_of(value.begin(), value.end(), [](unsigned char ch) {
        return ch >= 0x21U && ch <= 0x7eU;
    });
}

class DepthObserver final : public mockx::feed::FeedObserver {
public:
    DepthObserver(
        mockx::feed::ObjectRing<mockx::feed::DepthSnapshot>& ring,
        std::string symbol,
        std::size_t depth)
        : ring_(ring), symbol_(std::move(symbol)), depth_(depth) {}

    [[nodiscard]] bool publish(
        const mockx::feed::FeedStatus& status,
        const mockx::feed::OrderBook& book) override {
        if (status.counters.ring_drops != 0 || status.counters.gaps != 0) {
            continuity_lost_.store(true, std::memory_order_release);
            return false;
        }
        auto slot = ring_.try_reserve();
        if (!slot) {
            ring_saturated_.store(true, std::memory_order_release);
            return false;
        }
        (void)mockx::feed::fill_depth_snapshot(
            *slot->value, status, book, symbol_, depth_);
        ring_.commit(*slot);
        return true;
    }

    [[nodiscard]] bool ring_saturated() const noexcept {
        return ring_saturated_.load(std::memory_order_acquire);
    }

    [[nodiscard]] bool continuity_lost() const noexcept {
        return continuity_lost_.load(std::memory_order_acquire);
    }

private:
    mockx::feed::ObjectRing<mockx::feed::DepthSnapshot>& ring_;
    std::string symbol_;
    std::size_t depth_;
    std::atomic<bool> ring_saturated_{false};
    std::atomic<bool> continuity_lost_{false};
};

std::string fixed_string(const auto& value) {
    return std::string(value.data());
}

struct ChildTracker {
    const ChildOrder* child{};
    bool terminal{};
    bool rejected{};
    bool canceled{};
    std::uint32_t terminal_cum_lots{};
    std::uint32_t trade_lots{};
    std::uint64_t price4_lot_notional{};
    std::set<std::string> exec_ids;
};

void print_plan(
    const ParentOrder& parent,
    const RoutePlan& plan,
    const std::vector<VenueDefinition>& venues) {
    std::cout << "PARENT id=" << parent.parent_id << " side="
              << (parent.side == ParentSide::buy ? "BUY" : "SELL")
              << " symbol=" << parent.symbol
              << " requested_lots=" << parent.quantity_lots
              << " limit=" << format_price4(parent.limit_price4) << '\n';
    for (std::uint8_t index = 0; index < plan.child_count; ++index) {
        const auto& child = plan.children[index];
        std::cout << "CHILD cl_ord_id=" << child.cl_ord_id
                  << " venue=" << venues[child.venue_index].venue_id
                  << " quantity_lots=" << child.quantity_lots
                  << " limit=" << format_price4(child.limit_price4) << '\n';
    }
}

} // namespace

CommandLine parse_command_line(int argc, char** argv) {
    CommandLine result;
    bool side_set = false;
    bool quantity_set = false;
    bool lot_size_set = false;
    bool limit_set = false;
    for (int index = 1; index < argc; ++index) {
        const std::string_view option(argv[index]);
        if (option == "--help" || option == "-h") {
            result.help = true;
        } else if (option == "--version") {
            result.version = true;
        } else if (option == "--config") {
            result.config_path = require_value(index, argc, argv, option);
        } else if (option == "--parent-id") {
            result.parent.parent_id = require_value(index, argc, argv, option);
        } else if (option == "--symbol") {
            result.parent.symbol = require_value(index, argc, argv, option);
        } else if (option == "--side") {
            const auto side = require_value(index, argc, argv, option);
            if (side == "BUY") {
                result.parent.side = ParentSide::buy;
            } else if (side == "SELL") {
                result.parent.side = ParentSide::sell;
            } else {
                throw CommandLineError("--side must be BUY or SELL");
            }
            side_set = true;
        } else if (option == "--quantity-lots") {
            result.parent.quantity_lots = parse_unsigned<std::uint32_t>(
                require_value(index, argc, argv, option), option);
            quantity_set = true;
        } else if (option == "--lot-size") {
            result.lot_size = parse_positive_decimal(
                require_value(index, argc, argv, option));
            lot_size_set = true;
        } else if (option == "--limit-price") {
            result.parent.limit_price4 = parse_price4(
                require_value(index, argc, argv, option));
            limit_set = true;
        } else if (option == "--depth") {
            result.depth = parse_unsigned<std::size_t>(
                require_value(index, argc, argv, option), option);
        } else if (option == "--sync-timeout-ms") {
            result.sync_timeout_ms = parse_unsigned<std::uint32_t>(
                require_value(index, argc, argv, option), option);
        } else if (option == "--report-timeout-ms") {
            result.report_timeout_ms = parse_unsigned<std::uint32_t>(
                require_value(index, argc, argv, option), option);
        } else {
            throw CommandLineError("unknown option: " + std::string(option));
        }
    }

    if (result.help || result.version) {
        return result;
    }
    if (result.config_path.empty() || result.parent.parent_id.empty()
        || result.parent.symbol.empty() || !side_set || !quantity_set
        || !lot_size_set || !limit_set) {
        throw CommandLineError("all required parent and configuration options must be provided");
    }
    if (!visible_ascii(result.parent.parent_id) || result.parent.parent_id.size() > 48) {
        throw CommandLineError("--parent-id must contain 1-48 visible ASCII bytes");
    }
    if (!visible_ascii(result.parent.symbol) || result.parent.symbol.size() > 8) {
        throw CommandLineError("--symbol must contain 1-8 visible ASCII bytes");
    }
    if (result.depth == 0 || result.depth > mockx::feed::max_depth_levels) {
        throw CommandLineError("--depth must be between 1 and 16");
    }
    return result;
}

std::string usage() {
    return R"(mockx_sor 0.1.0

Usage: mockx_sor --config PATH --parent-id ID --symbol SYMBOL
                 --side BUY|SELL --quantity-lots N --lot-size DECIMAL
                 --limit-price DECIMAL [options]

  --config PATH             Two-session QuickFIX/SOR configuration
  --parent-id ID            Visible ASCII parent identifier (max 48 bytes)
  --symbol SYMBOL           One ITCH/FIX symbol (max 8 bytes)
  --side BUY|SELL           Parent side
  --quantity-lots N         Positive ITCH share/internal-lot count
  --lot-size DECIMAL        Exact MockX instrument lot size
  --limit-price DECIMAL     Exact parent limit with at most four decimals
  --depth N                 Levels per venue, 1-16 (16)
  --sync-timeout-ms MS      Feed and FIX readiness timeout (5000)
  --report-timeout-ms MS    Terminal-report timeout (5000)
  --help                    Show this help
  --version                 Show the version
)";
}

int run_sor(const CommandLine& command) {
    LoadedConfig loaded(command.config_path);
    for (auto& venue : loaded.venues) {
        venue.feed.symbols = {command.parent.symbol};
    }

    std::array<mockx::feed::ObjectRing<mockx::feed::DepthSnapshot>, venue_count>
        feed_rings{mockx::feed::ObjectRing<mockx::feed::DepthSnapshot>(1024),
                   mockx::feed::ObjectRing<mockx::feed::DepthSnapshot>(1024)};
    std::array<mockx::feed::ObjectRing<ExecutionEvent>, venue_count> report_rings{
        mockx::feed::ObjectRing<ExecutionEvent>(64),
        mockx::feed::ObjectRing<ExecutionEvent>(64)};
    FixApplication application(
        loaded.venues,
        command.lot_size,
        {&report_rings[0], &report_rings[1]});
    FIX::FileStoreFactory store_factory(loaded.fix_settings);
    FIX::FileLogFactory log_factory(loaded.fix_settings);
    FIX::ThreadedSocketInitiator initiator(
        application, store_factory, loaded.fix_settings, log_factory);

    std::atomic<bool> stop{false};
    std::array<std::atomic<int>, venue_count> feed_codes;
    for (auto& code : feed_codes) {
        code.store(-1, std::memory_order_relaxed);
    }
    std::array<std::thread, venue_count> feed_threads;
    std::array<DepthObserver, venue_count> observers{
        DepthObserver(feed_rings[0], command.parent.symbol, command.depth),
        DepthObserver(feed_rings[1], command.parent.symbol, command.depth)};
    try {
        for (std::size_t index = 0; index < venue_count; ++index) {
            feed_threads[index] = std::thread([&, index] {
                try {
                    const auto code = mockx::feed::FeedHandler(
                                          loaded.venues[index].feed)
                                          .run(stop, observers[index]);
                    feed_codes[index].store(code, std::memory_order_release);
                } catch (const std::exception& error) {
                    std::cerr << "venue " << loaded.venues[index].venue_id
                              << " feed failed: " << error.what() << '\n';
                    feed_codes[index].store(5, std::memory_order_release);
                    stop.store(true, std::memory_order_release);
                } catch (...) {
                    std::cerr << "venue " << loaded.venues[index].venue_id
                              << " feed failed with an unknown exception\n";
                    feed_codes[index].store(5, std::memory_order_release);
                    stop.store(true, std::memory_order_release);
                }
            });
        }
    } catch (...) {
        stop.store(true, std::memory_order_release);
        for (auto& thread : feed_threads) {
            if (thread.joinable()) {
                thread.join();
            }
        }
        throw;
    }

    bool initiator_started = false;

    auto stop_feeds = [&] {
        stop.store(true, std::memory_order_release);
        for (auto& thread : feed_threads) {
            if (thread.joinable()) {
                thread.join();
            }
        }
    };

    auto feed_failure_exit = [&] {
        if (observers[0].ring_saturated() || observers[1].ring_saturated()) {
            return 5;
        }
        for (const auto& code : feed_codes) {
            if (code.load(std::memory_order_acquire) > 0) {
                return 3;
            }
        }
        return 0;
    };

    auto shutdown = [&] {
        if (initiator_started) {
            initiator.stop();
            initiator_started = false;
        }
        stop_feeds();
    };

    try {
        initiator.start();
        initiator_started = true;
        std::array<mockx::feed::DepthSnapshot, venue_count> latest{};
        std::array<bool, venue_count> have_snapshot{};
        const auto sync_deadline = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(command.sync_timeout_ms);
        bool ready = false;
        while (std::chrono::steady_clock::now() < sync_deadline) {
            for (std::size_t index = 0; index < venue_count; ++index) {
                mockx::feed::DepthSnapshot update;
                while (feed_rings[index].try_pop(update)) {
                    latest[index] = update;
                    have_snapshot[index] = true;
                }
            }
            if (application.failed()) {
                std::cerr << application.error() << '\n';
                shutdown();
                return application.ring_overflowed() ? 5 : 4;
            }
            if (stop.load(std::memory_order_acquire)) {
                const auto ring_saturated = observers[0].ring_saturated()
                    || observers[1].ring_saturated();
                if (observers[0].continuity_lost()
                    || observers[1].continuity_lost()) {
                    std::cerr << "venue feed continuity was lost\n";
                }
                shutdown();
                return ring_saturated ? 5 : 3;
            }
            ready = application.all_logged_on();
            for (std::size_t index = 0; index < venue_count; ++index) {
                ready = ready && have_snapshot[index]
                    && latest[index].state == mockx::feed::FeedState::live
                    && latest[index].symbol_present;
            }
            if (ready) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        if (!ready) {
            std::cerr << "both venue feeds and FIX sessions did not become ready\n";
            shutdown();
            return application.all_logged_on() ? 3 : 4;
        }

        std::array<VenueSnapshot, venue_count> venue_books;
        for (std::size_t index = 0; index < venue_count; ++index) {
            venue_books[index] = VenueSnapshot{
                loaded.venues[index].venue_id,
                loaded.venues[index].priority,
                latest[index]};
        }
        const auto plan = plan_route(command.parent, venue_books);
        stop_feeds();
        if (const auto feed_exit = feed_failure_exit(); feed_exit != 0) {
            std::cerr << "a venue feed failed at the routing decision boundary\n";
            shutdown();
            return feed_exit;
        }
        print_plan(command.parent, plan, loaded.venues);
        if (plan.child_count == 0) {
            std::cout << "RESULT requested_lots=" << command.parent.quantity_lots
                      << " planned_lots=0 initial_unrouted_lots="
                      << plan.initial_unrouted_lots
                      << " filled_lots=0 ioc_canceled_lots=0 rejected_lots=0"
                         " total_remainder_lots="
                      << command.parent.quantity_lots
                      << " notional=" << format_notional(0, command.lot_size)
                      << " notional_price4_lots=0 vwap=N/A\n";
            shutdown();
            return 0;
        }

        std::array<ChildTracker, venue_count> trackers{};
        for (std::uint8_t index = 0; index < plan.child_count; ++index) {
            trackers[index].child = &plan.children[index];
            if (!application.send_child(plan.children[index], command.parent)) {
                std::cerr << application.error() << '\n';
                shutdown();
                return 4;
            }
        }

        const auto report_deadline = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(command.report_timeout_ms);
        std::uint8_t terminal_children = 0;
        bool invariant_failure = false;
        while (terminal_children < plan.child_count
               && std::chrono::steady_clock::now() < report_deadline) {
            if (application.failed()) {
                std::cerr << application.error() << '\n';
                shutdown();
                return application.ring_overflowed() ? 5 : 4;
            }
            for (std::size_t venue = 0; venue < venue_count; ++venue) {
                ExecutionEvent event;
                while (report_rings[venue].try_pop(event)) {
                    ChildTracker* tracker = nullptr;
                    const auto cl_ord_id = fixed_string(event.cl_ord_id);
                    for (std::uint8_t index = 0; index < plan.child_count; ++index) {
                        if (trackers[index].child->cl_ord_id == cl_ord_id) {
                            tracker = &trackers[index];
                            break;
                        }
                    }
                    if (tracker == nullptr) {
                        invariant_failure = true;
                        continue;
                    }
                    const auto exec_id = fixed_string(event.exec_id);
                    if (!tracker->exec_ids.insert(exec_id).second) {
                        continue;
                    }
                    if (event.has_last_fill) {
                        if (tracker->trade_lots
                            > std::numeric_limits<std::uint32_t>::max()
                                - event.last_qty_lots) {
                            invariant_failure = true;
                            continue;
                        }
                        tracker->trade_lots += event.last_qty_lots;
                        const auto fill_notional =
                            static_cast<std::uint64_t>(event.last_price4)
                            * event.last_qty_lots;
                        if (tracker->price4_lot_notional
                            > std::numeric_limits<std::uint64_t>::max()
                                - fill_notional) {
                            invariant_failure = true;
                            continue;
                        }
                        tracker->price4_lot_notional += fill_notional;
                    }
                    if (event.terminal && !tracker->terminal) {
                        tracker->terminal = true;
                        tracker->terminal_cum_lots = event.cum_qty_lots;
                        tracker->rejected = event.ord_status == '8';
                        tracker->canceled = event.ord_status == '4';
                        ++terminal_children;
                    }
                }
            }
            if (invariant_failure) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }

        if (invariant_failure) {
            std::cerr << "execution-report correlation invariant failed\n";
            shutdown();
            return 5;
        }
        if (terminal_children != plan.child_count) {
            std::cerr << "timed out waiting for terminal child execution reports\n";
            shutdown();
            return 4;
        }

        std::uint32_t filled = 0;
        std::uint32_t canceled = 0;
        std::uint32_t rejected = 0;
        std::uint64_t notional = 0;
        bool any_rejected = false;
        for (std::uint8_t index = 0; index < plan.child_count; ++index) {
            const auto& tracker = trackers[index];
            if (tracker.trade_lots != tracker.terminal_cum_lots
                || tracker.terminal_cum_lots > tracker.child->quantity_lots) {
                std::cerr << "terminal CumQty disagrees with unique trade reports\n";
                shutdown();
                return 5;
            }
            if (!tracker.rejected && !tracker.canceled
                && tracker.terminal_cum_lots != tracker.child->quantity_lots) {
                std::cerr << "FILLED child has an incomplete terminal CumQty\n";
                shutdown();
                return 5;
            }
            if (filled > std::numeric_limits<std::uint32_t>::max()
                    - tracker.terminal_cum_lots
                || notional > std::numeric_limits<std::uint64_t>::max()
                    - tracker.price4_lot_notional) {
                std::cerr << "execution aggregate overflow\n";
                shutdown();
                return 5;
            }
            filled += tracker.terminal_cum_lots;
            notional += tracker.price4_lot_notional;
            const auto unfilled = tracker.child->quantity_lots - tracker.terminal_cum_lots;
            if (tracker.rejected) {
                rejected += unfilled;
                any_rejected = true;
            } else if (tracker.canceled) {
                canceled += unfilled;
            }
        }
        if (filled > command.parent.quantity_lots) {
            std::cerr << "child fills exceed the parent quantity\n";
            shutdown();
            return 5;
        }
        const auto accounted = static_cast<std::uint64_t>(filled)
            + canceled + rejected + plan.initial_unrouted_lots;
        if (accounted != command.parent.quantity_lots) {
            std::cerr << "parent quantity accounting invariant failed\n";
            shutdown();
            return 5;
        }
        const auto total_remainder = command.parent.quantity_lots - filled;
        std::cout << "RESULT requested_lots=" << command.parent.quantity_lots
                  << " planned_lots=" << plan.planned_lots
                  << " initial_unrouted_lots=" << plan.initial_unrouted_lots
                  << " filled_lots=" << filled
                  << " ioc_canceled_lots=" << canceled
                  << " rejected_lots=" << rejected
                  << " total_remainder_lots=" << total_remainder
                  << " notional=" << format_notional(notional, command.lot_size)
                  << " notional_price4_lots=" << notional
                  << " vwap=" << format_vwap4(notional, filled) << '\n';
        shutdown();
        return any_rejected ? 6 : 0;
    } catch (...) {
        shutdown();
        throw;
    }
}

} // namespace mockx::sor

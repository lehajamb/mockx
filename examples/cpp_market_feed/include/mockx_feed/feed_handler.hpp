#pragma once

#include "mockx_feed/app.hpp"
#include "mockx_feed/order_book.hpp"

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string_view>
#include <utility>

namespace mockx::feed {

inline constexpr std::size_t max_depth_levels = 16;

enum class FeedState : std::uint8_t {
    snapshotting,
    recovering,
    live,
    end_of_session,
    stopped,
};

struct FeedStatus {
    FeedState state{FeedState::snapshotting};
    std::string_view mold_session;
    std::uint64_t next_sequence{1};
    FeedCounters counters;
};

struct DepthSnapshot {
    FeedState state{FeedState::snapshotting};
    std::uint64_t next_sequence{1};
    FeedCounters counters;
    char trading_state{'?'};
    bool symbol_present{};
    std::uint8_t bid_count{};
    std::uint8_t ask_count{};
    std::array<DisplayLevel, max_depth_levels> bids{};
    std::array<DisplayLevel, max_depth_levels> asks{};
};

class FeedObserver {
public:
    virtual ~FeedObserver() = default;
    [[nodiscard]] virtual bool publish(
        const FeedStatus& status,
        const OrderBook& book) = 0;
};

class FeedHandler {
public:
    explicit FeedHandler(AppConfig config) : config_(std::move(config)) {}

    [[nodiscard]] int run(std::atomic<bool>& stop, FeedObserver& observer) const;

private:
    AppConfig config_;
};

[[nodiscard]] bool fill_depth_snapshot(
    DepthSnapshot& output,
    const FeedStatus& status,
    const OrderBook& book,
    std::string_view symbol,
    std::size_t depth);

} // namespace mockx::feed

#pragma once

#include "mockx_feed/protocol.hpp"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <list>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

namespace mockx::feed {

enum class Side : char { buy = 'B', sell = 'S' };

struct DisplayLevel {
    std::uint32_t price{};
    std::uint64_t shares{};
    std::size_t order_count{};
};

struct DisplayInstrument {
    std::string symbol;
    char trading_state{'?'};
    std::vector<DisplayLevel> bids;
    std::vector<DisplayLevel> asks;
};

struct FeedCounters {
    std::uint64_t packets{};
    std::uint64_t messages{};
    std::uint64_t ring_drops{};
    std::uint64_t gaps{};
    std::uint64_t resyncs{};
};

struct DisplaySnapshot {
    std::string connection_state;
    std::string mold_session;
    std::uint64_t next_sequence{1};
    FeedCounters counters;
    std::vector<DisplayInstrument> instruments;
};

class OrderBook {
public:
    OrderBook() = default;
    OrderBook(const OrderBook&) = delete;
    OrderBook& operator=(const OrderBook&) = delete;
    OrderBook(OrderBook&&) noexcept = default;
    OrderBook& operator=(OrderBook&&) noexcept = default;

    void clear();
    void register_instrument(std::uint16_t locate, std::string symbol);
    void set_trading_state(std::uint16_t locate, std::string_view symbol, char state);
    void add_order(
        std::uint64_t order_reference,
        std::uint16_t locate,
        std::string_view symbol,
        Side side,
        std::uint32_t shares,
        std::uint32_t price);
    void execute_order(
        std::uint64_t order_reference,
        std::uint16_t locate,
        std::uint32_t executed_shares);
    void delete_order(std::uint64_t order_reference, std::uint16_t locate);

    [[nodiscard]] std::size_t order_count() const noexcept { return orders_.size(); }
    [[nodiscard]] std::size_t instrument_count() const noexcept {
        return instruments_.size();
    }
    [[nodiscard]] bool contains(std::uint64_t order_reference) const noexcept;
    [[nodiscard]] std::uint32_t leaves(std::uint64_t order_reference) const;
    [[nodiscard]] std::vector<DisplayInstrument> display(
        std::size_t levels,
        const std::set<std::string>& symbols) const;

private:
    struct Level {
        std::uint64_t shares{};
        std::list<std::uint64_t> fifo;
    };

    struct Instrument {
        std::string symbol;
        char trading_state{'?'};
        std::map<std::uint32_t, Level, std::greater<>> bids;
        std::map<std::uint32_t, Level, std::less<>> asks;
    };

    using FifoIterator = std::list<std::uint64_t>::iterator;

    struct Order {
        std::uint16_t locate{};
        Side side{Side::buy};
        std::uint32_t shares{};
        std::uint32_t price{};
        FifoIterator fifo_position;
    };

    Instrument& require_instrument(std::uint16_t locate);
    const Instrument& require_instrument(std::uint16_t locate) const;
    void erase_order(std::unordered_map<std::uint64_t, Order>::iterator order);

    std::map<std::uint16_t, Instrument> instruments_;
    std::unordered_map<std::string, std::uint16_t> locate_by_symbol_;
    std::unordered_map<std::uint64_t, Order> orders_;
};

} // namespace mockx::feed

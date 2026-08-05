#include "mockx_feed/order_book.hpp"

#include <algorithm>
#include <iterator>
#include <limits>
#include <utility>

namespace mockx::feed {

void OrderBook::clear() {
    orders_.clear();
    locate_by_symbol_.clear();
    instruments_.clear();
}

void OrderBook::register_instrument(std::uint16_t locate, std::string symbol) {
    if (locate == 0 || symbol.empty()) {
        throw ProtocolError("instrument locate and symbol must be non-empty");
    }
    if (const auto existing = instruments_.find(locate); existing != instruments_.end()) {
        if (existing->second.symbol != symbol) {
            throw ProtocolError("stock locate maps to conflicting symbols");
        }
        return;
    }
    if (const auto existing = locate_by_symbol_.find(symbol);
        existing != locate_by_symbol_.end() && existing->second != locate) {
        throw ProtocolError("symbol maps to conflicting stock locates");
    }
    locate_by_symbol_.emplace(symbol, locate);
    instruments_.emplace(locate, Instrument{std::move(symbol)});
}

OrderBook::Instrument& OrderBook::require_instrument(std::uint16_t locate) {
    const auto found = instruments_.find(locate);
    if (found == instruments_.end()) {
        throw ProtocolError("message references an unknown stock locate");
    }
    return found->second;
}

const OrderBook::Instrument& OrderBook::require_instrument(std::uint16_t locate) const {
    const auto found = instruments_.find(locate);
    if (found == instruments_.end()) {
        throw ProtocolError("message references an unknown stock locate");
    }
    return found->second;
}

void OrderBook::set_trading_state(
    std::uint16_t locate, std::string_view symbol, char state) {
    auto& instrument = require_instrument(locate);
    if (instrument.symbol != symbol) {
        throw ProtocolError("trading-action symbol disagrees with stock locate");
    }
    instrument.trading_state = state;
}

void OrderBook::add_order(
    std::uint64_t order_reference,
    std::uint16_t locate,
    std::string_view symbol,
    Side side,
    std::uint32_t shares,
    std::uint32_t price) {
    if (order_reference == 0 || shares == 0 || price == 0) {
        throw ProtocolError("resting order fields must be positive");
    }
    if (orders_.contains(order_reference)) {
        throw ProtocolError("duplicate order reference");
    }
    auto& instrument = require_instrument(locate);
    if (instrument.symbol != symbol) {
        throw ProtocolError("add-order symbol disagrees with stock locate");
    }

    if (side == Side::buy) {
        auto [level, inserted] = instrument.bids.try_emplace(price);
        (void)inserted;
        if (level->second.shares > std::numeric_limits<std::uint64_t>::max() - shares) {
            throw ProtocolError("bid level aggregate overflows uint64");
        }
        level->second.fifo.push_back(order_reference);
        const auto position = std::prev(level->second.fifo.end());
        try {
            orders_.emplace(
                order_reference, Order{locate, side, shares, price, position});
        } catch (...) {
            level->second.fifo.pop_back();
            if (level->second.fifo.empty()) {
                instrument.bids.erase(level);
            }
            throw;
        }
        level->second.shares += shares;
    } else {
        auto [level, inserted] = instrument.asks.try_emplace(price);
        (void)inserted;
        if (level->second.shares > std::numeric_limits<std::uint64_t>::max() - shares) {
            throw ProtocolError("ask level aggregate overflows uint64");
        }
        level->second.fifo.push_back(order_reference);
        const auto position = std::prev(level->second.fifo.end());
        try {
            orders_.emplace(
                order_reference, Order{locate, side, shares, price, position});
        } catch (...) {
            level->second.fifo.pop_back();
            if (level->second.fifo.empty()) {
                instrument.asks.erase(level);
            }
            throw;
        }
        level->second.shares += shares;
    }
}

void OrderBook::execute_order(
    std::uint64_t order_reference,
    std::uint16_t locate,
    std::uint32_t executed_shares) {
    if (executed_shares == 0) {
        throw ProtocolError("executed shares must be positive");
    }
    auto found = orders_.find(order_reference);
    if (found == orders_.end()) {
        throw ProtocolError("execution references an unknown order");
    }
    auto& order = found->second;
    if (order.locate != locate) {
        throw ProtocolError("execution stock locate disagrees with order");
    }
    if (executed_shares > order.shares) {
        throw ProtocolError("execution quantity exceeds order leaves");
    }
    auto& instrument = require_instrument(order.locate);
    if (order.side == Side::buy) {
        auto level = instrument.bids.find(order.price);
        if (level == instrument.bids.end() || level->second.shares < executed_shares) {
            throw ProtocolError("bid level aggregate is inconsistent");
        }
        level->second.shares -= executed_shares;
    } else {
        auto level = instrument.asks.find(order.price);
        if (level == instrument.asks.end() || level->second.shares < executed_shares) {
            throw ProtocolError("ask level aggregate is inconsistent");
        }
        level->second.shares -= executed_shares;
    }
    order.shares -= executed_shares;
    if (order.shares == 0) {
        erase_order(found);
    }
}

void OrderBook::delete_order(std::uint64_t order_reference, std::uint16_t locate) {
    auto found = orders_.find(order_reference);
    if (found == orders_.end()) {
        throw ProtocolError("delete references an unknown order");
    }
    if (found->second.locate != locate) {
        throw ProtocolError("delete stock locate disagrees with order");
    }
    auto& instrument = require_instrument(locate);
    if (found->second.side == Side::buy) {
        auto level = instrument.bids.find(found->second.price);
        if (level == instrument.bids.end() || level->second.shares < found->second.shares) {
            throw ProtocolError("bid level aggregate is inconsistent");
        }
        level->second.shares -= found->second.shares;
    } else {
        auto level = instrument.asks.find(found->second.price);
        if (level == instrument.asks.end() || level->second.shares < found->second.shares) {
            throw ProtocolError("ask level aggregate is inconsistent");
        }
        level->second.shares -= found->second.shares;
    }
    erase_order(found);
}

void OrderBook::erase_order(std::unordered_map<std::uint64_t, Order>::iterator found) {
    auto& order = found->second;
    auto& instrument = require_instrument(order.locate);
    if (order.side == Side::buy) {
        auto level = instrument.bids.find(order.price);
        if (level == instrument.bids.end()) {
            throw ProtocolError("bid level disappeared before order removal");
        }
        level->second.fifo.erase(order.fifo_position);
        if (level->second.fifo.empty()) {
            if (level->second.shares != 0) {
                throw ProtocolError("empty bid level has nonzero aggregate");
            }
            instrument.bids.erase(level);
        }
    } else {
        auto level = instrument.asks.find(order.price);
        if (level == instrument.asks.end()) {
            throw ProtocolError("ask level disappeared before order removal");
        }
        level->second.fifo.erase(order.fifo_position);
        if (level->second.fifo.empty()) {
            if (level->second.shares != 0) {
                throw ProtocolError("empty ask level has nonzero aggregate");
            }
            instrument.asks.erase(level);
        }
    }
    orders_.erase(found);
}

bool OrderBook::contains(std::uint64_t order_reference) const noexcept {
    return orders_.contains(order_reference);
}

std::uint32_t OrderBook::leaves(std::uint64_t order_reference) const {
    const auto found = orders_.find(order_reference);
    if (found == orders_.end()) {
        throw ProtocolError("unknown order reference");
    }
    return found->second.shares;
}

std::vector<DisplayInstrument> OrderBook::display(
    std::size_t levels, const std::set<std::string>& symbols) const {
    std::vector<DisplayInstrument> result;
    result.reserve(instruments_.size());
    for (const auto& [locate, instrument] : instruments_) {
        (void)locate;
        if (!symbols.empty() && !symbols.contains(instrument.symbol)) {
            continue;
        }
        DisplayInstrument display{instrument.symbol, instrument.trading_state};
        display.bids.reserve(std::min(levels, instrument.bids.size()));
        display.asks.reserve(std::min(levels, instrument.asks.size()));
        for (const auto& [price, level] : instrument.bids) {
            if (display.bids.size() == levels) {
                break;
            }
            display.bids.push_back({price, level.shares, level.fifo.size()});
        }
        for (const auto& [price, level] : instrument.asks) {
            if (display.asks.size() == levels) {
                break;
            }
            display.asks.push_back({price, level.shares, level.fifo.size()});
        }
        result.push_back(std::move(display));
    }
    return result;
}

bool OrderBook::copy_depth(
    std::string_view symbol,
    std::span<DisplayLevel> bids,
    std::size_t& bid_count,
    std::span<DisplayLevel> asks,
    std::size_t& ask_count,
    char& trading_state) const {
    bid_count = 0;
    ask_count = 0;
    const auto locate = locate_by_symbol_.find(symbol);
    if (locate == locate_by_symbol_.end()) {
        trading_state = '?';
        return false;
    }
    const auto& instrument = require_instrument(locate->second);
    trading_state = instrument.trading_state;
    for (const auto& [price, level] : instrument.bids) {
        if (bid_count == bids.size()) {
            break;
        }
        bids[bid_count++] = DisplayLevel{price, level.shares, level.fifo.size()};
    }
    for (const auto& [price, level] : instrument.asks) {
        if (ask_count == asks.size()) {
            break;
        }
        asks[ask_count++] = DisplayLevel{price, level.shares, level.fifo.size()};
    }
    return true;
}

} // namespace mockx::feed

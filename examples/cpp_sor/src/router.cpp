#include "mockx_sor/router.hpp"

#include <algorithm>
#include <limits>

namespace mockx::sor {
namespace {

const mockx::feed::DisplayLevel* current_level(
    const ParentOrder& parent,
    const VenueSnapshot& venue,
    std::size_t cursor) {
    const auto& book = venue.book;
    if (book.state != mockx::feed::FeedState::live || !book.symbol_present
        || book.trading_state != 'T') {
        return nullptr;
    }
    if (parent.side == ParentSide::buy) {
        if (cursor >= book.ask_count) {
            return nullptr;
        }
        const auto& level = book.asks[cursor];
        return level.price <= parent.limit_price4 ? &level : nullptr;
    }
    if (cursor >= book.bid_count) {
        return nullptr;
    }
    const auto& level = book.bids[cursor];
    return level.price >= parent.limit_price4 ? &level : nullptr;
}

bool better(
    ParentSide side,
    const VenueSnapshot& candidate,
    const mockx::feed::DisplayLevel& candidate_level,
    const VenueSnapshot& incumbent,
    const mockx::feed::DisplayLevel& incumbent_level) {
    if (candidate_level.price != incumbent_level.price) {
        return side == ParentSide::buy
            ? candidate_level.price < incumbent_level.price
            : candidate_level.price > incumbent_level.price;
    }
    return candidate.priority < incumbent.priority;
}

ChildOrder& child_for(
    RoutePlan& plan,
    const ParentOrder& parent,
    const VenueSnapshot& venue,
    std::uint8_t venue_index) {
    for (std::uint8_t index = 0; index < plan.child_count; ++index) {
        if (plan.children[index].venue_index == venue_index) {
            return plan.children[index];
        }
    }
    if (plan.child_count == plan.children.size()) {
        throw RoutingError("route plan exceeds two child orders");
    }
    const auto child_id = parent.parent_id + '-' + venue.venue_id;
    if (child_id.size() > 64) {
        throw RoutingError("generated child ClOrdID exceeds 64 bytes");
    }
    auto& child = plan.children[plan.child_count++];
    child.venue_index = venue_index;
    child.cl_ord_id = child_id;
    return child;
}

} // namespace

RoutePlan plan_route(
    const ParentOrder& parent,
    const std::array<VenueSnapshot, venue_count>& venues) {
    if (parent.parent_id.empty() || parent.symbol.empty()) {
        throw RoutingError("parent ID and symbol must be non-empty");
    }
    if (parent.quantity_lots == 0 || parent.limit_price4 == 0) {
        throw RoutingError("parent quantity and limit price must be positive");
    }
    if (venues[0].venue_id.empty() || venues[1].venue_id.empty()
        || venues[0].venue_id == venues[1].venue_id) {
        throw RoutingError("two distinct venue IDs are required");
    }
    if (venues[0].priority == venues[1].priority) {
        throw RoutingError("venue priorities must be distinct");
    }
    for (const auto& venue : venues) {
        if (venue.book.state != mockx::feed::FeedState::live
            || !venue.book.symbol_present) {
            throw RoutingError("both venue feeds must be synchronized");
        }
    }

    RoutePlan plan;
    std::array<std::size_t, venue_count> cursors{};
    std::uint32_t remaining = parent.quantity_lots;
    while (remaining != 0) {
        int selected = -1;
        const mockx::feed::DisplayLevel* selected_level = nullptr;
        for (std::size_t venue_index = 0; venue_index < venue_count; ++venue_index) {
            const auto* level = current_level(
                parent, venues[venue_index], cursors[venue_index]);
            if (level == nullptr || level->shares == 0) {
                continue;
            }
            if (selected < 0
                || better(
                    parent.side,
                    venues[venue_index],
                    *level,
                    venues[static_cast<std::size_t>(selected)],
                    *selected_level)) {
                selected = static_cast<int>(venue_index);
                selected_level = level;
            }
        }
        if (selected < 0 || selected_level == nullptr) {
            break;
        }

        const auto venue_index = static_cast<std::size_t>(selected);
        const auto take64 = std::min<std::uint64_t>(selected_level->shares, remaining);
        const auto take = static_cast<std::uint32_t>(take64);
        auto& child = child_for(
            plan,
            parent,
            venues[venue_index],
            static_cast<std::uint8_t>(venue_index));
        if (child.quantity_lots > std::numeric_limits<std::uint32_t>::max() - take) {
            throw RoutingError("child quantity overflows uint32");
        }
        child.quantity_lots += take;
        child.limit_price4 = selected_level->price;
        plan.planned_lots += take;
        remaining -= take;
        ++cursors[venue_index];
    }
    plan.initial_unrouted_lots = remaining;
    return plan;
}

} // namespace mockx::sor

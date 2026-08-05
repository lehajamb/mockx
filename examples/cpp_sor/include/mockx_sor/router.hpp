#pragma once

#include "mockx_feed/feed_handler.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>

namespace mockx::sor {

inline constexpr std::size_t venue_count = 2;

enum class ParentSide : char { buy = '1', sell = '2' };

class RoutingError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct ParentOrder {
    std::string parent_id;
    std::string symbol;
    ParentSide side{ParentSide::buy};
    std::uint32_t quantity_lots{};
    std::uint32_t limit_price4{};
};

struct VenueSnapshot {
    std::string venue_id;
    std::uint8_t priority{};
    mockx::feed::DepthSnapshot book;
};

struct ChildOrder {
    std::uint8_t venue_index{};
    std::uint32_t quantity_lots{};
    std::uint32_t limit_price4{};
    std::string cl_ord_id;
};

struct RoutePlan {
    std::array<ChildOrder, venue_count> children{};
    std::uint8_t child_count{};
    std::uint32_t planned_lots{};
    std::uint32_t initial_unrouted_lots{};
};

[[nodiscard]] RoutePlan plan_route(
    const ParentOrder& parent,
    const std::array<VenueSnapshot, venue_count>& venues);

} // namespace mockx::sor

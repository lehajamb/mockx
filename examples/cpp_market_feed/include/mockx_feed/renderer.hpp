#pragma once

#include "mockx_feed/order_book.hpp"
#include "mockx_feed/spsc_ring.hpp"

#include <atomic>
#include <cstdint>
#include <string>

namespace mockx::feed {

[[nodiscard]] std::string format_price4(std::uint32_t price);
void enable_virtual_terminal();
void render_snapshot(const DisplaySnapshot& snapshot);
void renderer_loop(ObjectRing<DisplaySnapshot>& ring, const std::atomic<bool>& stop);

} // namespace mockx::feed

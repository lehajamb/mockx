#include "mockx_feed/renderer.hpp"

#include <algorithm>
#include <chrono>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <thread>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace mockx::feed {

std::string format_price4(std::uint32_t price) {
    std::ostringstream output;
    output << (price / 10'000U) << '.' << std::setfill('0') << std::setw(4)
           << (price % 10'000U);
    return output.str();
}

void enable_virtual_terminal() {
#ifdef _WIN32
    const auto output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output == INVALID_HANDLE_VALUE) {
        return;
    }
    DWORD mode = 0;
    if (GetConsoleMode(output, &mode) != 0) {
        (void)SetConsoleMode(output, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING);
    }
#endif
}

void render_snapshot(const DisplaySnapshot& snapshot) {
    std::ostringstream output;
    output << "\x1b[2J\x1b[H"
           << "MockX GLIMPSE/ITCH market feed  state=" << snapshot.connection_state
           << "  session="
           << (snapshot.mold_session.empty() ? "<waiting>" : snapshot.mold_session)
           << "  next_seq=" << snapshot.next_sequence << '\n'
           << "packets=" << snapshot.counters.packets
           << "  messages=" << snapshot.counters.messages
           << "  ring_drops=" << snapshot.counters.ring_drops
           << "  gaps=" << snapshot.counters.gaps
           << "  resyncs=" << snapshot.counters.resyncs << "\n\n";

    for (const auto& instrument : snapshot.instruments) {
        output << instrument.symbol << "  trading_state=" << instrument.trading_state << '\n'
               << std::left << std::setw(14) << "BID PRICE"
               << std::right << std::setw(14) << "BID SHARES"
               << std::setw(9) << "ORDERS"
               << "    " << std::left << std::setw(14) << "ASK PRICE"
               << std::right << std::setw(14) << "ASK SHARES"
               << std::setw(9) << "ORDERS" << '\n';
        const auto rows = std::max(instrument.bids.size(), instrument.asks.size());
        for (std::size_t index = 0; index < rows; ++index) {
            if (index < instrument.bids.size()) {
                const auto& level = instrument.bids[index];
                output << std::left << std::setw(14) << format_price4(level.price)
                       << std::right << std::setw(14) << level.shares
                       << std::setw(9) << level.order_count;
            } else {
                output << std::setw(37) << " ";
            }
            output << "    ";
            if (index < instrument.asks.size()) {
                const auto& level = instrument.asks[index];
                output << std::left << std::setw(14) << format_price4(level.price)
                       << std::right << std::setw(14) << level.shares
                       << std::setw(9) << level.order_count;
            }
            output << '\n';
        }
        output << '\n';
    }
    output << "Ctrl+C to stop\n";
    std::cout << output.str() << std::flush;
}

void renderer_loop(ObjectRing<DisplaySnapshot>& ring, const std::atomic<bool>& stop) {
    enable_virtual_terminal();
    DisplaySnapshot current;
    while (!stop.load(std::memory_order_acquire)) {
        bool received = false;
        DisplaySnapshot next;
        while (ring.try_pop(next)) {
            current = std::move(next);
            received = true;
        }
        if (received) {
            render_snapshot(current);
        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
    }
    DisplaySnapshot final_snapshot;
    while (ring.try_pop(final_snapshot)) {
        current = std::move(final_snapshot);
    }
    if (!current.connection_state.empty()) {
        render_snapshot(current);
    }
}

} // namespace mockx::feed

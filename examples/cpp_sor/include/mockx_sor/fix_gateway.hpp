#pragma once

#include "mockx_feed/spsc_ring.hpp"
#include "mockx_sor/config.hpp"
#include "mockx_sor/decimal.hpp"
#include "mockx_sor/router.hpp"

#include <quickfix/Application.h>
#include <quickfix/Message.h>

#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <unordered_set>
#include <vector>

namespace mockx::sor {

struct ExecutionEvent {
    std::uint8_t venue_index{};
    std::array<char, 65> cl_ord_id{};
    std::array<char, 65> exec_id{};
    std::array<char, 129> text{};
    char exec_type{};
    char ord_status{};
    std::uint32_t last_qty_lots{};
    std::uint32_t last_price4{};
    std::uint32_t cum_qty_lots{};
    bool has_last_fill{};
    bool terminal{};
};

[[nodiscard]] FIX::Message make_child_message(
    const ChildOrder& child,
    const ParentOrder& parent,
    Decimal lot_size);

class FixApplication final : public FIX::Application {
public:
    FixApplication(
        const std::vector<VenueDefinition>& venues,
        Decimal lot_size,
        std::array<mockx::feed::ObjectRing<ExecutionEvent>*, venue_count> rings);

    void onCreate(const FIX::SessionID& session_id) override;
    void onLogon(const FIX::SessionID& session_id) override;
    void onLogout(const FIX::SessionID& session_id) override;
    void toAdmin(FIX::Message& message, const FIX::SessionID& session_id) override;
    void toApp(FIX::Message& message, const FIX::SessionID& session_id) override;
    void fromAdmin(const FIX::Message& message, const FIX::SessionID& session_id) override;
    void fromApp(const FIX::Message& message, const FIX::SessionID& session_id) override;

    [[nodiscard]] bool all_logged_on() const noexcept;
    [[nodiscard]] bool failed() const noexcept;
    [[nodiscard]] bool ring_overflowed() const noexcept;
    [[nodiscard]] std::string error() const;
    [[nodiscard]] bool send_child(
        const ChildOrder& child,
        const ParentOrder& parent);

private:
    struct Binding {
        FIX::SessionID session_id;
        std::uint8_t venue_index{};
    };

    [[nodiscard]] int find_venue(const FIX::SessionID& session_id) const noexcept;
    void fail(std::string message) noexcept;

    std::vector<Binding> bindings_;
    Decimal lot_size_;
    std::array<mockx::feed::ObjectRing<ExecutionEvent>*, venue_count> rings_;
    std::array<std::atomic<bool>, venue_count> logged_on_{};
    std::atomic<bool> failed_{false};
    std::atomic<bool> ring_overflowed_{false};
    std::array<std::unordered_set<std::string>, venue_count> seen_exec_ids_;
    mutable std::mutex dedupe_mutex_;
    mutable std::mutex error_mutex_;
    std::string error_;
};

} // namespace mockx::sor

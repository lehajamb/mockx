#include "mockx_sor/fix_gateway.hpp"

#include <quickfix/Fields.h>
#include <quickfix/Session.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <ctime>
#include <stdexcept>

namespace mockx::sor {
namespace {

std::string field_string(const FIX::Message& message, int tag) {
    FIX::StringField field(tag);
    message.getField(field);
    return field.getValue();
}

std::string header_string(const FIX::Message& message, int tag) {
    FIX::StringField field(tag);
    message.getHeader().getField(field);
    return field.getValue();
}

template <std::size_t N>
void copy_fixed(std::array<char, N>& output, const std::string& input, const char* name) {
    if (input.size() >= N) {
        throw std::runtime_error(std::string(name) + " exceeds fixed event capacity");
    }
    output.fill('\0');
    std::copy(input.begin(), input.end(), output.begin());
}

std::string utc_timestamp() {
    const auto now = std::chrono::system_clock::now();
    const auto seconds = std::chrono::system_clock::to_time_t(now);
    std::tm utc{};
#ifdef _WIN32
    gmtime_s(&utc, &seconds);
#else
    gmtime_r(&seconds, &utc);
#endif
    const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()) % 1000;
    char buffer[32]{};
    const auto written = std::snprintf(
        buffer,
        sizeof(buffer),
        "%04d%02d%02d-%02d:%02d:%02d.%03lld",
        utc.tm_year + 1900,
        utc.tm_mon + 1,
        utc.tm_mday,
        utc.tm_hour,
        utc.tm_min,
        utc.tm_sec,
        static_cast<long long>(millis.count()));
    if (written <= 0 || static_cast<std::size_t>(written) >= sizeof(buffer)) {
        throw std::runtime_error("failed to format FIX TransactTime");
    }
    return buffer;
}

bool terminal_status(char status) noexcept {
    return status == '2' || status == '4' || status == '8';
}

} // namespace

FIX::Message make_child_message(
    const ChildOrder& child,
    const ParentOrder& parent,
    Decimal lot_size) {
    if (child.cl_ord_id.empty() || parent.symbol.empty()
        || child.quantity_lots == 0 || child.limit_price4 == 0) {
        throw std::invalid_argument("FIX child fields must be non-empty and positive");
    }
    FIX::Message message;
    message.getHeader().setField(FIX::MsgType("D"));
    message.setField(FIX::ClOrdID(child.cl_ord_id));
    message.setField(FIX::Symbol(parent.symbol));
    message.setField(FIX::Side(
        parent.side == ParentSide::buy ? FIX::Side_BUY : FIX::Side_SELL));
    message.setField(FIX::StringField(38, format_lots(child.quantity_lots, lot_size)));
    message.setField(FIX::OrdType(FIX::OrdType_LIMIT));
    message.setField(FIX::StringField(44, format_price4(child.limit_price4)));
    message.setField(FIX::TimeInForce(FIX::TimeInForce_IMMEDIATE_OR_CANCEL));
    message.setField(FIX::StringField(60, utc_timestamp()));
    return message;
}

FixApplication::FixApplication(
    const std::vector<VenueDefinition>& venues,
    Decimal lot_size,
    std::array<mockx::feed::ObjectRing<ExecutionEvent>*, venue_count> rings)
    : lot_size_(lot_size), rings_(rings) {
    if (venues.size() != venue_count || rings_[0] == nullptr || rings_[1] == nullptr) {
        throw std::invalid_argument("FIX application requires exactly two venue rings");
    }
    for (std::size_t index = 0; index < venues.size(); ++index) {
        bindings_.push_back(Binding{
            venues[index].session_id, static_cast<std::uint8_t>(index)});
    }
}

int FixApplication::find_venue(const FIX::SessionID& session_id) const noexcept {
    for (const auto& binding : bindings_) {
        if (binding.session_id == session_id) {
            return binding.venue_index;
        }
    }
    return -1;
}

void FixApplication::fail(std::string message) noexcept {
    try {
        {
            std::lock_guard lock(error_mutex_);
            if (error_.empty()) {
                error_ = std::move(message);
            }
        }
        failed_.store(true, std::memory_order_release);
    } catch (...) {
        failed_.store(true, std::memory_order_release);
    }
}

void FixApplication::onCreate(const FIX::SessionID& session_id) {
    if (find_venue(session_id) < 0) {
        fail("QuickFIX created an unknown session: " + session_id.toString());
    }
}

void FixApplication::onLogon(const FIX::SessionID& session_id) {
    const auto venue = find_venue(session_id);
    if (venue < 0) {
        fail("QuickFIX logged on an unknown session: " + session_id.toString());
        return;
    }
    logged_on_[static_cast<std::size_t>(venue)].store(true, std::memory_order_release);
}

void FixApplication::onLogout(const FIX::SessionID& session_id) {
    const auto venue = find_venue(session_id);
    if (venue >= 0) {
        const auto was_logged_on = logged_on_[static_cast<std::size_t>(venue)].exchange(
            false, std::memory_order_acq_rel);
        if (was_logged_on) {
            fail("FIX session logged out: " + session_id.toString());
        }
    }
}

void FixApplication::toAdmin(FIX::Message&, const FIX::SessionID&) {}

void FixApplication::toApp(FIX::Message&, const FIX::SessionID&) {}

void FixApplication::fromAdmin(
    const FIX::Message& message,
    const FIX::SessionID& session_id) {
    try {
        const auto type = header_string(message, 35);
        if (type == "3") {
            fail("FIX session reject from " + session_id.toString());
        }
    } catch (const std::exception& error) {
        fail(std::string("invalid FIX admin message: ") + error.what());
    }
}

void FixApplication::fromApp(
    const FIX::Message& message,
    const FIX::SessionID& session_id) {
    try {
        const auto venue = find_venue(session_id);
        if (venue < 0) {
            throw std::runtime_error("application message belongs to an unknown session");
        }
        const auto type = header_string(message, 35);
        if (type == "j") {
            fail("FIX business-message reject from " + session_id.toString());
            return;
        }
        if (type != "8") {
            throw std::runtime_error("unsupported FIX application message type " + type);
        }

        const auto cl_ord_id = field_string(message, 11);
        const auto exec_id = field_string(message, 17);
        if (cl_ord_id.empty() || exec_id.empty()) {
            throw std::runtime_error("ClOrdID and ExecID must be non-empty");
        }
        {
            std::lock_guard lock(dedupe_mutex_);
            if (!seen_exec_ids_[static_cast<std::size_t>(venue)]
                     .insert(exec_id)
                     .second) {
                return;
            }
        }

        auto slot = rings_[static_cast<std::size_t>(venue)]->try_reserve();
        if (!slot) {
            ring_overflowed_.store(true, std::memory_order_release);
            fail("FIX execution-report ring overflow");
            return;
        }
        auto& event = *slot->value;
        event = ExecutionEvent{};
        event.venue_index = static_cast<std::uint8_t>(venue);
        copy_fixed(event.cl_ord_id, cl_ord_id, "ClOrdID");
        copy_fixed(event.exec_id, exec_id, "ExecID");
        const auto exec_type = field_string(message, 150);
        const auto ord_status = field_string(message, 39);
        if (exec_type.size() != 1 || ord_status.size() != 1) {
            throw std::runtime_error("ExecType and OrdStatus must be one byte");
        }
        event.exec_type = exec_type.front();
        event.ord_status = ord_status.front();
        event.cum_qty_lots = parse_lots(field_string(message, 14), lot_size_);
        if (message.isSetField(32) || message.isSetField(31)) {
            if (!message.isSetField(32) || !message.isSetField(31)) {
                throw std::runtime_error("LastQty and LastPx must appear together");
            }
            event.last_qty_lots = parse_lots(field_string(message, 32), lot_size_);
            event.last_price4 = parse_price4(field_string(message, 31));
            event.has_last_fill = true;
        }
        if (message.isSetField(58)) {
            copy_fixed(event.text, field_string(message, 58), "Text");
        }
        event.terminal = terminal_status(event.ord_status);
        rings_[static_cast<std::size_t>(venue)]->commit(*slot);
    } catch (const std::exception& error) {
        fail(std::string("invalid FIX execution report: ") + error.what());
    }
}

bool FixApplication::all_logged_on() const noexcept {
    return logged_on_[0].load(std::memory_order_acquire)
        && logged_on_[1].load(std::memory_order_acquire);
}

bool FixApplication::failed() const noexcept {
    return failed_.load(std::memory_order_acquire);
}

bool FixApplication::ring_overflowed() const noexcept {
    return ring_overflowed_.load(std::memory_order_acquire);
}

std::string FixApplication::error() const {
    std::lock_guard lock(error_mutex_);
    return error_;
}

bool FixApplication::send_child(
    const ChildOrder& child,
    const ParentOrder& parent) {
    if (child.venue_index >= bindings_.size()) {
        fail("child references an unknown FIX venue");
        return false;
    }
    try {
        auto message = make_child_message(child, parent, lot_size_);
        const auto sent = FIX::Session::sendToTarget(
            message, bindings_[child.venue_index].session_id);
        if (!sent) {
            fail("QuickFIX refused child " + child.cl_ord_id);
        }
        return sent;
    } catch (const std::exception& error) {
        fail(std::string("failed to send FIX child: ") + error.what());
        return false;
    }
}

} // namespace mockx::sor

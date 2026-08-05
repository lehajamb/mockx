#pragma once

#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <new>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>

namespace mockx::feed {

class ProtocolError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class UnsupportedMessage : public ProtocolError {
public:
    using ProtocolError::ProtocolError;
};

template <std::size_t N>
using Bytes = std::array<std::byte, N>;

struct MoldHeaderWire {
    std::array<char, 10> session;
    Bytes<8> sequence;
    Bytes<2> message_count;
};

struct SystemEventWire {
    char type;
    Bytes<2> stock_locate;
    Bytes<2> tracking_number;
    Bytes<6> timestamp;
    char event_code;
};

struct StockDirectoryWire {
    char type;
    Bytes<2> stock_locate;
    Bytes<2> tracking_number;
    Bytes<6> timestamp;
    std::array<char, 8> stock;
    char market_category;
    char financial_status;
    Bytes<4> round_lot_size;
    char round_lots_only;
    char issue_classification;
    std::array<char, 2> issue_sub_type;
    char authenticity;
    char short_sale_threshold;
    char ipo_flag;
    char luld_tier;
    char etp_flag;
    Bytes<4> etp_leverage_factor;
    char inverse_indicator;
};

struct StockTradingActionWire {
    char type;
    Bytes<2> stock_locate;
    Bytes<2> tracking_number;
    Bytes<6> timestamp;
    std::array<char, 8> stock;
    char trading_state;
    char reserved;
    std::array<char, 4> reason;
};

struct AddOrderWire {
    char type;
    Bytes<2> stock_locate;
    Bytes<2> tracking_number;
    Bytes<6> timestamp;
    Bytes<8> order_reference;
    char side;
    Bytes<4> shares;
    std::array<char, 8> stock;
    Bytes<4> price;
};

struct OrderExecutedWire {
    char type;
    Bytes<2> stock_locate;
    Bytes<2> tracking_number;
    Bytes<6> timestamp;
    Bytes<8> order_reference;
    Bytes<4> executed_shares;
    Bytes<8> match_number;
};

struct OrderExecutedWithPriceWire {
    char type;
    Bytes<2> stock_locate;
    Bytes<2> tracking_number;
    Bytes<6> timestamp;
    Bytes<8> order_reference;
    Bytes<4> executed_shares;
    Bytes<8> match_number;
    char printable;
    Bytes<4> execution_price;
};

struct OrderDeleteWire {
    char type;
    Bytes<2> stock_locate;
    Bytes<2> tracking_number;
    Bytes<6> timestamp;
    Bytes<8> order_reference;
};

struct EndSnapshotWire {
    char type;
    std::array<char, 20> next_itch_sequence;
};

template <typename T>
inline constexpr bool valid_wire_type =
    std::is_standard_layout_v<T> && std::is_trivially_default_constructible_v<T>
    && std::is_trivially_destructible_v<T> && alignof(T) == 1;

static_assert(valid_wire_type<MoldHeaderWire> && sizeof(MoldHeaderWire) == 20);
static_assert(valid_wire_type<SystemEventWire> && sizeof(SystemEventWire) == 12);
static_assert(valid_wire_type<StockDirectoryWire> && sizeof(StockDirectoryWire) == 39);
static_assert(valid_wire_type<StockTradingActionWire> && sizeof(StockTradingActionWire) == 25);
static_assert(valid_wire_type<AddOrderWire> && sizeof(AddOrderWire) == 36);
static_assert(valid_wire_type<OrderExecutedWire> && sizeof(OrderExecutedWire) == 31);
static_assert(
    valid_wire_type<OrderExecutedWithPriceWire>
    && sizeof(OrderExecutedWithPriceWire) == 36);
static_assert(valid_wire_type<OrderDeleteWire> && sizeof(OrderDeleteWire) == 19);
static_assert(valid_wire_type<EndSnapshotWire> && sizeof(EndSnapshotWire) == 21);

template <typename T>
[[nodiscard]] T* overlay(std::byte* address) noexcept {
    static_assert(valid_wire_type<T>);
    // Default-initialization of these trivial byte-only wire types performs no
    // writes. It starts T's lifetime in the receive buffer before the cast.
    ::new (static_cast<void*>(address)) T;
    return std::launder(reinterpret_cast<T*>(address));
}

template <std::size_t N>
[[nodiscard]] std::uint64_t load_be(const Bytes<N>& value) {
    static_assert(N > 0 && N <= 8);
    std::uint64_t result = 0;
    for (const auto byte : value) {
        result = (result << 8U) | std::to_integer<std::uint8_t>(byte);
    }
    return result;
}

inline void store_be(std::span<std::byte> output, std::uint64_t value) {
    if (output.empty() || output.size() > 8) {
        throw ProtocolError("big-endian field width must be between 1 and 8");
    }
    for (std::size_t index = output.size(); index-- > 0;) {
        output[index] = static_cast<std::byte>(value & 0xffU);
        value >>= 8U;
    }
    if (value != 0) {
        throw ProtocolError("integer does not fit big-endian field");
    }
}

template <std::size_t N>
[[nodiscard]] std::string trim_ascii(const std::array<char, N>& value) {
    std::size_t first = 0;
    std::size_t last = N;
    while (first < last && value[first] == ' ') {
        ++first;
    }
    while (last > first && value[last - 1] == ' ') {
        --last;
    }
    for (std::size_t index = first; index < last; ++index) {
        const auto ch = static_cast<unsigned char>(value[index]);
        if (ch < 0x21U || ch > 0x7eU) {
            throw ProtocolError("fixed-width field contains non-printable ASCII");
        }
    }
    return std::string(value.data() + first, last - first);
}

[[nodiscard]] inline std::uint64_t parse_padded_decimal(std::string_view value) {
    std::size_t first = 0;
    while (first < value.size() && value[first] == ' ') {
        ++first;
    }
    if (first == value.size()) {
        throw ProtocolError("numeric field is empty");
    }
    std::uint64_t result = 0;
    for (std::size_t index = first; index < value.size(); ++index) {
        const char ch = value[index];
        if (ch < '0' || ch > '9') {
            throw ProtocolError("numeric field is not left-space-padded decimal");
        }
        const auto digit = static_cast<std::uint64_t>(ch - '0');
        if (result > (std::numeric_limits<std::uint64_t>::max() - digit) / 10U) {
            throw ProtocolError("numeric field overflows uint64");
        }
        result = result * 10U + digit;
    }
    return result;
}

[[nodiscard]] inline std::optional<std::size_t> expected_itch_size(char type) noexcept {
    switch (type) {
    case 'S':
        return sizeof(SystemEventWire);
    case 'R':
        return sizeof(StockDirectoryWire);
    case 'H':
        return sizeof(StockTradingActionWire);
    case 'A':
        return sizeof(AddOrderWire);
    case 'E':
        return sizeof(OrderExecutedWire);
    case 'C':
        return sizeof(OrderExecutedWithPriceWire);
    case 'D':
        return sizeof(OrderDeleteWire);
    case 'G':
        return sizeof(EndSnapshotWire);
    default:
        return std::nullopt;
    }
}

inline void validate_itch_message(std::span<const std::byte> message) {
    if (message.empty()) {
        throw ProtocolError("ITCH message is empty");
    }
    const auto type = static_cast<char>(std::to_integer<unsigned char>(message.front()));
    const auto expected = expected_itch_size(type);
    if (!expected) {
        throw UnsupportedMessage(std::string("unsupported ITCH message type: ") + type);
    }
    if (message.size() != *expected) {
        throw ProtocolError(
            "ITCH message has invalid length for type " + std::string(1, type));
    }
}

} // namespace mockx::feed

#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

namespace mockx::sor {

class DecimalError : public std::invalid_argument {
public:
    using std::invalid_argument::invalid_argument;
};

struct Decimal {
    std::uint64_t mantissa{};
    std::uint8_t scale{};
};

[[nodiscard]] Decimal parse_positive_decimal(
    std::string_view text,
    std::uint8_t maximum_scale = 9);
[[nodiscard]] std::uint32_t parse_price4(std::string_view text);
[[nodiscard]] std::string format_price4(std::uint32_t price);
[[nodiscard]] std::string format_lots(std::uint32_t lots, Decimal lot_size);
[[nodiscard]] std::uint32_t parse_lots(std::string_view quantity, Decimal lot_size);
[[nodiscard]] std::string format_notional(
    std::uint64_t price4_lot_notional,
    Decimal lot_size);
[[nodiscard]] std::string format_vwap4(
    std::uint64_t price4_lot_notional,
    std::uint32_t filled_lots);

} // namespace mockx::sor

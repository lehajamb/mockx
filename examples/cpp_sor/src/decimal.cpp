#include "mockx_sor/decimal.hpp"

#include <charconv>
#include <iomanip>
#include <limits>
#include <sstream>
#include <vector>

namespace mockx::sor {
namespace {

std::uint64_t power10(std::uint8_t exponent) {
    std::uint64_t value = 1;
    for (std::uint8_t index = 0; index < exponent; ++index) {
        if (value > std::numeric_limits<std::uint64_t>::max() / 10U) {
            throw DecimalError("decimal scale overflows uint64");
        }
        value *= 10U;
    }
    return value;
}

std::uint64_t checked_multiply(std::uint64_t left, std::uint64_t right) {
    if (right != 0 && left > std::numeric_limits<std::uint64_t>::max() / right) {
        throw DecimalError("decimal multiplication overflows uint64");
    }
    return left * right;
}

std::string multiply_decimal_digits(std::uint64_t left, std::uint64_t right) {
    if (left == 0 || right == 0) {
        return "0";
    }
    const auto left_digits = std::to_string(left);
    const auto right_digits = std::to_string(right);
    std::vector<unsigned int> digits(left_digits.size() + right_digits.size());
    for (std::size_t left_index = 0; left_index < left_digits.size(); ++left_index) {
        for (std::size_t right_index = 0;
             right_index < right_digits.size();
             ++right_index) {
            digits[left_index + right_index + 1U] +=
                static_cast<unsigned int>(left_digits[left_index] - '0')
                * static_cast<unsigned int>(right_digits[right_index] - '0');
        }
    }
    for (std::size_t index = digits.size() - 1U; index != 0; --index) {
        digits[index - 1U] += digits[index] / 10U;
        digits[index] %= 10U;
    }
    std::string result;
    result.reserve(digits.size());
    bool leading = true;
    for (const auto digit : digits) {
        if (leading && digit == 0) {
            continue;
        }
        leading = false;
        result.push_back(static_cast<char>('0' + digit));
    }
    return result;
}

std::string format_scaled_digits(std::string digits, std::size_t scale) {
    if (scale == 0) {
        return digits;
    }
    if (digits.size() <= scale) {
        digits.insert(0, scale + 1U - digits.size(), '0');
    }
    digits.insert(digits.size() - scale, 1, '.');
    return digits;
}

} // namespace

Decimal parse_positive_decimal(std::string_view text, std::uint8_t maximum_scale) {
    if (text.empty()) {
        throw DecimalError("decimal value is empty");
    }
    std::uint64_t mantissa = 0;
    std::uint8_t scale = 0;
    bool decimal_seen = false;
    bool digit_seen = false;
    for (const char ch : text) {
        if (ch == '.') {
            if (decimal_seen) {
                throw DecimalError("decimal value contains more than one point");
            }
            decimal_seen = true;
            continue;
        }
        if (ch < '0' || ch > '9') {
            throw DecimalError("decimal value must contain only digits and one point");
        }
        digit_seen = true;
        if (mantissa > (std::numeric_limits<std::uint64_t>::max()
                        - static_cast<std::uint64_t>(ch - '0'))
                / 10U) {
            throw DecimalError("decimal value overflows uint64");
        }
        mantissa = mantissa * 10U + static_cast<std::uint64_t>(ch - '0');
        if (decimal_seen) {
            if (scale == maximum_scale) {
                throw DecimalError("decimal value has too many fractional digits");
            }
            ++scale;
        }
    }
    if (!digit_seen || mantissa == 0) {
        throw DecimalError("decimal value must be positive");
    }
    return Decimal{mantissa, scale};
}

std::uint32_t parse_price4(std::string_view text) {
    const auto parsed = parse_positive_decimal(text, 4);
    const auto scaled = checked_multiply(parsed.mantissa, power10(4 - parsed.scale));
    if (scaled > std::numeric_limits<std::uint32_t>::max()) {
        throw DecimalError("Price(4) value exceeds uint32");
    }
    return static_cast<std::uint32_t>(scaled);
}

std::string format_price4(std::uint32_t price) {
    std::ostringstream output;
    output << (price / 10'000U) << '.' << std::setfill('0') << std::setw(4)
           << (price % 10'000U);
    return output.str();
}

std::string format_lots(std::uint32_t lots, Decimal lot_size) {
    if (lots == 0 || lot_size.mantissa == 0) {
        throw DecimalError("lots and lot size must be positive");
    }
    const auto value = checked_multiply(lots, lot_size.mantissa);
    if (lot_size.scale == 0) {
        return std::to_string(value);
    }
    std::string digits = std::to_string(value);
    if (digits.size() <= lot_size.scale) {
        digits.insert(0, static_cast<std::size_t>(lot_size.scale) + 1U - digits.size(), '0');
    }
    digits.insert(digits.size() - lot_size.scale, 1, '.');
    return digits;
}

std::uint32_t parse_lots(std::string_view quantity, Decimal lot_size) {
    bool zero = !quantity.empty();
    bool point_seen = false;
    bool digit_seen = false;
    for (const char ch : quantity) {
        if (ch == '.' && !point_seen) {
            point_seen = true;
            continue;
        }
        if (ch < '0' || ch > '9') {
            zero = false;
            break;
        }
        digit_seen = true;
        if (ch != '0') {
            zero = false;
        }
    }
    if (zero && digit_seen) {
        return 0;
    }
    const auto actual = parse_positive_decimal(quantity, 9);
    const auto common_scale = actual.scale > lot_size.scale ? actual.scale : lot_size.scale;
    const auto actual_scaled = checked_multiply(
        actual.mantissa, power10(common_scale - actual.scale));
    const auto lot_scaled = checked_multiply(
        lot_size.mantissa, power10(common_scale - lot_size.scale));
    if (lot_scaled == 0 || actual_scaled % lot_scaled != 0) {
        throw DecimalError("FIX quantity is not an exact number of configured lots");
    }
    const auto lots = actual_scaled / lot_scaled;
    if (lots == 0 || lots > std::numeric_limits<std::uint32_t>::max()) {
        throw DecimalError("FIX quantity lot count exceeds uint32");
    }
    return static_cast<std::uint32_t>(lots);
}

std::string format_notional(
    std::uint64_t price4_lot_notional,
    Decimal lot_size) {
    if (lot_size.mantissa == 0) {
        throw DecimalError("lot size must be positive");
    }
    return format_scaled_digits(
        multiply_decimal_digits(price4_lot_notional, lot_size.mantissa),
        static_cast<std::size_t>(lot_size.scale) + 4U);
}

std::string format_vwap4(
    std::uint64_t price4_lot_notional,
    std::uint32_t filled_lots) {
    if (filled_lots == 0) {
        return "N/A";
    }
    const auto quotient = price4_lot_notional / filled_lots;
    const auto remainder = price4_lot_notional % filled_lots;
    const auto rounded = quotient
        + (remainder * 2U >= static_cast<std::uint64_t>(filled_lots) ? 1U : 0U);
    if (rounded > std::numeric_limits<std::uint32_t>::max()) {
        throw DecimalError("VWAP exceeds Price(4) range");
    }
    return format_price4(static_cast<std::uint32_t>(rounded));
}

} // namespace mockx::sor

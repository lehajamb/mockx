#include "mockx_sor/decimal.hpp"
#include "mockx_sor/router.hpp"

#include <exception>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using namespace mockx::sor;

struct TestFailure : std::runtime_error {
    using std::runtime_error::runtime_error;
};

#define CHECK(condition) \
    do { \
        if (!(condition)) { \
            throw TestFailure(std::string("CHECK failed: ") + #condition); \
        } \
    } while (false)

template <typename Exception, typename Callable>
void check_throws(Callable&& callable) {
    try {
        callable();
    } catch (const Exception&) {
        return;
    }
    throw TestFailure("expected exception was not thrown");
}

VenueSnapshot venue(std::string id, std::uint8_t priority) {
    VenueSnapshot result;
    result.venue_id = std::move(id);
    result.priority = priority;
    result.book.state = mockx::feed::FeedState::live;
    result.book.symbol_present = true;
    result.book.trading_state = 'T';
    return result;
}

void ask(VenueSnapshot& venue, std::uint32_t price, std::uint64_t shares) {
    venue.book.asks[venue.book.ask_count++] = {price, shares, 1};
}

void bid(VenueSnapshot& venue, std::uint32_t price, std::uint64_t shares) {
    venue.book.bids[venue.book.bid_count++] = {price, shares, 1};
}

void test_buy_sweep_and_partial_remainder() {
    auto first = venue("A", 0);
    auto second = venue("B", 1);
    ask(first, 1'000'000, 2);
    ask(first, 1'020'000, 9);
    ask(second, 990'000, 3);
    ask(second, 1'000'000, 1);
    const ParentOrder parent{"P1", "TEST", ParentSide::buy, 7, 1'000'000};
    const auto plan = plan_route(parent, {first, second});
    CHECK(plan.child_count == 2);
    CHECK(plan.planned_lots == 6);
    CHECK(plan.initial_unrouted_lots == 1);
    CHECK(plan.children[0].venue_index == 1);
    CHECK(plan.children[0].quantity_lots == 4);
    CHECK(plan.children[0].limit_price4 == 1'000'000);
    CHECK(plan.children[1].venue_index == 0);
    CHECK(plan.children[1].quantity_lots == 2);
}

void test_sell_sweep_and_limit() {
    auto first = venue("A", 0);
    auto second = venue("B", 1);
    bid(first, 1'010'000, 2);
    bid(first, 990'000, 5);
    bid(second, 1'020'000, 1);
    bid(second, 1'000'000, 4);
    const ParentOrder parent{"P2", "TEST", ParentSide::sell, 8, 1'000'000};
    const auto plan = plan_route(parent, {first, second});
    CHECK(plan.planned_lots == 7);
    CHECK(plan.initial_unrouted_lots == 1);
    CHECK(plan.children[0].venue_index == 1);
    CHECK(plan.children[0].quantity_lots == 5);
    CHECK(plan.children[0].limit_price4 == 1'000'000);
    CHECK(plan.children[1].quantity_lots == 2);
}

void test_equal_price_priority_and_halt() {
    auto first = venue("A", 1);
    auto second = venue("B", 0);
    ask(first, 1'000'000, 2);
    ask(second, 1'000'000, 2);
    const ParentOrder parent{"P3", "TEST", ParentSide::buy, 1, 1'000'000};
    auto plan = plan_route(parent, {first, second});
    CHECK(plan.child_count == 1);
    CHECK(plan.children[0].venue_index == 1);

    second.book.trading_state = 'H';
    plan = plan_route(parent, {first, second});
    CHECK(plan.children[0].venue_index == 0);
}

void test_zero_liquidity_and_unready() {
    auto first = venue("A", 0);
    auto second = venue("B", 1);
    const ParentOrder parent{"P4", "TEST", ParentSide::buy, 3, 1'000'000};
    const auto plan = plan_route(parent, {first, second});
    CHECK(plan.child_count == 0);
    CHECK(plan.initial_unrouted_lots == 3);
    second.book.state = mockx::feed::FeedState::recovering;
    check_throws<RoutingError>([&] { (void)plan_route(parent, {first, second}); });
}

void test_exact_decimals() {
    CHECK(parse_price4("100.01") == 1'000'100);
    CHECK(format_price4(1'000'100) == "100.0100");
    const auto lot = parse_positive_decimal("0.001");
    CHECK(format_lots(6, lot) == "0.006");
    CHECK(parse_lots("0.006", lot) == 6);
    CHECK(parse_lots("6.000", parse_positive_decimal("1")) == 6);
    CHECK(format_notional(5'970'000, lot) == "0.5970000");
    CHECK(format_vwap4(990'000U * 3U + 1'000'000U * 2U, 5) == "99.4000");
    check_throws<DecimalError>([] { (void)parse_price4("1.00001"); });
    check_throws<DecimalError>([&] { (void)parse_lots("0.0015", lot); });
    check_throws<DecimalError>([] { (void)parse_price4("429496.7296"); });
    check_throws<DecimalError>([] {
        (void)parse_lots("4294967296", parse_positive_decimal("1"));
    });
    check_throws<DecimalError>([] {
        (void)format_lots(
            2,
            parse_positive_decimal("18446744073709551615"));
    });
}

} // namespace

int main() {
    const std::vector<std::pair<std::string, std::function<void()>>> tests = {
        {"buy sweep and remainder", test_buy_sweep_and_partial_remainder},
        {"sell sweep and limit", test_sell_sweep_and_limit},
        {"priority and halt", test_equal_price_priority_and_halt},
        {"zero liquidity and readiness", test_zero_liquidity_and_unready},
        {"exact decimals", test_exact_decimals},
    };
    std::size_t failures = 0;
    for (const auto& [name, test] : tests) {
        try {
            test();
            std::cout << "[PASS] " << name << '\n';
        } catch (const std::exception& error) {
            ++failures;
            std::cerr << "[FAIL] " << name << ": " << error.what() << '\n';
        }
    }
    return failures == 0 ? 0 : 1;
}

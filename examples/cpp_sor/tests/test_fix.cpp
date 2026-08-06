#include "mockx_sor/app.hpp"
#include "mockx_sor/fix_gateway.hpp"

#include <quickfix/Fields.h>

#include <cstdlib>
#include <exception>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
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

std::string field(const FIX::Message& message, int tag) {
    FIX::StringField value(tag);
    message.getField(value);
    return value.getValue();
}

FIX::Message report(
    std::string cl_ord_id,
    std::string exec_id,
    char exec_type,
    char status,
    std::string cum_qty,
    std::string last_qty = {},
    std::string last_price = {}) {
    FIX::Message message;
    message.getHeader().setField(FIX::MsgType("8"));
    message.setField(FIX::ClOrdID(std::move(cl_ord_id)));
    message.setField(FIX::ExecID(std::move(exec_id)));
    message.setField(FIX::ExecType(exec_type));
    message.setField(FIX::OrdStatus(status));
    message.setField(FIX::StringField(14, std::move(cum_qty)));
    if (!last_qty.empty()) {
        message.setField(FIX::StringField(32, std::move(last_qty)));
        message.setField(FIX::StringField(31, std::move(last_price)));
    }
    return message;
}

std::vector<VenueDefinition> venues() {
    VenueDefinition first;
    first.venue_id = "A";
    first.priority = 0;
    first.session_id = FIX::SessionID("FIX.4.4", "SOR_A", "MOCKX_A");
    VenueDefinition second;
    second.venue_id = "B";
    second.priority = 1;
    second.session_id = FIX::SessionID("FIX.4.4", "SOR_B", "MOCKX_B");
    return {first, second};
}

void set_environment(const char* name, const char* value) {
#ifdef _WIN32
    if (_putenv_s(name, value) != 0) {
        throw TestFailure("failed to set test environment variable");
    }
#else
    if (setenv(name, value, 1) != 0) {
        throw TestFailure("failed to set test environment variable");
    }
#endif
}

void test_two_session_configuration() {
    set_environment("MOCKX_SOR_A_GLIMPSE_PASSWORD", "SECRET_A");
    set_environment("MOCKX_SOR_B_GLIMPSE_PASSWORD", "SECRET_B");
    const LoadedConfig loaded(std::string(MOCKX_SOR_SOURCE_DIR) + "/sor.cfg");
    CHECK(loaded.venues.size() == venue_count);
    CHECK(loaded.venues[0].venue_id == "A");
    CHECK(loaded.venues[0].priority == 0);
    CHECK(loaded.venues[0].session_id.getSenderCompID().getValue() == "SOR_A");
    CHECK(loaded.venues[0].feed.itch_port == 18100);
    CHECK(loaded.venues[1].venue_id == "B");
    CHECK(loaded.venues[1].priority == 1);
    CHECK(loaded.venues[1].session_id.getSenderCompID().getValue() == "SOR_B");
    CHECK(loaded.venues[1].feed.glimpse_port == 18201);
}

void test_command_line_contract() {
    const char* valid[] = {
        "mockx_sor",
        "--config", "sor.cfg",
        "--parent-id", "PARENT-1",
        "--symbol", "TEST",
        "--side", "BUY",
        "--quantity-lots", "7",
        "--lot-size", "0.001",
        "--limit-price", "100.01"};
    const auto command = parse_command_line(15, const_cast<char**>(valid));
    CHECK(command.parent.quantity_lots == 7);
    CHECK(command.parent.limit_price4 == 1'000'100);
    CHECK(command.lot_size.mantissa == 1);
    CHECK(command.lot_size.scale == 3);

    const char* invalid[] = {
        "mockx_sor",
        "--config", "sor.cfg",
        "--parent-id", "PARENT-1",
        "--symbol", "TEST",
        "--side", "BUY",
        "--quantity-lots", "7",
        "--lot-size", "0.001",
        "--limit-price", "100.01",
        "--depth", "17"};
    check_throws<CommandLineError>([&] {
        (void)parse_command_line(17, const_cast<char**>(invalid));
    });
}

void test_child_message() {
    const ChildOrder child{0, 6, 1'000'100, "P1-A"};
    const ParentOrder parent{"P1", "TEST", ParentSide::buy, 6, 1'000'100};
    const auto message = make_child_message(
        child, parent, parse_positive_decimal("0.001"));
    CHECK(field(message, 11) == "P1-A");
    CHECK(field(message, 38) == "0.006");
    CHECK(field(message, 44) == "100.0100");
    CHECK(field(message, 40) == "2");
    CHECK(field(message, 59) == "3");
    CHECK(field(message, 54) == "1");
    CHECK(!field(message, 60).empty());
}

void test_logon_and_execution_reports() {
    mockx::feed::ObjectRing<ExecutionEvent> first(4);
    mockx::feed::ObjectRing<ExecutionEvent> second(4);
    const auto definitions = venues();
    FixApplication application(
        definitions,
        parse_positive_decimal("1"),
        {&first, &second});
    application.onLogon(definitions[0].session_id);
    CHECK(!application.all_logged_on());
    application.onLogon(definitions[1].session_id);
    CHECK(application.all_logged_on());

    application.fromApp(
        report("P1-A", "E1", 'F', '1', "2", "2", "99.0000"),
        definitions[0].session_id);
    application.fromApp(
        report("P1-A", "E1", 'F', '1', "2", "2", "99.0000"),
        definitions[0].session_id);
    ExecutionEvent event;
    CHECK(first.try_pop(event));
    CHECK(event.last_qty_lots == 2);
    CHECK(event.last_price4 == 990'000);
    CHECK(event.cum_qty_lots == 2);
    CHECK(!event.terminal);
    CHECK(!first.try_pop(event));

    application.fromApp(
        report("P1-A", "E2", '4', '4', "2"),
        definitions[0].session_id);
    CHECK(first.try_pop(event));
    CHECK(event.terminal);
    CHECK(event.ord_status == '4');
    CHECK(event.cum_qty_lots == 2);

    application.fromApp(
        report("P1-B", "E3", 'F', '2', "3", "3", "100.0000"),
        definitions[1].session_id);
    CHECK(second.try_pop(event));
    CHECK(event.terminal);
    CHECK(event.ord_status == '2');
    CHECK(event.cum_qty_lots == 3);

    application.fromApp(
        report("P2-B", "E4", '8', '8', "0"), definitions[1].session_id);
    CHECK(second.try_pop(event));
    CHECK(event.terminal);
    CHECK(event.ord_status == '8');
    CHECK(event.cum_qty_lots == 0);
    application.onLogout(definitions[1].session_id);
    CHECK(!application.all_logged_on());
    CHECK(application.failed());
    CHECK(application.error().find("logged out") != std::string::npos);
}

void test_report_ring_overflow_is_fatal() {
    mockx::feed::ObjectRing<ExecutionEvent> first(2);
    mockx::feed::ObjectRing<ExecutionEvent> second(2);
    const auto definitions = venues();
    FixApplication application(
        definitions,
        parse_positive_decimal("1"),
        {&first, &second});
    application.fromApp(
        report("P-A", "E1", '0', '0', "0"), definitions[0].session_id);
    application.fromApp(
        report("P-A", "E2", '0', '0', "0"), definitions[0].session_id);
    application.fromApp(
        report("P-A", "E3", '0', '0', "0"), definitions[0].session_id);
    CHECK(application.failed());
    CHECK(application.error().find("overflow") != std::string::npos);
}

} // namespace

int main() {
    const std::vector<std::pair<std::string, std::function<void()>>> tests = {
        {"two-session configuration", test_two_session_configuration},
        {"command-line contract", test_command_line_contract},
        {"child message", test_child_message},
        {"logon and reports", test_logon_and_execution_reports},
        {"report overflow", test_report_ring_overflow_is_fatal},
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

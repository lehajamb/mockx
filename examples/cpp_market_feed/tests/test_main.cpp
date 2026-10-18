#include "mockx_feed/app.hpp"
#include "mockx_feed/decoder.hpp"
#include "mockx_feed/feed_handler.hpp"
#include "mockx_feed/order_book.hpp"
#include "mockx_feed/protocol.hpp"
#include "mockx_feed/renderer.hpp"
#include "mockx_feed/spsc_ring.hpp"
#include "mockx_feed/soup.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <exception>
#include <functional>
#include <iostream>
#include <span>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {
using namespace mockx::feed;

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

void set_be(std::vector<std::byte>& wire, std::size_t offset, std::size_t width, std::uint64_t value) {
    store_be(std::span<std::byte>(wire).subspan(offset, width), value);
}

void set_ascii(
    std::vector<std::byte>& wire,
    std::size_t offset,
    std::size_t width,
    const std::string& value) {
    if (value.size() > width) {
        throw TestFailure("fixture ASCII field is too long");
    }
    for (std::size_t index = 0; index < width; ++index) {
        wire[offset + index] = static_cast<std::byte>(
            index < value.size() ? static_cast<unsigned char>(value[index]) : ' ');
    }
}

std::vector<std::byte> directory(std::uint16_t locate, const std::string& symbol) {
    std::vector<std::byte> message(sizeof(StockDirectoryWire));
    message[0] = std::byte{'R'};
    set_be(message, 1, 2, locate);
    set_ascii(message, 11, 8, symbol);
    set_be(message, 21, 4, 1);
    return message;
}

std::vector<std::byte> trading_action(std::uint16_t locate, const std::string& symbol) {
    std::vector<std::byte> message(sizeof(StockTradingActionWire));
    message[0] = std::byte{'H'};
    set_be(message, 1, 2, locate);
    set_ascii(message, 11, 8, symbol);
    message[19] = std::byte{'T'};
    message[20] = std::byte{' '};
    set_ascii(message, 21, 4, "");
    return message;
}

std::vector<std::byte> add_order(
    std::uint16_t locate,
    const std::string& symbol,
    std::uint64_t order,
    char side,
    std::uint32_t shares,
    std::uint32_t price) {
    std::vector<std::byte> message(sizeof(AddOrderWire));
    message[0] = std::byte{'A'};
    set_be(message, 1, 2, locate);
    set_be(message, 11, 8, order);
    message[19] = static_cast<std::byte>(side);
    set_be(message, 20, 4, shares);
    set_ascii(message, 24, 8, symbol);
    set_be(message, 32, 4, price);
    return message;
}

std::vector<std::byte> execution(
    std::uint16_t locate,
    std::uint64_t order,
    std::uint32_t shares,
    bool with_price = false) {
    std::vector<std::byte> message(
        with_price ? sizeof(OrderExecutedWithPriceWire) : sizeof(OrderExecutedWire));
    message[0] = with_price ? std::byte{'C'} : std::byte{'E'};
    set_be(message, 1, 2, locate);
    set_be(message, 11, 8, order);
    set_be(message, 19, 4, shares);
    set_be(message, 23, 8, 77);
    if (with_price) {
        message[31] = std::byte{'Y'};
        set_be(message, 32, 4, 1'000'000);
    }
    return message;
}

std::vector<std::byte> delete_order(std::uint16_t locate, std::uint64_t order) {
    std::vector<std::byte> message(sizeof(OrderDeleteWire));
    message[0] = std::byte{'D'};
    set_be(message, 1, 2, locate);
    set_be(message, 11, 8, order);
    return message;
}

std::vector<std::byte> system_event(char code) {
    std::vector<std::byte> message(sizeof(SystemEventWire));
    message[0] = std::byte{'S'};
    message[11] = static_cast<std::byte>(code);
    return message;
}

std::vector<std::byte> mold_packet(
    const std::string& session,
    std::uint64_t sequence,
    const std::vector<std::vector<std::byte>>& messages,
    std::uint16_t control_count = 1) {
    std::size_t size = sizeof(MoldHeaderWire);
    for (const auto& message : messages) {
        size += 2 + message.size();
    }
    std::vector<std::byte> packet(size);
    set_ascii(packet, 0, 10, session);
    set_be(packet, 10, 8, sequence);
    const auto count = messages.empty() ? control_count
                                        : static_cast<std::uint16_t>(messages.size());
    set_be(packet, 18, 2, count);
    std::size_t offset = 20;
    for (const auto& message : messages) {
        set_be(packet, offset, 2, message.size());
        offset += 2;
        std::memcpy(packet.data() + offset, message.data(), message.size());
        offset += message.size();
    }
    return packet;
}

void test_wire_views() {
    Bytes<8> value{};
    store_be(value, 0x0102030405060708ULL);
    CHECK(load_be(value) == 0x0102030405060708ULL);
    std::vector<std::byte> message = add_order(1, "TEST", 42, 'B', 7, 123'450);
    const auto* address = message.data();
    auto* view = overlay<AddOrderWire>(message.data());
    CHECK(reinterpret_cast<std::byte*>(view) == address);
    CHECK(load_be(view->order_reference) == 42);
    CHECK(trim_ascii(view->stock) == "TEST");
    CHECK(format_price4(1'005'099) == "100.5099");
}

void test_soup_codec_and_glimpse_marker() {
    const auto login = encode_soup_login_request("GLIMP", "SECRET", "", 1, 30'000);
    CHECK(login.size() == 54);
    CHECK(login[0] == std::byte{0} && login[1] == std::byte{52});
    CHECK(login[2] == std::byte{'L'});
    CHECK(std::string(reinterpret_cast<const char*>(login.data() + 3), 6) == "GLIMP ");
    CHECK(std::string(reinterpret_cast<const char*>(login.data() + 9), 10)
          == "SECRET    ");
    CHECK(std::string(reinterpret_cast<const char*>(login.data() + 19), 10)
          == "          ");
    CHECK(std::string(reinterpret_cast<const char*>(login.data() + 29), 20)
          == "                   1");
    CHECK(std::string(reinterpret_cast<const char*>(login.data() + 49), 5) == "30000");
    const auto heartbeat = encode_soup_control('R');
    CHECK(heartbeat[0] == std::byte{0} && heartbeat[1] == std::byte{1}
          && heartbeat[2] == std::byte{'R'});

    std::vector<std::byte> marker(sizeof(EndSnapshotWire), std::byte{' '});
    marker[0] = std::byte{'G'};
    marker.back() = std::byte{'7'};
    MessageDecoder decoder;
    OrderBook book;
    CHECK(decoder.apply(marker, book, DecodeContext::glimpse) == 7);
    check_throws<ProtocolError>([&] {
        (void)decoder.apply(marker, book, DecodeContext::live_itch);
    });
}

void test_ring() {
    DatagramRing ring(4, 512);
    for (std::uint64_t value = 0; value < 4; ++value) {
        auto slot = ring.try_reserve();
        CHECK(slot.has_value());
        store_be(std::span<std::byte>(slot->data, 8), value);
        ring.commit(*slot, 8);
    }
    CHECK(!ring.try_reserve().has_value());
    for (std::uint64_t value = 0; value < 4; ++value) {
        auto slot = ring.try_peek();
        CHECK(slot.has_value());
        Bytes<8> encoded{};
        std::memcpy(encoded.data(), slot->data, encoded.size());
        CHECK(load_be(encoded) == value);
        ring.release(*slot);
    }
    CHECK(ring.empty());

    DatagramRing stress(1024, 64);
    constexpr std::uint64_t count = 100'000;
    std::atomic<bool> failed{false};
    std::thread producer([&] {
        for (std::uint64_t value = 0; value < count;) {
            if (auto slot = stress.try_reserve()) {
                store_be(std::span<std::byte>(slot->data, 8), value);
                stress.commit(*slot, 8);
                ++value;
            } else {
                std::this_thread::yield();
            }
        }
    });
    std::thread consumer([&] {
        for (std::uint64_t expected = 0; expected < count;) {
            if (auto slot = stress.try_peek()) {
                Bytes<8> encoded{};
                std::memcpy(encoded.data(), slot->data, encoded.size());
                if (load_be(encoded) != expected) {
                    failed.store(true);
                }
                stress.release(*slot);
                ++expected;
            } else {
                std::this_thread::yield();
            }
        }
    });
    producer.join();
    consumer.join();
    CHECK(!failed.load());

    ObjectRing<std::uint64_t> objects(2);
    auto produced = objects.try_reserve();
    CHECK(produced.has_value());
    *produced->value = 42;
    objects.commit(*produced);
    produced = objects.try_reserve();
    CHECK(produced.has_value());
    *produced->value = 43;
    objects.commit(*produced);
    CHECK(!objects.try_reserve().has_value());
    auto consumed = objects.try_peek();
    CHECK(consumed.has_value());
    CHECK(*consumed->value == 42);
    objects.release(*consumed);
    consumed = objects.try_peek();
    CHECK(consumed.has_value());
    CHECK(*consumed->value == 43);
    objects.release(*consumed);
    CHECK(objects.empty());
}

void test_book_and_decoder() {
    MessageDecoder decoder;
    OrderBook book;
    auto r = directory(1, "TEST");
    auto h = trading_action(1, "TEST");
    auto first = add_order(1, "TEST", 10, 'B', 5, 1'000'000);
    auto second = add_order(1, "TEST", 11, 'B', 3, 1'000'000);
    (void)decoder.apply(r, book, DecodeContext::glimpse);
    (void)decoder.apply(h, book, DecodeContext::glimpse);
    (void)decoder.apply(first, book, DecodeContext::glimpse);
    (void)decoder.apply(second, book, DecodeContext::glimpse);
    CHECK(book.order_count() == 2);
    auto display = book.display(10, {});
    CHECK(display.size() == 1);
    CHECK(display[0].bids[0].shares == 8);
    CHECK(display[0].bids[0].order_count == 2);

    auto partial = execution(1, 10, 2);
    (void)decoder.apply(partial, book, DecodeContext::live_itch);
    CHECK(book.leaves(10) == 3);
    auto complete = execution(1, 10, 3, true);
    (void)decoder.apply(complete, book, DecodeContext::live_itch);
    CHECK(!book.contains(10));
    auto remove = delete_order(1, 11);
    (void)decoder.apply(remove, book, DecodeContext::live_itch);
    CHECK(book.order_count() == 0);
    CHECK(book.display(10, {})[0].bids.empty());

    auto ask = add_order(1, "TEST", 12, 'S', 9, 1'020'000);
    (void)decoder.apply(ask, book, DecodeContext::live_itch);
    DepthSnapshot depth;
    const FeedStatus status{FeedState::live, "MX00000001", 9, {}};
    CHECK(fill_depth_snapshot(depth, status, book, "TEST", 1));
    CHECK(depth.state == FeedState::live);
    CHECK(depth.symbol_present);
    CHECK(depth.ask_count == 1);
    CHECK(depth.asks[0].price == 1'020'000);
    CHECK(depth.asks[0].shares == 9);

    const FeedStatus recovering{FeedState::recovering, "MX00000001", 10, {}};
    CHECK(fill_depth_snapshot(depth, recovering, book, "TEST", 1));
    CHECK(depth.state == FeedState::recovering);
    CHECK(depth.next_sequence == 10);
    CHECK(!fill_depth_snapshot(depth, status, book, "MISSING", 1));
    CHECK(!depth.symbol_present);
    CHECK(depth.trading_state == '?');

    check_throws<ProtocolError>([&] {
        auto unknown = execution(1, 99, 1);
        (void)decoder.apply(unknown, book, DecodeContext::live_itch);
    });
}

void test_stitching_and_framing() {
    MessageDecoder decoder;
    OrderBook book;
    auto r = directory(1, "TEST");
    auto a = add_order(1, "TEST", 42, 'S', 3, 1'010'000);
    (void)decoder.apply(r, book, DecodeContext::glimpse);
    (void)decoder.apply(a, book, DecodeContext::glimpse);

    auto stale_delete = delete_order(1, 42);
    auto current_execution = execution(1, 42, 2);
    auto packet = mold_packet(
        "MX00000001", 4, {stale_delete, current_execution});
    std::uint64_t expected = 5;
    const auto info = parse_mold_packet(
        packet,
        [&](std::uint64_t sequence, std::span<std::byte> message) {
            if (sequence < expected) {
                return;
            }
            CHECK(sequence == expected);
            (void)decoder.apply(message, book, DecodeContext::live_itch);
            ++expected;
        });
    CHECK(info.first_sequence == 4);
    CHECK(expected == 6);
    CHECK(book.leaves(42) == 1);

    auto heartbeat = mold_packet("MX00000001", 6, {}, 0);
    auto heartbeat_info = parse_mold_packet(
        heartbeat, [](std::uint64_t, std::span<std::byte>) {});
    CHECK(heartbeat_info.heartbeat());
    auto end = mold_packet("MX00000001", 6, {}, 0xffffU);
    CHECK(parse_mold_packet(end, [](std::uint64_t, std::span<std::byte>) {})
              .end_of_session());

    auto closing = system_event('C');
    CHECK(is_end_of_messages(closing));
    (void)decoder.apply(closing, book, DecodeContext::live_itch);
    auto opening = system_event('O');
    CHECK(!is_end_of_messages(opening));
    (void)decoder.apply(opening, book, DecodeContext::live_itch);

    auto truncated = packet;
    truncated.pop_back();
    check_throws<ProtocolError>([&] {
        (void)parse_mold_packet(
            truncated, [](std::uint64_t, std::span<std::byte>) {});
    });

    std::vector<std::byte> unsupported(1, std::byte{'U'});
    auto unsupported_packet = mold_packet("MX00000001", 6, {unsupported});
    check_throws<UnsupportedMessage>([&] {
        (void)parse_mold_packet(
            unsupported_packet,
            [&](std::uint64_t, std::span<std::byte> message) {
                (void)decoder.apply(message, book, DecodeContext::live_itch);
            });
    });
}

void test_cli() {
    const char* argv[] = {
        "mockx_market_feed",
        "--password",
        "SECRET",
        "--ring-capacity",
        "8",
        "--symbol",
        "TEST"};
    auto command = parse_command_line(
        static_cast<int>(std::size(argv)), const_cast<char**>(argv));
    CHECK(command.config.password == "SECRET");
    CHECK(command.config.ring_capacity == 8);
    CHECK(command.config.symbols.contains("TEST"));
}

void test_partial_cancel_preserves_identity_and_validates_before_mutation() {
    OrderBook book;
    MessageDecoder decoder;
    auto instrument = directory(1, "TEST");
    (void)decoder.apply(instrument, book, DecodeContext::live_itch);
    auto first = add_order(1, "TEST", 42, 'B', 10, 1'000'000);
    auto second = add_order(1, "TEST", 43, 'B', 5, 1'000'000);
    (void)decoder.apply(first, book, DecodeContext::live_itch);
    (void)decoder.apply(second, book, DecodeContext::live_itch);
    std::vector<std::byte> reduction(23);
    reduction[0] = std::byte{'X'};
    set_be(reduction, 1, 2, 1);
    set_be(reduction, 11, 8, 42);
    set_be(reduction, 19, 4, 3);
    (void)decoder.apply(reduction, book, DecodeContext::live_itch);
    CHECK(book.leaves(42) == 7);
    CHECK(book.leaves(43) == 5);
    CHECK(book.display(1, {})[0].bids[0].order_count == 2);
    CHECK(book.display(1, {})[0].bids[0].shares == 12);
    check_throws<ProtocolError>([&] {
        (void)decoder.apply(reduction, book, DecodeContext::glimpse);
    });
    set_be(reduction, 19, 4, 8);
    check_throws<ProtocolError>([&] {
        (void)decoder.apply(reduction, book, DecodeContext::live_itch);
    });
    CHECK(book.leaves(42) == 7);
    set_be(reduction, 19, 4, 0);
    check_throws<ProtocolError>([&] {
        (void)decoder.apply(reduction, book, DecodeContext::live_itch);
    });
    set_be(reduction, 19, 4, 1);
    set_be(reduction, 1, 2, 2);
    check_throws<ProtocolError>([&] {
        (void)decoder.apply(reduction, book, DecodeContext::live_itch);
    });
    set_be(reduction, 1, 2, 1);
    set_be(reduction, 11, 8, 99);
    check_throws<ProtocolError>([&] {
        (void)decoder.apply(reduction, book, DecodeContext::live_itch);
    });
    set_be(reduction, 11, 8, 42);
    set_be(reduction, 19, 4, 7);
    (void)decoder.apply(reduction, book, DecodeContext::live_itch);
    CHECK(!book.contains(42));
    CHECK(book.leaves(43) == 5);
    CHECK(book.display(1, {})[0].bids[0].order_count == 1);
    reduction.pop_back();
    check_throws<ProtocolError>([&] {
        (void)decoder.apply(reduction, book, DecodeContext::live_itch);
    });
}

} // namespace

int main() {
    const std::vector<std::pair<std::string, std::function<void()>>> tests = {
        {"wire views", test_wire_views},
        {"Soup and GLIMPSE codec", test_soup_codec_and_glimpse_marker},
        {"SPSC ring", test_ring},
        {"book and decoder", test_book_and_decoder},
        {"snapshot stitching", test_stitching_and_framing},
        {"command line", test_cli},
        {"partial cancel", test_partial_cancel_preserves_identity_and_validates_before_mutation},
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

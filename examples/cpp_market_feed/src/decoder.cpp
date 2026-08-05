#include "mockx_feed/decoder.hpp"

#include <string_view>

namespace mockx::feed {

std::uint64_t MessageDecoder::apply(
    std::span<std::byte> message,
    OrderBook& book,
    DecodeContext context) const {
    validate_itch_message(message);
    const auto type = static_cast<char>(std::to_integer<unsigned char>(message.front()));

    switch (type) {
    case 'S': {
        if (context != DecodeContext::live_itch) {
            throw ProtocolError("system event is not valid in a GLIMPSE spin");
        }
        auto* wire = overlay<SystemEventWire>(message.data());
        const std::string_view valid_codes("OSQMEC");
        if (valid_codes.find(wire->event_code) == std::string_view::npos) {
            throw ProtocolError("invalid ITCH system-event code");
        }
        return 0;
    }
    case 'R': {
        auto* wire = overlay<StockDirectoryWire>(message.data());
        const auto locate = static_cast<std::uint16_t>(load_be(wire->stock_locate));
        book.register_instrument(locate, trim_ascii(wire->stock));
        return 0;
    }
    case 'H': {
        auto* wire = overlay<StockTradingActionWire>(message.data());
        const auto locate = static_cast<std::uint16_t>(load_be(wire->stock_locate));
        if (wire->trading_state != 'H' && wire->trading_state != 'P'
            && wire->trading_state != 'Q' && wire->trading_state != 'T') {
            throw ProtocolError("invalid ITCH trading state");
        }
        book.set_trading_state(locate, trim_ascii(wire->stock), wire->trading_state);
        return 0;
    }
    case 'A': {
        auto* wire = overlay<AddOrderWire>(message.data());
        Side side;
        if (wire->side == 'B') {
            side = Side::buy;
        } else if (wire->side == 'S') {
            side = Side::sell;
        } else {
            throw ProtocolError("invalid add-order side");
        }
        book.add_order(
            load_be(wire->order_reference),
            static_cast<std::uint16_t>(load_be(wire->stock_locate)),
            trim_ascii(wire->stock),
            side,
            static_cast<std::uint32_t>(load_be(wire->shares)),
            static_cast<std::uint32_t>(load_be(wire->price)));
        return 0;
    }
    case 'E': {
        if (context != DecodeContext::live_itch) {
            throw ProtocolError("execution is not valid in a GLIMPSE spin");
        }
        auto* wire = overlay<OrderExecutedWire>(message.data());
        book.execute_order(
            load_be(wire->order_reference),
            static_cast<std::uint16_t>(load_be(wire->stock_locate)),
            static_cast<std::uint32_t>(load_be(wire->executed_shares)));
        return 0;
    }
    case 'C': {
        if (context != DecodeContext::live_itch) {
            throw ProtocolError("priced execution is not valid in a GLIMPSE spin");
        }
        auto* wire = overlay<OrderExecutedWithPriceWire>(message.data());
        if (wire->printable != 'Y' && wire->printable != 'N') {
            throw ProtocolError("invalid printable flag");
        }
        book.execute_order(
            load_be(wire->order_reference),
            static_cast<std::uint16_t>(load_be(wire->stock_locate)),
            static_cast<std::uint32_t>(load_be(wire->executed_shares)));
        return 0;
    }
    case 'D': {
        if (context != DecodeContext::live_itch) {
            throw ProtocolError("delete is not valid in a GLIMPSE spin");
        }
        auto* wire = overlay<OrderDeleteWire>(message.data());
        book.delete_order(
            load_be(wire->order_reference),
            static_cast<std::uint16_t>(load_be(wire->stock_locate)));
        return 0;
    }
    case 'G': {
        if (context != DecodeContext::glimpse) {
            throw ProtocolError("GLIMPSE marker appeared in the live ITCH feed");
        }
        auto* wire = overlay<EndSnapshotWire>(message.data());
        const auto sequence = parse_padded_decimal(std::string_view(
            wire->next_itch_sequence.data(), wire->next_itch_sequence.size()));
        if (sequence == 0) {
            throw ProtocolError("GLIMPSE marker sequence must be positive");
        }
        return sequence;
    }
    default:
        throw UnsupportedMessage(std::string("unsupported ITCH message type: ") + type);
    }
}

bool is_end_of_messages(std::span<std::byte> message) {
    if (message.empty()
        || static_cast<char>(std::to_integer<unsigned char>(message.front())) != 'S') {
        return false;
    }
    validate_itch_message(message);
    return overlay<SystemEventWire>(message.data())->event_code == 'C';
}

} // namespace mockx::feed

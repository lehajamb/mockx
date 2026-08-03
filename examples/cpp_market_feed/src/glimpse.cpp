#include "mockx_feed/glimpse.hpp"

#include "mockx_feed/protocol.hpp"
#include "mockx_feed/soup.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <span>
#include <thread>

namespace mockx::feed {

GlimpseSnapshot GlimpseClient::capture() const {
    MessageDecoder decoder;
    OrderBook candidate;
    std::string requested_session;
    std::uint64_t next_soup_sequence = 1;
    std::uint64_t marker = 0;
    std::uint32_t failures = 0;

    while (failures <= config_.transport_retries) {
        try {
            auto socket = TcpSocket::connect(config_.host, config_.port);
            const auto login = encode_soup_login_request(
                config_.username,
                config_.password,
                requested_session,
                next_soup_sequence,
                config_.heartbeat_timeout_ms);
            socket.send_all(login);

            auto response = read_soup_packet(socket);
            if (response.type == 'J') {
                if (response.payload.size() != 1) {
                    throw ProtocolError("Soup Login Rejected payload must be one byte");
                }
                const auto reason = static_cast<char>(
                    std::to_integer<unsigned char>(response.payload.front()));
                if (reason == 'A') {
                    throw AuthenticationError("GLIMPSE authentication rejected");
                }
                if (reason != 'S') {
                    throw ProtocolError("unknown Soup login-rejection reason");
                }
                if (!requested_session.empty()) {
                    requested_session.clear();
                    next_soup_sequence = 1;
                    marker = 0;
                    candidate.clear();
                    ++failures;
                    continue;
                }
                throw GlimpseUnavailable("GLIMPSE snapshot session is unavailable");
            }
            if (response.type != 'A' || response.payload.size() != 30) {
                throw ProtocolError("expected a 30-byte Soup Login Accepted payload");
            }
            const auto accepted_session = decode_soup_session(
                std::span<const std::byte>(response.payload).first(10));
            const auto accepted_sequence = decode_soup_decimal(
                std::span<const std::byte>(response.payload).subspan(10));
            if (accepted_session.empty() || accepted_sequence != next_soup_sequence) {
                throw ProtocolError("Soup Login Accepted disagrees with requested sequence");
            }
            requested_session = accepted_session;

            auto last_client_heartbeat = std::chrono::steady_clock::now();
            const auto heartbeat_period = std::chrono::milliseconds(
                std::min<std::uint32_t>(1'000, config_.heartbeat_timeout_ms / 2));
            for (;;) {
                auto packet = read_soup_packet(socket);
                const auto now = std::chrono::steady_clock::now();
                if (now - last_client_heartbeat >= heartbeat_period) {
                    const auto heartbeat = encode_soup_control('R');
                    socket.send_all(heartbeat);
                    last_client_heartbeat = now;
                }

                if (packet.type == 'H') {
                    if (!packet.payload.empty()) {
                        throw ProtocolError("Soup heartbeat must not have a payload");
                    }
                    continue;
                }
                if (packet.type == '+' || packet.type == 'U') {
                    continue;
                }
                if (packet.type == 'Z') {
                    if (!packet.payload.empty() || marker == 0) {
                        throw ProtocolError("Soup ended before a complete GLIMPSE marker");
                    }
                    return GlimpseSnapshot{std::move(candidate), marker};
                }
                if (packet.type != 'S') {
                    throw ProtocolError("unexpected Soup packet during GLIMPSE spin");
                }
                if (marker != 0) {
                    throw ProtocolError("sequenced data followed the GLIMPSE marker");
                }
                if (packet.payload.empty()) {
                    throw ProtocolError("Soup Sequenced Data payload is empty");
                }
                const auto type = static_cast<char>(
                    std::to_integer<unsigned char>(packet.payload.front()));
                if (type != 'R' && type != 'H' && type != 'A' && type != 'G') {
                    throw UnsupportedMessage(
                        std::string("unsupported GLIMPSE message type: ") + type);
                }
                const auto decoded = decoder.apply(packet.payload, candidate, DecodeContext::glimpse);
                ++next_soup_sequence;
                if (decoded != 0) {
                    marker = decoded;
                }
            }
        } catch (const AuthenticationError&) {
            throw;
        } catch (const GlimpseUnavailable&) {
            throw;
        } catch (const NetworkError&) {
            if (++failures > config_.transport_retries) {
                throw GlimpseUnavailable("GLIMPSE transport retries exhausted");
            }
            const auto delay = std::chrono::milliseconds(50U << std::min(failures, 5U));
            std::this_thread::sleep_for(delay);
        }
    }
    throw GlimpseUnavailable("GLIMPSE transport retries exhausted");
}

} // namespace mockx::feed

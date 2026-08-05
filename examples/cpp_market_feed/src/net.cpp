#include "mockx_feed/net.hpp"

#include <algorithm>
#include <array>
#include <cstring>
#include <limits>
#include <sstream>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <cerrno>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#endif

namespace mockx::feed {
namespace {

#ifdef _WIN32
using NativeSocket = SOCKET;
constexpr NativeSocket invalid_socket = INVALID_SOCKET;
NativeSocket native(std::intptr_t handle) { return static_cast<NativeSocket>(handle); }
std::intptr_t stored(NativeSocket handle) { return static_cast<std::intptr_t>(handle); }
int last_socket_error() { return WSAGetLastError(); }
void close_socket(NativeSocket handle) { closesocket(handle); }
bool interrupted(int error) { return error == WSAEINTR; }
bool timed_out(int error) { return error == WSAETIMEDOUT || error == WSAEWOULDBLOCK; }
std::string socket_error_text(int error) {
    return "Winsock error " + std::to_string(error);
}
#else
using NativeSocket = int;
constexpr NativeSocket invalid_socket = -1;
NativeSocket native(std::intptr_t handle) { return static_cast<NativeSocket>(handle); }
std::intptr_t stored(NativeSocket handle) { return static_cast<std::intptr_t>(handle); }
int last_socket_error() { return errno; }
void close_socket(NativeSocket handle) { ::close(handle); }
bool interrupted(int error) { return error == EINTR; }
bool timed_out(int error) { return error == EAGAIN || error == EWOULDBLOCK; }
std::string socket_error_text(int error) { return std::strerror(error); }
#endif

[[noreturn]] void throw_socket_error(const std::string& operation) {
    const auto error = last_socket_error();
    throw NetworkError(operation + ": " + socket_error_text(error));
}

void set_reuse_address(NativeSocket handle) {
    int enabled = 1;
    if (::setsockopt(
            handle,
            SOL_SOCKET,
            SO_REUSEADDR,
            reinterpret_cast<const char*>(&enabled),
            sizeof(enabled)) != 0) {
        throw_socket_error("setsockopt(SO_REUSEADDR)");
    }
}

void set_receive_timeout(NativeSocket handle, std::uint32_t timeout_ms) {
#ifdef _WIN32
    const DWORD timeout = timeout_ms;
    if (::setsockopt(
            handle,
            SOL_SOCKET,
            SO_RCVTIMEO,
            reinterpret_cast<const char*>(&timeout),
            sizeof(timeout)) != 0) {
        throw_socket_error("setsockopt(SO_RCVTIMEO)");
    }
#else
    timeval timeout{
        static_cast<long>(timeout_ms / 1'000U),
        static_cast<long>((timeout_ms % 1'000U) * 1'000U)};
    if (::setsockopt(handle, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0) {
        throw_socket_error("setsockopt(SO_RCVTIMEO)");
    }
#endif
}

} // namespace

NetworkRuntime::NetworkRuntime() {
#ifdef _WIN32
    WSADATA data{};
    const auto result = WSAStartup(MAKEWORD(2, 2), &data);
    if (result != 0) {
        throw NetworkError("WSAStartup: Winsock error " + std::to_string(result));
    }
#endif
}

NetworkRuntime::~NetworkRuntime() {
#ifdef _WIN32
    WSACleanup();
#endif
}

UdpMulticastSocket::UdpMulticastSocket(
    const std::string& group,
    std::uint16_t port,
    const std::string& interface_address) {
    const auto handle = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (handle == invalid_socket) {
        throw_socket_error("socket(UDP)");
    }
    handle_ = stored(handle);
    try {
        set_reuse_address(handle);
        set_receive_timeout(handle, 200);
        int receive_buffer = 4 * 1024 * 1024;
        (void)::setsockopt(
            handle,
            SOL_SOCKET,
            SO_RCVBUF,
            reinterpret_cast<const char*>(&receive_buffer),
            sizeof(receive_buffer));

        sockaddr_in local{};
        local.sin_family = AF_INET;
        local.sin_port = htons(port);
        local.sin_addr.s_addr = htonl(INADDR_ANY);
        if (::bind(
                handle,
                reinterpret_cast<const sockaddr*>(&local),
                sizeof(local)) != 0) {
            throw_socket_error("bind(UDP)");
        }

        ip_mreq membership{};
        if (::inet_pton(AF_INET, group.c_str(), &membership.imr_multiaddr) != 1) {
            throw NetworkError("invalid IPv4 multicast group: " + group);
        }
        if (::inet_pton(
                AF_INET,
                interface_address.c_str(),
                &membership.imr_interface) != 1) {
            throw NetworkError("invalid IPv4 interface address: " + interface_address);
        }
        if (::setsockopt(
                handle,
                IPPROTO_IP,
                IP_ADD_MEMBERSHIP,
                reinterpret_cast<const char*>(&membership),
                sizeof(membership)) != 0) {
            throw_socket_error("setsockopt(IP_ADD_MEMBERSHIP)");
        }
    } catch (...) {
        close();
        throw;
    }
}

UdpMulticastSocket::~UdpMulticastSocket() { close(); }

ReceiveResult UdpMulticastSocket::receive(std::span<std::byte> destination) {
    if (handle_ == -1 || destination.empty()) {
        throw NetworkError("receive called with a closed socket or empty buffer");
    }
    for (;;) {
#ifdef _WIN32
        const auto maximum = static_cast<int>(std::min<std::size_t>(
            destination.size(), static_cast<std::size_t>(std::numeric_limits<int>::max())));
        const auto received = ::recvfrom(
            native(handle_),
            reinterpret_cast<char*>(destination.data()),
            maximum,
            0,
            nullptr,
            nullptr);
        if (received != SOCKET_ERROR) {
            return {ReceiveStatus::data, static_cast<std::size_t>(received)};
        }
        const auto error = last_socket_error();
        if (error == WSAEMSGSIZE) {
            return {ReceiveStatus::truncated, 0};
        }
#else
        iovec vector{destination.data(), destination.size()};
        msghdr message{};
        message.msg_iov = &vector;
        message.msg_iovlen = 1;
        const auto received = ::recvmsg(native(handle_), &message, MSG_TRUNC);
        if (received >= 0) {
            if (static_cast<std::size_t>(received) > destination.size()
                || (message.msg_flags & MSG_TRUNC) != 0) {
                return {ReceiveStatus::truncated, 0};
            }
            return {ReceiveStatus::data, static_cast<std::size_t>(received)};
        }
        const auto error = last_socket_error();
#endif
        if (interrupted(error)) {
            continue;
        }
        if (timed_out(error)) {
            return {ReceiveStatus::timeout, 0};
        }
        throw NetworkError("recvfrom(UDP): " + socket_error_text(error));
    }
}

void UdpMulticastSocket::close() noexcept {
    if (handle_ != -1) {
        close_socket(native(handle_));
        handle_ = -1;
    }
}

TcpSocket::~TcpSocket() { close(); }

TcpSocket::TcpSocket(TcpSocket&& other) noexcept
    : handle_(std::exchange(other.handle_, -1)) {}

TcpSocket& TcpSocket::operator=(TcpSocket&& other) noexcept {
    if (this != &other) {
        close();
        handle_ = std::exchange(other.handle_, -1);
    }
    return *this;
}

TcpSocket TcpSocket::connect(const std::string& host, std::uint16_t port) {
    addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    addrinfo* addresses = nullptr;
    const auto service = std::to_string(port);
    const auto lookup = ::getaddrinfo(host.c_str(), service.c_str(), &hints, &addresses);
    if (lookup != 0) {
#ifdef _WIN32
        throw NetworkError("getaddrinfo: " + std::to_string(lookup));
#else
        throw NetworkError(std::string("getaddrinfo: ") + gai_strerror(lookup));
#endif
    }

    NativeSocket connected = invalid_socket;
    for (auto* address = addresses; address != nullptr; address = address->ai_next) {
        const auto candidate = ::socket(
            address->ai_family, address->ai_socktype, address->ai_protocol);
        if (candidate == invalid_socket) {
            continue;
        }
        if (::connect(candidate, address->ai_addr, static_cast<int>(address->ai_addrlen))
            == 0) {
            connected = candidate;
            break;
        }
        close_socket(candidate);
    }
    ::freeaddrinfo(addresses);
    if (connected == invalid_socket) {
        throw_socket_error("connect(TCP)");
    }
    // MockX sends a Soup server heartbeat after one idle second. A two-second
    // socket timeout preserves responsiveness while allowing that heartbeat.
    set_receive_timeout(connected, 2'000);
    return TcpSocket(stored(connected));
}

void TcpSocket::send_all(std::span<const std::byte> data) {
    std::size_t offset = 0;
    while (offset < data.size()) {
        const auto remaining = std::min<std::size_t>(
            data.size() - offset,
            static_cast<std::size_t>(std::numeric_limits<int>::max()));
#ifdef _WIN32
        const auto sent = ::send(
            native(handle_),
            reinterpret_cast<const char*>(data.data() + offset),
            static_cast<int>(remaining),
            0);
        if (sent != SOCKET_ERROR) {
#else
        const auto sent = ::send(
            native(handle_), data.data() + offset, remaining, 0);
        if (sent >= 0) {
#endif
            if (sent == 0) {
                throw NetworkError("TCP peer closed during send");
            }
            offset += static_cast<std::size_t>(sent);
            continue;
        }
        const auto error = last_socket_error();
        if (!interrupted(error)) {
            throw NetworkError("send(TCP): " + socket_error_text(error));
        }
    }
}

void TcpSocket::read_exact(std::span<std::byte> destination) {
    std::size_t offset = 0;
    while (offset < destination.size()) {
        const auto remaining = std::min<std::size_t>(
            destination.size() - offset,
            static_cast<std::size_t>(std::numeric_limits<int>::max()));
#ifdef _WIN32
        const auto received = ::recv(
            native(handle_),
            reinterpret_cast<char*>(destination.data() + offset),
            static_cast<int>(remaining),
            0);
        if (received != SOCKET_ERROR) {
#else
        const auto received = ::recv(native(handle_), destination.data() + offset, remaining, 0);
        if (received >= 0) {
#endif
            if (received == 0) {
                throw NetworkError("TCP peer closed during read");
            }
            offset += static_cast<std::size_t>(received);
            continue;
        }
        const auto error = last_socket_error();
        if (interrupted(error)) {
            continue;
        }
        if (timed_out(error)) {
            // Soup server heartbeats normally prevent this; represent a timeout
            // as a retryable transport failure for the GLIMPSE resume path.
            throw NetworkError("TCP read timed out");
        }
        throw NetworkError("recv(TCP): " + socket_error_text(error));
    }
}

void TcpSocket::close() noexcept {
    if (handle_ != -1) {
        close_socket(native(handle_));
        handle_ = -1;
    }
}

} // namespace mockx::feed

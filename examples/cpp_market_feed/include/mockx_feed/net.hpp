#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <string>

namespace mockx::feed {

class NetworkError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class NetworkRuntime {
public:
    NetworkRuntime();
    ~NetworkRuntime();
    NetworkRuntime(const NetworkRuntime&) = delete;
    NetworkRuntime& operator=(const NetworkRuntime&) = delete;
};

enum class ReceiveStatus { data, timeout, truncated };

struct ReceiveResult {
    ReceiveStatus status{ReceiveStatus::timeout};
    std::size_t length{};
};

class UdpMulticastSocket {
public:
    UdpMulticastSocket(
        const std::string& group,
        std::uint16_t port,
        const std::string& interface_address);
    ~UdpMulticastSocket();
    UdpMulticastSocket(const UdpMulticastSocket&) = delete;
    UdpMulticastSocket& operator=(const UdpMulticastSocket&) = delete;

    [[nodiscard]] ReceiveResult receive(std::span<std::byte> destination);
    void close() noexcept;

private:
    std::intptr_t handle_{-1};
};

class TcpSocket {
public:
    TcpSocket() = default;
    ~TcpSocket();
    TcpSocket(const TcpSocket&) = delete;
    TcpSocket& operator=(const TcpSocket&) = delete;
    TcpSocket(TcpSocket&& other) noexcept;
    TcpSocket& operator=(TcpSocket&& other) noexcept;

    static TcpSocket connect(const std::string& host, std::uint16_t port);
    void send_all(std::span<const std::byte> data);
    void read_exact(std::span<std::byte> destination);
    void close() noexcept;
    [[nodiscard]] bool valid() const noexcept { return handle_ != -1; }

private:
    explicit TcpSocket(std::intptr_t handle) : handle_(handle) {}
    std::intptr_t handle_{-1};
};

} // namespace mockx::feed

#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <new>
#include <limits>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

namespace mockx::feed {

inline constexpr std::size_t cache_line_size = 64;
static_assert(sizeof(void*) == 8, "MockX market-feed example requires a 64-bit target");

[[nodiscard]] inline bool is_power_of_two(std::size_t value) noexcept {
    return value >= 2 && (value & (value - 1U)) == 0;
}

class DatagramRing {
public:
    struct ProducerSlot {
        std::byte* data{};
        std::size_t capacity{};
        std::uint64_t ticket{};
    };

    struct ConsumerSlot {
        std::byte* data{};
        std::size_t length{};
        std::uint64_t ticket{};
    };

    DatagramRing(std::size_t capacity, std::size_t datagram_bytes)
        : capacity_(checked_capacity(capacity)), mask_(capacity_ - 1U),
          datagram_bytes_(checked_datagram_size(datagram_bytes)),
          stride_((datagram_bytes_ + cache_line_size - 1U) & ~(cache_line_size - 1U)),
          lengths_(capacity_, 0) {
        if (!head_.is_lock_free() || !tail_.is_lock_free()) {
            throw std::runtime_error("platform does not provide lock-free 64-bit atomics");
        }
        if (capacity_ > std::numeric_limits<std::size_t>::max() / stride_) {
            throw std::invalid_argument("ring allocation size overflows size_t");
        }
        const auto bytes = capacity_ * stride_;
        storage_ = static_cast<std::byte*>(
            ::operator new[](bytes, std::align_val_t{cache_line_size}));
    }

    DatagramRing(const DatagramRing&) = delete;
    DatagramRing& operator=(const DatagramRing&) = delete;

    ~DatagramRing() {
        ::operator delete[](storage_, std::align_val_t{cache_line_size});
    }

    [[nodiscard]] std::optional<ProducerSlot> try_reserve() noexcept {
        const auto head = head_.load(std::memory_order_relaxed);
        const auto tail = tail_.load(std::memory_order_acquire);
        if (head - tail >= capacity_) {
            return std::nullopt;
        }
        const auto index = static_cast<std::size_t>(head) & mask_;
        return ProducerSlot{storage_ + index * stride_, datagram_bytes_, head};
    }

    void commit(const ProducerSlot& slot, std::size_t length) {
        const auto head = head_.load(std::memory_order_relaxed);
        if (slot.ticket != head || length > datagram_bytes_) {
            throw std::logic_error("invalid producer commit");
        }
        lengths_[static_cast<std::size_t>(head) & mask_] = length;
        head_.store(head + 1U, std::memory_order_release);
    }

    [[nodiscard]] std::optional<ConsumerSlot> try_peek() noexcept {
        const auto tail = tail_.load(std::memory_order_relaxed);
        if (tail == head_.load(std::memory_order_acquire)) {
            return std::nullopt;
        }
        const auto index = static_cast<std::size_t>(tail) & mask_;
        return ConsumerSlot{storage_ + index * stride_, lengths_[index], tail};
    }

    void release(const ConsumerSlot& slot) {
        const auto tail = tail_.load(std::memory_order_relaxed);
        if (slot.ticket != tail) {
            throw std::logic_error("invalid consumer release");
        }
        tail_.store(tail + 1U, std::memory_order_release);
    }

    [[nodiscard]] bool empty() const noexcept {
        return tail_.load(std::memory_order_relaxed)
            == head_.load(std::memory_order_acquire);
    }

    [[nodiscard]] std::size_t size() const noexcept {
        const auto head = head_.load(std::memory_order_acquire);
        const auto tail = tail_.load(std::memory_order_acquire);
        return static_cast<std::size_t>(head - tail);
    }

    [[nodiscard]] std::size_t capacity() const noexcept { return capacity_; }
    [[nodiscard]] std::size_t datagram_bytes() const noexcept { return datagram_bytes_; }

private:
    static std::size_t checked_capacity(std::size_t capacity) {
        if (!is_power_of_two(capacity)) {
            throw std::invalid_argument("ring capacity must be a power of two and at least 2");
        }
        return capacity;
    }

    static std::size_t checked_datagram_size(std::size_t datagram_bytes) {
        if (datagram_bytes < 20
            || datagram_bytes > std::numeric_limits<std::size_t>::max()
                - cache_line_size) {
            throw std::invalid_argument("invalid datagram slot size");
        }
        return datagram_bytes;
    }

    const std::size_t capacity_;
    const std::size_t mask_;
    const std::size_t datagram_bytes_;
    const std::size_t stride_;
    std::vector<std::size_t> lengths_;
    std::byte* storage_{};
    alignas(cache_line_size) std::atomic<std::uint64_t> head_{0};
    alignas(cache_line_size) std::atomic<std::uint64_t> tail_{0};
};

template <typename T>
class ObjectRing {
public:
    explicit ObjectRing(std::size_t capacity)
        : capacity_(checked_capacity(capacity)), mask_(capacity_ - 1U), slots_(capacity_) {
        if (!head_.is_lock_free() || !tail_.is_lock_free()) {
            throw std::runtime_error("platform does not provide lock-free 64-bit atomics");
        }
    }

    ObjectRing(const ObjectRing&) = delete;
    ObjectRing& operator=(const ObjectRing&) = delete;

    [[nodiscard]] bool try_push(T value) {
        const auto head = head_.load(std::memory_order_relaxed);
        if (head - tail_.load(std::memory_order_acquire) >= capacity_) {
            return false;
        }
        slots_[static_cast<std::size_t>(head) & mask_] = std::move(value);
        head_.store(head + 1U, std::memory_order_release);
        return true;
    }

    [[nodiscard]] bool try_pop(T& output) {
        const auto tail = tail_.load(std::memory_order_relaxed);
        if (tail == head_.load(std::memory_order_acquire)) {
            return false;
        }
        output = std::move(slots_[static_cast<std::size_t>(tail) & mask_]);
        tail_.store(tail + 1U, std::memory_order_release);
        return true;
    }

private:
    static std::size_t checked_capacity(std::size_t capacity) {
        if (!is_power_of_two(capacity)) {
            throw std::invalid_argument("ring capacity must be a power of two and at least 2");
        }
        return capacity;
    }

    const std::size_t capacity_;
    const std::size_t mask_;
    std::vector<T> slots_;
    alignas(cache_line_size) std::atomic<std::uint64_t> head_{0};
    alignas(cache_line_size) std::atomic<std::uint64_t> tail_{0};
};

} // namespace mockx::feed

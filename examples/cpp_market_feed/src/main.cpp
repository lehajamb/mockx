#include "mockx_feed/app.hpp"

#include <atomic>
#include <csignal>
#include <exception>
#include <iostream>

namespace {
std::atomic<bool> stop_requested{false};

extern "C" void handle_signal(int) {
    stop_requested.store(true, std::memory_order_release);
}
} // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, handle_signal);
#ifdef SIGTERM
    std::signal(SIGTERM, handle_signal);
#endif
    try {
        const auto command = mockx::feed::parse_command_line(argc, argv);
        if (command.help) {
            std::cout << mockx::feed::usage();
            return 0;
        }
        if (command.version) {
            std::cout << "mockx_market_feed 0.1.0\n";
            return 0;
        }
        return mockx::feed::run_market_feed(command.config, stop_requested);
    } catch (const mockx::feed::CommandLineError& error) {
        std::cerr << error.what() << "\n\n" << mockx::feed::usage();
        return 2;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 4;
    }
}

#pragma once

#include "mockx_sor/decimal.hpp"
#include "mockx_sor/router.hpp"

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>

namespace mockx::sor {

class CommandLineError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct CommandLine {
    std::string config_path;
    ParentOrder parent;
    Decimal lot_size;
    std::size_t depth{16};
    std::uint32_t sync_timeout_ms{5'000};
    std::uint32_t report_timeout_ms{5'000};
    bool help{};
    bool version{};
};

[[nodiscard]] CommandLine parse_command_line(int argc, char** argv);
[[nodiscard]] std::string usage();
[[nodiscard]] int run_sor(const CommandLine& command);

} // namespace mockx::sor

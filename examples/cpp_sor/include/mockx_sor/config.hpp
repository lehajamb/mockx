#pragma once

#include "mockx_feed/app.hpp"

#include <quickfix/SessionID.h>
#include <quickfix/SessionSettings.h>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace mockx::sor {

class ConfigError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct VenueDefinition {
    std::string venue_id;
    std::uint8_t priority{};
    FIX::SessionID session_id;
    mockx::feed::AppConfig feed;
};

class LoadedConfig {
public:
    explicit LoadedConfig(const std::string& path);

    FIX::SessionSettings fix_settings;
    std::vector<VenueDefinition> venues;
};

} // namespace mockx::sor

#include "mockx_sor/config.hpp"

#include <algorithm>
#include <charconv>
#include <cstdlib>
#include <limits>

namespace mockx::sor {
namespace {

template <typename T>
T parse_unsigned(const std::string& text, const std::string& field) {
    std::uint64_t value = 0;
    const auto [end, error] = std::from_chars(
        text.data(), text.data() + text.size(), value);
    if (error != std::errc{} || end != text.data() + text.size()
        || value > std::numeric_limits<T>::max()) {
        throw ConfigError(field + " must be an unsigned integer in range");
    }
    return static_cast<T>(value);
}

bool printable_non_space(const std::string& value) {
    return std::all_of(value.begin(), value.end(), [](unsigned char ch) {
        return ch >= 0x21U && ch <= 0x7eU;
    });
}

std::string required(const FIX::Dictionary& dictionary, const char* key) {
    try {
        const auto value = dictionary.getString(key);
        if (value.empty()) {
            throw ConfigError(std::string(key) + " must not be empty");
        }
        return value;
    } catch (const ConfigError&) {
        throw;
    } catch (const std::exception& error) {
        throw ConfigError(std::string("missing or invalid ") + key + ": " + error.what());
    }
}

VenueDefinition load_venue(
    const FIX::SessionSettings& settings,
    const FIX::SessionID& session_id) {
    const auto dictionary = settings.get(session_id);
    VenueDefinition venue;
    venue.session_id = session_id;
    venue.venue_id = required(dictionary, "VenueID");
    venue.priority = parse_unsigned<std::uint8_t>(
        required(dictionary, "VenuePriority"), "VenuePriority");
    if (venue.venue_id.size() > 8 || !printable_non_space(venue.venue_id)) {
        throw ConfigError("VenueID must contain 1-8 printable non-space bytes");
    }
    if (venue.priority >= venue_count) {
        throw ConfigError("VenuePriority must be 0 or 1");
    }

    venue.feed.itch_group = required(dictionary, "ItchGroup");
    venue.feed.itch_port = parse_unsigned<std::uint16_t>(
        required(dictionary, "ItchPort"), "ItchPort");
    venue.feed.interface_address = required(dictionary, "ItchInterface");
    venue.feed.session_prefix = required(dictionary, "MoldSessionPrefix");
    venue.feed.glimpse_host = required(dictionary, "GlimpseHost");
    venue.feed.glimpse_port = parse_unsigned<std::uint16_t>(
        required(dictionary, "GlimpsePort"), "GlimpsePort");
    venue.feed.username = required(dictionary, "GlimpseUsername");
    const auto password_environment = required(dictionary, "GlimpsePasswordEnv");
    const auto* password = std::getenv(password_environment.c_str());
    if (password == nullptr || *password == '\0') {
        throw ConfigError(
            "environment variable " + password_environment
            + " must contain the GLIMPSE password for " + venue.venue_id);
    }
    venue.feed.password = password;
    if (venue.feed.itch_port == 0 || venue.feed.glimpse_port == 0) {
        throw ConfigError("ITCH and GLIMPSE ports must be positive");
    }
    if (venue.feed.session_prefix.empty() || venue.feed.session_prefix.size() > 2
        || !printable_non_space(venue.feed.session_prefix)) {
        throw ConfigError("MoldSessionPrefix must contain 1-2 printable bytes");
    }
    if (venue.feed.username.size() > 6 || venue.feed.password.size() > 10
        || !printable_non_space(venue.feed.username)
        || !printable_non_space(venue.feed.password)) {
        throw ConfigError("GLIMPSE credentials are invalid for " + venue.venue_id);
    }
    return venue;
}

} // namespace

LoadedConfig::LoadedConfig(const std::string& path) try : fix_settings(path) {
    try {
        const auto sessions = fix_settings.getSessions();
        if (sessions.size() != venue_count) {
            throw ConfigError("SOR configuration must define exactly two FIX sessions");
        }
        for (const auto& session_id : sessions) {
            venues.push_back(load_venue(fix_settings, session_id));
        }
        std::sort(venues.begin(), venues.end(), [](const auto& left, const auto& right) {
            return left.priority < right.priority;
        });
        if (venues[0].priority == venues[1].priority) {
            throw ConfigError("venue priorities must be unique");
        }
        if (venues[0].venue_id == venues[1].venue_id) {
            throw ConfigError("venue IDs must be unique");
        }
    } catch (const ConfigError&) {
        throw;
    } catch (const std::exception& error) {
        throw ConfigError(std::string("invalid SOR configuration: ") + error.what());
    }
} catch (const ConfigError&) {
    throw;
} catch (const std::exception& error) {
    throw ConfigError(std::string("invalid SOR configuration: ") + error.what());
}

} // namespace mockx::sor

"""Strict GLIMPSE and SoupBinTCP configuration contracts."""

import pytest

from src.mockx.configuration import ConfigurationError, validate_config

from .api_helpers import api_config
from .test_itch_integration import itch_config


def enabled_glimpse_config():
    raw = itch_config()
    raw["soupbintcp"] = {
        "host": "127.0.0.1",
        "port": 18001,
        "session_prefix": "GS",
        "credentials": {"GLIMP": "SECRET"},
        "login_timeout_ms": 30_000,
        "write_timeout_ms": 5_000,
        "max_connections": 64,
        "replay_ttl_ms": 60_000,
        "max_cached_sessions": 32,
        "max_cached_bytes": 67_108_864,
    }
    return raw


def test_native_glimpse_is_required_and_legacy_flags_are_rejected():
    config = validate_config(api_config(), environ={})
    assert config.soupbintcp is not None
    raw = api_config()
    raw["glimpse"] = {"enabled": False}
    with pytest.raises(ConfigurationError, match="glimpse"):
        validate_config(raw, environ={})


def test_native_glimpse_requires_soup_transport():
    raw = api_config()
    del raw["soupbintcp"]
    with pytest.raises(ConfigurationError, match="soupbintcp"):
        validate_config(raw, environ={})


def test_soup_credentials_are_ascii_bounded_and_case_insensitively_unique():
    raw = enabled_glimpse_config()
    raw["soupbintcp"]["credentials"] = {"glimp": "SECRET", "GLIMP": "OTHER"}
    with pytest.raises(ConfigurationError, match="case-insensitive"):
        validate_config(raw, environ={})

    raw = enabled_glimpse_config()
    raw["soupbintcp"]["credentials"] = {"TOOLONG": "SECRET"}
    with pytest.raises(ConfigurationError, match="1-6 ASCII"):
        validate_config(raw, environ={})

    raw = enabled_glimpse_config()
    raw["soupbintcp"]["credentials"] = {"GLIMP": "TOO-LONG-PASSWORD"}
    with pytest.raises(ConfigurationError, match="1-10 ASCII"):
        validate_config(raw, environ={})

    raw = enabled_glimpse_config()
    raw["soupbintcp"]["credentials"] = {"GL IMP": "SECRET"}
    with pytest.raises(ConfigurationError, match="printable non-space"):
        validate_config(raw, environ={})


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", 0),
        ("session_prefix", "LONG"),
        ("session_prefix", "_"),
        ("login_timeout_ms", 0),
        ("write_timeout_ms", 0),
        ("max_connections", 0),
        ("replay_ttl_ms", 0),
        ("max_cached_sessions", 0),
        ("max_cached_bytes", 0),
    ],
)
def test_soup_resource_and_timeout_bounds_are_strict(field, value):
    raw = enabled_glimpse_config()
    raw["soupbintcp"][field] = value
    with pytest.raises(ConfigurationError):
        validate_config(raw, environ={})


def test_enabled_glimpse_configuration_normalizes_credentials():
    config = validate_config(enabled_glimpse_config(), environ={})
    assert config.soupbintcp is not None
    assert dict(config.soupbintcp.credentials) == {"GLIMP": "SECRET"}

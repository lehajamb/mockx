"""Strict, immutable configuration loading for MockX."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import ipaddress
import os
from pathlib import Path
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
import yaml


MAX_LATENCY_MS = 2_147_483_647
MAX_SEED = (1 << 63) - 1
MIN_SEED = -(1 << 63)
MAX_INSTRUMENT_UNITS = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class ConfigurationIssue:
    """Stable, path-addressed configuration problem."""

    path: str
    code: str
    message: str


class ConfigurationError(ValueError):
    """Raised when a configuration file or environment merge is invalid."""

    def __init__(self, issues: tuple[ConfigurationIssue, ...]):
        if not issues:
            raise ValueError("ConfigurationError requires at least one issue")
        self.issues = tuple(sorted(issues, key=lambda item: (item.path, item.code)))
        super().__init__(
            "; ".join(f"{issue.path}: {issue.message}" for issue in self.issues)
        )


class StrictConfigModel(BaseModel):
    """Shared strict/frozen policy for every configuration section."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


def _nonempty(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a decimal value")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field_name} must be a decimal value") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


def _scale(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("decimal scale requires a finite value")
    return max(0, -exponent)


class InstrumentConfig(StrictConfigModel):
    """Validated per-symbol decimal metadata."""

    tick_size: Decimal
    lot_size: Decimal
    price_display_precision: Optional[StrictInt] = None
    quantity_display_precision: Optional[StrictInt] = None

    @field_validator(
        "tick_size",
        "lot_size",
        mode="before",
    )
    @classmethod
    def parse_decimals(cls, value: object, info):
        if value is None:
            return None
        return _decimal(value, field_name=info.field_name)

    @field_validator("tick_size", "lot_size")
    @classmethod
    def positive_increment(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("price_display_precision", "quantity_display_precision")
    @classmethod
    def valid_precision(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and not 0 <= value <= 18:
            raise ValueError("must be between 0 and 18")
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> "InstrumentConfig":
        price_precision = (
            _scale(self.tick_size)
            if self.price_display_precision is None
            else self.price_display_precision
        )
        quantity_precision = (
            _scale(self.lot_size)
            if self.quantity_display_precision is None
            else self.quantity_display_precision
        )
        if price_precision < _scale(self.tick_size):
            raise ValueError(
                "price_display_precision cannot be smaller than tick-size scale"
            )
        if quantity_precision < _scale(self.lot_size):
            raise ValueError(
                "quantity_display_precision cannot be smaller than lot-size scale"
            )
        object.__setattr__(self, "price_display_precision", price_precision)
        object.__setattr__(
            self,
            "quantity_display_precision",
            quantity_precision,
        )
        return self


class LatencyConfig(StrictConfigModel):
    engine_ms: StrictInt = Field(ge=0, le=MAX_LATENCY_MS)
    network_ms: Optional[StrictInt] = Field(
        default=None,
        ge=0,
        le=MAX_LATENCY_MS,
    )
    inbound_ms: Optional[StrictInt] = Field(
        default=None,
        ge=0,
        le=MAX_LATENCY_MS,
    )
    outbound_ms: Optional[StrictInt] = Field(
        default=None,
        ge=0,
        le=MAX_LATENCY_MS,
    )
    session_delays: Mapping[str, StrictInt] = Field(default_factory=dict)

    @field_validator("session_delays")
    @classmethod
    def validate_session_delays(
        cls,
        values: Mapping[str, int],
    ) -> Mapping[str, int]:
        normalized: dict[str, int] = {}
        for alias, delay in values.items():
            key = _nonempty(alias, field_name="session delay key")
            if (
                not isinstance(delay, int)
                or isinstance(delay, bool)
                or not 0 <= delay <= MAX_LATENCY_MS
            ):
                raise ValueError(
                    f"session delay for {key!r} must be an integer between "
                    f"0 and {MAX_LATENCY_MS}"
                )
            normalized[key] = delay
        return normalized

    @model_validator(mode="after")
    def validate_directional_shape(self) -> "LatencyConfig":
        directional = self.inbound_ms is not None or self.outbound_ms is not None
        if self.network_ms is None and not directional:
            raise ValueError("configure network_ms or both inbound_ms and outbound_ms")
        if self.network_ms is not None and directional:
            raise ValueError(
                "network_ms cannot be combined with inbound_ms/outbound_ms"
            )
        if directional and (self.inbound_ms is None or self.outbound_ms is None):
            raise ValueError("inbound_ms and outbound_ms must be configured together")
        return self

    @property
    def total_network_ms(self) -> int:
        if self.network_ms is not None:
            return self.network_ms
        return int(self.inbound_ms or 0) + int(self.outbound_ms or 0)


class ExecutionConfig(StrictConfigModel):
    reject_prob: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    random_seed: StrictInt = Field(ge=MIN_SEED, le=MAX_SEED)

    @field_validator("reject_prob", mode="before")
    @classmethod
    def reject_boolean_probability(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("reject_prob must be a number")
        return value


class AdminAPIConfig(StrictConfigModel):
    host: str = "127.0.0.1"
    port: StrictInt = Field(ge=1, le=65535)
    enabled: StrictBool = True

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        host = _nonempty(value, field_name="admin_api.host")
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        if host == "localhost":
            return host
        if len(host) > 253 or not re.fullmatch(
            r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
            r"[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
            r"[A-Za-z0-9])?",
            host,
        ):
            raise ValueError("admin_api.host must be a valid IP address or hostname")
        return host


class FixConfig(StrictConfigModel):
    enabled: StrictBool = False
    delivery_retry_interval_ms: StrictInt = Field(
        default=1_000,
        ge=1,
        le=MAX_LATENCY_MS,
    )
    delivery_max_attempts_per_cycle: StrictInt = Field(
        default=3,
        ge=1,
        le=10_000,
    )
    delivery_terminal_retention_records: StrictInt = Field(
        default=10_000,
        ge=1,
        le=1_000_000,
    )
    session_participants: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("session_participants")
    @classmethod
    def valid_session_participants(
        cls,
        values: Mapping[str, str],
    ) -> Mapping[str, str]:
        return {
            _nonempty(key, field_name="FIX SessionID"): _nonempty(
                participant,
                field_name="participant_id",
            )
            for key, participant in values.items()
        }

    @model_validator(mode="after")
    def require_enabled_session_mapping(self) -> "FixConfig":
        if self.enabled and not self.session_participants:
            raise ValueError("enabled FIX configuration requires session_participants")
        return self


class SchedulerConfig(StrictConfigModel):
    mode: str = "external"
    interval_ms: StrictInt = Field(default=10, ge=1, le=60_000)
    max_advance_ms: StrictInt = Field(
        default=86_400_000,
        ge=1,
        le=86_400_000,
    )
    max_events_per_advance: StrictInt = Field(
        default=100_000,
        ge=1,
        le=1_000_000,
    )

    @field_validator("mode")
    @classmethod
    def supported_mode(cls, value: str) -> str:
        if value not in {"external", "realtime"}:
            raise ValueError("must be 'external' or 'realtime'")
        return value


class ITCHConfig(StrictConfigModel):
    """ITCH 5.0 outbound market-data feed configuration."""

    enabled: StrictBool = False
    market_category: str = " "
    financial_status: str = " "
    round_lot_size: StrictInt = Field(default=1, ge=1, le=0xFFFFFFFF)

    @field_validator("market_category", "financial_status")
    @classmethod
    def single_byte(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be one ASCII byte")
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("must be one ASCII byte") from exc
        if len(encoded) != 1:
            raise ValueError("must be one ASCII byte")
        return value


class GlimpseConfig(StrictConfigModel):
    """GLIMPSE 5.0 outbound snapshot-service configuration."""

    enabled: StrictBool = False


class SoupBinTCPTransportConfig(StrictConfigModel):
    """SoupBinTCP listener, authentication, and bounded replay configuration."""

    host: str = "127.0.0.1"
    port: StrictInt = Field(ge=1, le=65535)
    session_prefix: str = "GS"
    credentials: Mapping[str, str]
    login_timeout_ms: StrictInt = Field(default=30_000, ge=1, le=86_400_000)
    write_timeout_ms: StrictInt = Field(default=5_000, ge=1, le=86_400_000)
    max_connections: StrictInt = Field(default=64, ge=1, le=100_000)
    replay_ttl_ms: StrictInt = Field(default=60_000, ge=1, le=86_400_000)
    max_cached_sessions: StrictInt = Field(default=32, ge=1, le=100_000)
    max_cached_bytes: StrictInt = Field(
        default=67_108_864,
        ge=1,
        le=1_073_741_824,
    )

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        host = _nonempty(value, field_name="soupbintcp.host")
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        if host == "localhost":
            return host
        if len(host) > 253 or not re.fullmatch(
            r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
            r"[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
            r"[A-Za-z0-9])?",
            host,
        ):
            raise ValueError("soupbintcp.host must be a valid IP address or hostname")
        return host

    @field_validator("session_prefix")
    @classmethod
    def valid_session_prefix(cls, value: str) -> str:
        prefix = _nonempty(value, field_name="SoupBinTCP session_prefix")
        try:
            encoded = prefix.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("session_prefix must contain only ASCII") from exc
        if not 1 <= len(encoded) <= 2:
            raise ValueError("session_prefix must contain 1-2 ASCII bytes")
        if not prefix.isalnum():
            raise ValueError("session_prefix must be ASCII alphanumeric")
        return prefix.upper()

    @field_validator("credentials")
    @classmethod
    def valid_credentials(cls, values: Mapping[str, str]) -> Mapping[str, str]:
        if not values:
            raise ValueError("credentials must contain at least one entry")
        normalized: dict[str, str] = {}
        for raw_username, raw_password in values.items():
            if (
                not isinstance(raw_username, str)
                or not raw_username
                or raw_username != raw_username.strip()
            ):
                raise ValueError("Soup username must be a non-empty unpadded string")
            if (
                not isinstance(raw_password, str)
                or not raw_password
                or raw_password != raw_password.strip()
            ):
                raise ValueError("Soup password must be a non-empty unpadded string")
            username = raw_username
            password = raw_password
            try:
                encoded_username = username.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("Soup username must contain 1-6 ASCII bytes") from exc
            try:
                encoded_password = password.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("Soup password must contain 1-10 ASCII bytes") from exc
            if not 1 <= len(encoded_username) <= 6:
                raise ValueError("Soup username must contain 1-6 ASCII bytes")
            if not 1 <= len(encoded_password) <= 10:
                raise ValueError("Soup password must contain 1-10 ASCII bytes")
            if any(byte < 0x21 or byte > 0x7E for byte in encoded_username):
                raise ValueError("Soup username must use printable non-space ASCII")
            if any(byte < 0x21 or byte > 0x7E for byte in encoded_password):
                raise ValueError("Soup password must use printable non-space ASCII")
            key = username.upper()
            if key in normalized:
                raise ValueError("Soup usernames must be case-insensitive unique")
            normalized[key] = password.upper()
        return normalized


class MoldUDP64TransportConfig(StrictConfigModel):
    """MoldUDP64 transport configuration."""

    multicast_group: str = "239.192.0.1"
    multicast_port: StrictInt = Field(ge=1, le=65535)
    session_prefix: str = "MX"
    heartbeat_interval_ms: StrictInt = Field(
        default=1_000,
        ge=100,
        le=60_000,
    )
    max_payload_bytes: StrictInt = Field(
        default=1400,
        ge=512,
        le=65507,
    )
    interface_address: str = "0.0.0.0"
    ttl: StrictInt = Field(default=1, ge=0, le=255)

    @field_validator("session_prefix")
    @classmethod
    def valid_session_prefix(cls, value: str) -> str:
        prefix = _nonempty(value, field_name="MoldUDP64 session_prefix")
        try:
            encoded = prefix.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("session_prefix must contain only ASCII") from exc
        if not 1 <= len(encoded) <= 2:
            raise ValueError("session_prefix must contain 1-2 ASCII bytes")
        return prefix

    @field_validator("multicast_group")
    @classmethod
    def valid_multicast_group(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("must be an IPv4 multicast address") from exc
        if address.version != 4 or not address.is_multicast:
            raise ValueError("must be an IPv4 multicast address")
        return value

    @field_validator("interface_address")
    @classmethod
    def valid_interface_address(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("must be an IPv4 address") from exc
        if address.version != 4 or address.is_multicast:
            raise ValueError("must be a non-multicast IPv4 address")
        return value


class WebSocketConfig(StrictConfigModel):
    heartbeat_interval_seconds: float = Field(
        default=10.0,
        gt=0,
        le=86_400,
        allow_inf_nan=False,
    )
    outbound_queue_capacity: StrictInt = Field(
        default=8,
        ge=1,
        le=100_000,
    )
    send_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=86_400,
        allow_inf_nan=False,
    )
    heartbeat_stale_seconds: float = Field(
        default=30.0,
        gt=0,
        le=86_400,
        allow_inf_nan=False,
    )
    market_data_stale_seconds: float = Field(
        default=30.0,
        gt=0,
        le=86_400,
        allow_inf_nan=False,
    )
    fallback_poll_seconds: float = Field(
        default=2.0,
        gt=0,
        le=86_400,
        allow_inf_nan=False,
    )
    reconnect_initial_seconds: float = Field(
        default=1.0,
        gt=0,
        le=86_400,
        allow_inf_nan=False,
    )
    reconnect_max_seconds: float = Field(
        default=30.0,
        gt=0,
        le=86_400,
        allow_inf_nan=False,
    )
    staleness_check_seconds: float = Field(
        default=1.0,
        gt=0,
        le=86_400,
        allow_inf_nan=False,
    )

    @field_validator(
        "heartbeat_interval_seconds",
        "send_timeout_seconds",
        "heartbeat_stale_seconds",
        "market_data_stale_seconds",
        "fallback_poll_seconds",
        "reconnect_initial_seconds",
        "reconnect_max_seconds",
        "staleness_check_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_duration(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("timing values must be numbers")
        return value

    @model_validator(mode="after")
    def validate_cadence(self) -> "WebSocketConfig":
        if self.heartbeat_stale_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("heartbeat_stale_seconds must exceed heartbeat cadence")
        if self.market_data_stale_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("market_data_stale_seconds must exceed heartbeat cadence")
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError(
                "reconnect_max_seconds must be at least " "reconnect_initial_seconds"
            )
        return self


class PrometheusConfig(StrictConfigModel):
    enabled: StrictBool = True


class QuestDBConfig(StrictConfigModel):
    enabled: StrictBool = True
    url: Optional[str] = None
    health_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    flush_batch_size: StrictInt = Field(default=1_000, ge=1, le=100_000)
    max_buffer_records: StrictInt = Field(
        default=10_000,
        ge=1,
        le=1_000_000,
    )
    max_buffer_bytes: StrictInt = Field(
        default=67_108_864,
        ge=1,
        le=1_073_741_824,
    )
    flush_interval_ms: StrictInt = Field(
        default=1_000,
        ge=1,
        le=86_400_000,
    )
    retry_backoff_ms: StrictInt = Field(
        default=1_000,
        ge=1,
        le=86_400_000,
    )
    max_retry_delay_ms: StrictInt = Field(
        default=30_000,
        ge=1,
        le=86_400_000,
    )
    overflow_retry_attempts: StrictInt = Field(
        default=3,
        ge=1,
        le=10_000,
    )
    shutdown_timeout_ms: StrictInt = Field(
        default=10_000,
        ge=1,
        le=86_400_000,
    )
    overflow_policy: str = "reject_new"
    request_timeout_ms: StrictInt = Field(
        default=10_000,
        ge=1,
        le=86_400_000,
    )

    @field_validator("url", "health_url", "username", "password")
    @classmethod
    def strip_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("overflow_policy")
    @classmethod
    def valid_overflow_policy(cls, value: str) -> str:
        if value not in {"reject_new", "drop_oldest"}:
            raise ValueError("must be 'reject_new' or 'drop_oldest'")
        return value

    @model_validator(mode="after")
    def validate_enabled_contract(self) -> "QuestDBConfig":
        if self.max_buffer_records < self.flush_batch_size:
            raise ValueError("max_buffer_records must be at least flush_batch_size")
        if self.max_retry_delay_ms < self.retry_backoff_ms:
            raise ValueError("max_retry_delay_ms must be at least retry_backoff_ms")
        if self.enabled:
            missing = [
                name for name in ("url", "health_url") if not getattr(self, name)
            ]
            if missing:
                raise ValueError(
                    "enabled QuestDB configuration is missing: " + ", ".join(missing)
                )
            for name in ("url", "health_url"):
                parsed = urlsplit(getattr(self, name) or "")
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.netloc
                    or parsed.path not in {"", "/"}
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError(
                        f"{name} must be an absolute HTTP(S) URL without "
                        "a path, query, or fragment"
                    )
        if (self.username is None) != (self.password is None):
            raise ValueError(
                "QuestDB username and password must be configured together"
            )
        return self


class ExchangeConfig(StrictConfigModel):
    """Complete final MockX configuration contract."""

    exchange_name: str
    instruments: Mapping[str, InstrumentConfig]
    latency: LatencyConfig
    execution: ExecutionConfig
    admin_api: AdminAPIConfig
    fix: FixConfig = Field(default_factory=FixConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    cors_origins: tuple[str, ...] = ()
    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)
    questdb: Optional[QuestDBConfig] = None
    itch: ITCHConfig = Field(default_factory=ITCHConfig)
    moldudp64: Optional[MoldUDP64TransportConfig] = None
    glimpse: GlimpseConfig = Field(default_factory=GlimpseConfig)
    soupbintcp: Optional[SoupBinTCPTransportConfig] = None

    @field_validator("exchange_name")
    @classmethod
    def nonempty_exchange_name(cls, value: str) -> str:
        return _nonempty(value, field_name="exchange_name")

    @field_validator("instruments", mode="before")
    @classmethod
    def normalize_instruments(cls, value: object) -> object:
        if not isinstance(value, Mapping) or not value:
            raise ValueError("instruments must be a non-empty mapping")
        normalized: dict[str, object] = {}
        for raw_symbol, metadata in value.items():
            if not isinstance(raw_symbol, str):
                raise ValueError("instrument symbols must be strings")
            symbol = raw_symbol.strip().upper()
            if not symbol:
                raise ValueError("instrument symbols cannot be empty")
            if symbol in normalized:
                raise ValueError(f"duplicate normalized instrument: {symbol}")
            normalized[symbol] = metadata
        return normalized

    @field_validator("cors_origins", mode="before")
    @classmethod
    def valid_cors_origins(cls, values: object) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            raise ValueError("cors_origins must be a list")
        result: list[str] = []
        for raw in values:
            origin = _nonempty(raw, field_name="cors origin")
            if origin == "*":
                raise ValueError("wildcard CORS is not supported")
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    f"CORS origin must be an HTTP(S) origin without a path: "
                    f"{origin}"
                )
            result.append(origin.rstrip("/"))
        if len(result) != len(set(result)):
            raise ValueError("cors_origins must be unique")
        return tuple(result)

    @model_validator(mode="after")
    def validate_itch_contract(self) -> "ExchangeConfig":
        if self.glimpse.enabled:
            if not self.itch.enabled:
                raise ValueError("enabled GLIMPSE requires enabled ITCH")
            if self.soupbintcp is None:
                raise ValueError("enabled GLIMPSE requires soupbintcp configuration")
        if not self.itch.enabled:
            return self
        if self.moldudp64 is None:
            raise ValueError("enabled ITCH requires moldudp64 configuration")
        if len(self.instruments) > 0xFFFF:
            raise ValueError("ITCH supports at most 65535 instruments")
        for symbol, instrument in self.instruments.items():
            try:
                encoded = symbol.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"ITCH instrument symbol {symbol!r} must be ASCII"
                ) from exc
            if len(encoded) > 8:
                raise ValueError(
                    f"ITCH instrument symbol {symbol!r} exceeds 8 ASCII bytes"
                )
            scaled_tick = instrument.tick_size * Decimal(10_000)
            if scaled_tick != scaled_tick.to_integral_value():
                raise ValueError(
                    f"ITCH tick_size for {symbol} is not representable as Price(4)"
                )
        return self


_ENV_OVERRIDES: Mapping[str, tuple[str, str]] = {
    "QUESTDB_URL": ("url", "text"),
    "QUESTDB_HEALTH_URL": ("health_url", "text"),
    "QUESTDB_USERNAME": ("username", "text"),
    "QUESTDB_PASSWORD": ("password", "text"),
    "QUESTDB_ENABLED": ("enabled", "bool"),
    "QUESTDB_FLUSH_BATCH_SIZE": ("flush_batch_size", "int"),
    "QUESTDB_MAX_BUFFER_RECORDS": ("max_buffer_records", "int"),
    "QUESTDB_MAX_BUFFER_BYTES": ("max_buffer_bytes", "int"),
    "QUESTDB_FLUSH_INTERVAL_MS": ("flush_interval_ms", "int"),
    "QUESTDB_RETRY_BACKOFF_MS": ("retry_backoff_ms", "int"),
    "QUESTDB_MAX_RETRY_DELAY_MS": ("max_retry_delay_ms", "int"),
    "QUESTDB_OVERFLOW_RETRY_ATTEMPTS": (
        "overflow_retry_attempts",
        "int",
    ),
    "QUESTDB_SHUTDOWN_TIMEOUT_MS": ("shutdown_timeout_ms", "int"),
    "QUESTDB_OVERFLOW_POLICY": ("overflow_policy", "text"),
    "QUESTDB_REQUEST_TIMEOUT_MS": ("request_timeout_ms", "int"),
}


def _parse_override(name: str, raw: str, kind: str) -> object:
    if kind == "text":
        return raw
    if kind == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigurationError(
                (
                    ConfigurationIssue(
                        f"environment.{name}",
                        "invalid_integer",
                        "must be an integer",
                    ),
                )
            ) from exc
    if kind == "bool":
        lowered = raw.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        raise ConfigurationError(
            (
                ConfigurationIssue(
                    f"environment.{name}",
                    "invalid_boolean",
                    "must be 'true' or 'false'",
                ),
            )
        )
    raise AssertionError(f"Unsupported environment override kind: {kind}")


def merge_environment(
    data: Mapping[str, Any],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """Merge supported environment overrides before model validation."""

    merged = deepcopy(dict(data))
    supplied = {name: environ[name] for name in _ENV_OVERRIDES if name in environ}
    if supplied:
        questdb = dict(merged.get("questdb") or {})
        if "enabled" not in questdb and "QUESTDB_ENABLED" not in supplied:
            questdb["enabled"] = True
        for name, raw in supplied.items():
            field_name, kind = _ENV_OVERRIDES[name]
            questdb[field_name] = _parse_override(name, raw, kind)
        merged["questdb"] = questdb
    return merged


def _configuration_error(exc: ValidationError) -> ConfigurationError:
    issues = []
    for error in exc.errors(include_url=False):
        path = ".".join(str(item) for item in error["loc"]) or "configuration"
        issues.append(
            ConfigurationIssue(
                path=path,
                code=str(error["type"]),
                message=str(error["msg"]).removeprefix("Value error, "),
            )
        )
    return ConfigurationError(tuple(issues))


def validate_config(
    data: Mapping[str, Any],
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> ExchangeConfig:
    """Validate a raw mapping after applying supported environment overrides."""

    try:
        return ExchangeConfig.model_validate(
            merge_environment(data, os.environ if environ is None else environ)
        )
    except ValidationError as exc:
        raise _configuration_error(exc) from exc


def load_config(
    config_path: str | Path,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> ExchangeConfig:
    """Load YAML and return one validated immutable configuration."""

    path = Path(config_path)
    if not path.is_file():
        raise ConfigurationError(
            (
                ConfigurationIssue(
                    "configuration",
                    "file_not_found",
                    f"file not found: {path}",
                ),
            )
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            (
                ConfigurationIssue(
                    "configuration",
                    "invalid_yaml",
                    str(exc),
                ),
            )
        ) from exc
    if not isinstance(raw, Mapping):
        raise ConfigurationError(
            (
                ConfigurationIssue(
                    "configuration",
                    "invalid_root",
                    "YAML root must be a mapping",
                ),
            )
        )
    return validate_config(raw, environ=environ)

"""Bounded GLIMPSE 5.0 snapshot delivery over SoupBinTCP 4.1."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import hmac
import logging
import secrets
import string
import time
from typing import Any, Awaitable, Callable, Optional

from .configuration import SoupBinTCPTransportConfig
from .glimpse_encoder import GlimpseEncoder, GlimpseSnapshot
from .soupbintcp_transport import (
    LOGIN_REJECT_NOT_AUTHORIZED,
    LOGIN_REJECT_SESSION_UNAVAILABLE,
    PACKET_CLIENT_HEARTBEAT,
    PACKET_DEBUG,
    PACKET_LOGOUT_REQUEST,
    PACKET_UNSEQUENCED_DATA,
    SoupProtocolError,
    encode_end_session,
    encode_login_accepted,
    encode_login_rejected,
    encode_sequenced_data,
    encode_server_heartbeat,
    parse_login_request,
    read_packet,
)


logger = logging.getLogger(__name__)


class GlimpseServiceState(str, Enum):
    IDLE = "idle"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPED = "stopped"


class GlimpseSessionUnavailable(RuntimeError):
    """Raised when a coherent fresh or replayed spin cannot be supplied."""


@dataclass(slots=True)
class _CachedSpin:
    session: str
    run_generation: int
    messages: tuple[bytes, ...]
    wire_bytes: int
    expires_at: float
    leased: bool = False


@dataclass(slots=True)
class _ConnectionIO:
    writer: Any
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_outbound: float = field(default_factory=time.monotonic)


class GlimpseService:
    """Own one process-lifetime Soup listener and bounded immutable spin cache."""

    SERVER_HEARTBEAT_SECONDS = 1.0

    def __init__(
        self,
        config: SoupBinTCPTransportConfig,
        encoder: GlimpseEncoder,
        snapshot_factory: Callable[[], Awaitable[GlimpseSnapshot]],
        metrics: Any,
        *,
        on_state_change: Optional[Callable[[str], None]] = None,
        server_factory: Callable[..., Awaitable[Any]] = asyncio.start_server,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.encoder = encoder
        self.snapshot_factory = snapshot_factory
        self.metrics = metrics
        self.on_state_change = on_state_change
        self.server_factory = server_factory
        self.monotonic = monotonic
        self.state = GlimpseServiceState.IDLE
        self.server: Optional[Any] = None
        self._listener_task: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None
        self.active_generation: Optional[int] = None
        self._session_serial = 0
        self._cache: dict[str, _CachedSpin] = {}
        self._cached_bytes = 0
        self._fresh_spin_lock = asyncio.Lock()
        self._connection_tasks: set[asyncio.Task] = set()
        self._connection_generations: dict[asyncio.Task, Optional[int]] = {}
        self._active_connections = 0
        self._counts: dict[str, int] = {
            "connections_accepted": 0,
            "fresh_sessions": 0,
            "resumed_sessions": 0,
            "completed_sessions": 0,
            "rejected_sessions": 0,
            "sequenced_messages": 0,
            "sequenced_bytes": 0,
            "heartbeats_sent": 0,
            "timeouts": 0,
            "failures": 0,
        }

    def _metrics_call(self, name: str, *args: object) -> None:
        callback = getattr(self.metrics, name, None)
        if callback is not None:
            callback(*args)

    def _set_state(self, state: GlimpseServiceState) -> None:
        self.state = state
        self._metrics_call("update_glimpse_state", state.value)
        if self.on_state_change is not None:
            self.on_state_change(state.value)

    def _record(self, name: str, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("GLIMPSE metric increments cannot be negative")
        self._counts[name] += amount
        self._metrics_call("record_glimpse_count", name, amount)

    def _update_connection_metric(self) -> None:
        self._metrics_call(
            "update_glimpse_active_connections", self._active_connections
        )

    def _update_cache_metrics(self) -> None:
        self._metrics_call("update_glimpse_cache", len(self._cache), self._cached_bytes)

    async def start(self) -> bool:
        if self.server is not None:
            return True
        if self.state == GlimpseServiceState.STOPPED:
            return False
        try:
            self.server = await self.server_factory(
                self._handle_client,
                host=self.config.host,
                port=self.config.port,
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._record("failures")
            self._set_state(GlimpseServiceState.DEGRADED)
            logger.error("GLIMPSE listener unavailable: %s", self.last_error)
            return False
        self.last_error = None
        self._set_state(GlimpseServiceState.READY)
        self._listener_task = asyncio.create_task(
            self._watch_listener(self.server), name="glimpse-listener"
        )
        logger.info(
            "GLIMPSE SoupBinTCP listener started",
            extra={"host": self.config.host, "port": self.config.port},
        )
        return True

    async def _watch_listener(self, server) -> None:
        try:
            await server.serve_forever()
            raise RuntimeError("Required GLIMPSE listener stopped unexpectedly")
        except asyncio.CancelledError:
            if self.server is not server:
                return
            self.last_error = "Required GLIMPSE listener was closed unexpectedly"
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
        if self.server is server:
            self._record("failures")
            self.revoke_all()
            self._set_state(GlimpseServiceState.DEGRADED)

    def begin_run(self, generation: int) -> None:
        self.revoke_all()
        self.active_generation = generation

    def revoke_run(self, generation: int) -> None:
        if self.active_generation == generation:
            self.active_generation = None
        for session, entry in tuple(self._cache.items()):
            if entry.run_generation == generation:
                self._remove_cached(session)
        for task, task_generation in tuple(self._connection_generations.items()):
            if task_generation == generation and not task.done():
                task.cancel()

    def revoke_all(self) -> None:
        self.active_generation = None
        self._cache.clear()
        self._cached_bytes = 0
        self._update_cache_metrics()
        for task, generation in tuple(self._connection_generations.items()):
            if generation is not None and not task.done():
                task.cancel()

    async def stop(self) -> None:
        if self.state == GlimpseServiceState.STOPPED:
            return
        self.revoke_all()
        server = self.server
        self.server = None
        if self._listener_task is not None:
            self._listener_task.cancel()
            await asyncio.gather(self._listener_task, return_exceptions=True)
            self._listener_task = None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                logger.exception("GLIMPSE listener close failed")
        tasks = [task for task in self._connection_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(0.001, self.config.write_timeout_ms / 1000),
                )
            except asyncio.TimeoutError:
                self._record("timeouts")
        self._set_state(GlimpseServiceState.STOPPED)

    def _next_session_name(self) -> str:
        digit_width = 10 - len(self.config.session_prefix)
        if self._session_serial >= (10**digit_width) - 1:
            raise GlimpseSessionUnavailable("Soup session identifier space exhausted")
        self._session_serial += 1
        return self.config.session_prefix + "".join(
            secrets.choice(string.ascii_letters + string.digits)
            for _ in range(digit_width)
        )

    def _remove_cached(self, session: str) -> None:
        entry = self._cache.pop(session, None)
        if entry is not None:
            self._cached_bytes = max(0, self._cached_bytes - entry.wire_bytes)
            self._update_cache_metrics()

    def _prune_expired(self) -> None:
        now = self.monotonic()
        for session, entry in tuple(self._cache.items()):
            if not entry.leased and entry.expires_at <= now:
                self._remove_cached(session)

    def _authenticate(self, username: str, password: str) -> bool:
        expected = self.config.credentials.get(username.upper())
        if expected is None:
            expected = ""
        password_matches = hmac.compare_digest(expected, password.upper())
        return bool(expected) and password_matches

    async def _write(self, connection: _ConnectionIO, wire: bytes) -> None:
        async with connection.write_lock:
            connection.writer.write(wire)
            await asyncio.wait_for(
                connection.writer.drain(),
                timeout=self.config.write_timeout_ms / 1000,
            )
            connection.last_outbound = self.monotonic()

    async def _reject(self, connection: _ConnectionIO, reason: bytes) -> None:
        self._record("rejected_sessions")
        try:
            await self._write(connection, encode_login_rejected(reason))
        except Exception:
            self._record("failures")

    async def _read_login(self, reader: Any):
        deadline = self.monotonic() + self.config.login_timeout_ms / 1000
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            packet = await asyncio.wait_for(read_packet(reader), remaining)
            if packet.packet_type == PACKET_DEBUG:
                continue
            return parse_login_request(packet)

    async def _fresh_spin(self) -> _CachedSpin:
        async with self._fresh_spin_lock:
            self._prune_expired()
            if len(self._cache) >= self.config.max_cached_sessions:
                raise GlimpseSessionUnavailable(
                    "GLIMPSE replay session capacity reached"
                )
            snapshot = await self.snapshot_factory()
            if (
                self.active_generation is None
                or snapshot.run_generation != self.active_generation
                or self.state != GlimpseServiceState.READY
            ):
                raise GlimpseSessionUnavailable("GLIMPSE run is not available")

            instrument_count = len(self.encoder.itch_encoder.symbols)
            wire_bytes = instrument_count * (42 + 28) + len(snapshot.orders) * 39 + 24
            if wire_bytes > self.config.max_cached_bytes:
                raise GlimpseSessionUnavailable("GLIMPSE spin exceeds replay capacity")
            if self._cached_bytes + wire_bytes > self.config.max_cached_bytes:
                raise GlimpseSessionUnavailable("GLIMPSE replay byte capacity reached")

            def encode_spin() -> tuple[tuple[bytes, ...], int]:
                encoded = self.encoder.encode(snapshot)
                actual_wire_bytes = sum(3 + len(message) for message in encoded)
                return encoded, actual_wire_bytes

            try:
                messages, actual_wire_bytes = await asyncio.to_thread(encode_spin)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._record("failures")
                self.revoke_all()
                self._set_state(GlimpseServiceState.DEGRADED)
                raise GlimpseSessionUnavailable(
                    "Required GLIMPSE encoder failed"
                ) from exc
            if (
                self.active_generation is None
                or snapshot.run_generation != self.active_generation
                or self.state != GlimpseServiceState.READY
            ):
                raise GlimpseSessionUnavailable("GLIMPSE run ended during encoding")
            if actual_wire_bytes != wire_bytes:
                raise RuntimeError("GLIMPSE wire-size estimate disagrees with encoding")
            session = self._next_session_name()
            entry = _CachedSpin(
                session=session,
                run_generation=snapshot.run_generation,
                messages=messages,
                wire_bytes=wire_bytes,
                expires_at=float("inf"),
                leased=True,
            )
            self._cache[session] = entry
            self._cached_bytes += wire_bytes
            self._update_cache_metrics()
            return entry

    def _resume_spin(self, session: str, sequence: int) -> tuple[_CachedSpin, int]:
        self._prune_expired()
        entry = self._cache.get(session)
        if (
            entry is None
            or entry.leased
            or entry.run_generation != self.active_generation
        ):
            raise GlimpseSessionUnavailable("Soup session is unavailable")
        actual_sequence = len(entry.messages) if sequence == 0 else sequence
        if not 1 <= actual_sequence <= len(entry.messages) + 1:
            raise GlimpseSessionUnavailable("Soup sequence is unavailable")
        entry.leased = True
        entry.expires_at = float("inf")
        return entry, actual_sequence

    async def _heartbeat_sender(self, connection: _ConnectionIO) -> None:
        while True:
            elapsed = self.monotonic() - connection.last_outbound
            await asyncio.sleep(max(0.001, self.SERVER_HEARTBEAT_SECONDS - elapsed))
            if (
                self.monotonic() - connection.last_outbound
                >= self.SERVER_HEARTBEAT_SECONDS
            ):
                await self._write(connection, encode_server_heartbeat())
                self._record("heartbeats_sent")

    async def _receiver(self, reader: Any, heartbeat_timeout_ms: int) -> str:
        timeout = heartbeat_timeout_ms / 1000
        while True:
            packet = await asyncio.wait_for(read_packet(reader), timeout=timeout)
            if packet.packet_type == PACKET_LOGOUT_REQUEST:
                if packet.payload:
                    raise SoupProtocolError("Logout Request must not have a payload")
                return "logout"
            if packet.packet_type == PACKET_CLIENT_HEARTBEAT:
                if packet.payload:
                    raise SoupProtocolError("Client Heartbeat must not have a payload")
                continue
            if packet.packet_type in {
                PACKET_DEBUG,
                PACKET_UNSEQUENCED_DATA,
            }:
                continue
            raise SoupProtocolError("unsupported client packet after login")

    async def _sender(
        self,
        connection: _ConnectionIO,
        entry: _CachedSpin,
        first_sequence: int,
    ) -> None:
        for message in entry.messages[first_sequence - 1 :]:
            if entry.run_generation != self.active_generation:
                raise GlimpseSessionUnavailable("GLIMPSE run ended during spin")
            wire = encode_sequenced_data(message)
            await self._write(connection, wire)
            self._record("sequenced_messages")
            self._record("sequenced_bytes", len(wire))
        await self._write(connection, encode_end_session())

    async def _serve_spin(
        self,
        reader: Any,
        connection: _ConnectionIO,
        entry: _CachedSpin,
        first_sequence: int,
        heartbeat_timeout_ms: int,
    ) -> bool:
        await self._write(
            connection,
            encode_login_accepted(entry.session, first_sequence),
        )
        sender = asyncio.create_task(
            self._sender(connection, entry, first_sequence),
            name=f"mockx-glimpse-sender-{entry.session}",
        )
        receiver = asyncio.create_task(
            self._receiver(reader, heartbeat_timeout_ms),
            name=f"mockx-glimpse-receiver-{entry.session}",
        )
        heartbeat = asyncio.create_task(
            self._heartbeat_sender(connection),
            name=f"mockx-glimpse-heartbeat-{entry.session}",
        )
        completed = False
        try:
            done, _ = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sender in done:
                sender.result()
                completed = True
            else:
                receiver.result()
        finally:
            for task in (sender, receiver, heartbeat):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sender, receiver, heartbeat, return_exceptions=True)
        return completed

    async def _handle_client(self, reader: Any, writer: Any) -> None:
        task = asyncio.current_task()
        if task is None:
            writer.close()
            return
        if self._active_connections >= self.config.max_connections:
            connection = _ConnectionIO(writer, last_outbound=self.monotonic())
            await self._reject(connection, LOGIN_REJECT_SESSION_UNAVAILABLE)
            writer.close()
            return
        self._connection_tasks.add(task)
        self._connection_generations[task] = None
        self._active_connections += 1
        self._record("connections_accepted")
        self._update_connection_metric()
        connection = _ConnectionIO(writer, last_outbound=self.monotonic())
        entry: Optional[_CachedSpin] = None
        completed = False
        try:
            try:
                login = await self._read_login(reader)
            except asyncio.TimeoutError:
                self._record("timeouts")
                return
            except (asyncio.IncompleteReadError, SoupProtocolError):
                return

            if not self._authenticate(login.username, login.password):
                await self._reject(connection, LOGIN_REJECT_NOT_AUTHORIZED)
                return

            try:
                if not login.requested_session:
                    if login.requested_sequence != 1:
                        raise GlimpseSessionUnavailable(
                            "fresh GLIMPSE login must request Soup sequence 1"
                        )
                    entry = await self._fresh_spin()
                    first_sequence = 1
                    self._record("fresh_sessions")
                else:
                    entry, first_sequence = self._resume_spin(
                        login.requested_session,
                        login.requested_sequence,
                    )
                    self._record("resumed_sessions")
            except Exception as exc:
                if not isinstance(exc, GlimpseSessionUnavailable):
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self._record("failures")
                    logger.exception("GLIMPSE spin construction failed")
                await self._reject(connection, LOGIN_REJECT_SESSION_UNAVAILABLE)
                return

            self._connection_generations[task] = entry.run_generation
            try:
                completed = await self._serve_spin(
                    reader,
                    connection,
                    entry,
                    first_sequence,
                    login.heartbeat_timeout_ms,
                )
            except asyncio.TimeoutError:
                self._record("timeouts")
            except asyncio.CancelledError:
                return
            except (
                asyncio.IncompleteReadError,
                ConnectionError,
                OSError,
                SoupProtocolError,
                GlimpseSessionUnavailable,
            ):
                pass
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._record("failures")
                logger.exception("Unexpected GLIMPSE connection failure")
        finally:
            if entry is not None:
                current = self._cache.get(entry.session)
                if current is entry:
                    if completed:
                        self._record("completed_sessions")
                        self._remove_cached(entry.session)
                    elif entry.run_generation == self.active_generation:
                        entry.leased = False
                        entry.expires_at = (
                            self.monotonic() + self.config.replay_ttl_ms / 1000
                        )
                    else:
                        self._remove_cached(entry.session)
                    self._update_cache_metrics()
            writer.close()
            self._connection_generations.pop(task, None)
            self._connection_tasks.discard(task)
            self._active_connections = max(0, self._active_connections - 1)
            self._update_connection_metric()

    def diagnostics(self) -> dict[str, object]:
        self._prune_expired()
        return {
            "enabled": True,
            "state": self.state.value,
            "active_connections": self._active_connections,
            "cached_sessions": len(self._cache),
            "cached_bytes": self._cached_bytes,
            **self._counts,
            "last_error": self.last_error,
        }

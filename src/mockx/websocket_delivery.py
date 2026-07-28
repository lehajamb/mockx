"""Isolated, bounded outbound delivery for WebSocket subscriptions."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Callable, Deque, Optional, TYPE_CHECKING
from uuid import uuid4

from fastapi import WebSocketDisconnect

if TYPE_CHECKING:
    from .metrics import MetricsCollector


logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    """Lifecycle of one accepted, valid-symbol WebSocket subscription."""

    CONNECTING = "CONNECTING"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class OutboundKind(str, Enum):
    MARKET_DATA = "MARKET_DATA"
    HEARTBEAT = "HEARTBEAT"
    ERROR = "ERROR"
    CLOSE = "CLOSE"


class EnqueueDisposition(str, Enum):
    ENQUEUED = "ENQUEUED"
    CONFLATED = "CONFLATED"
    DROPPED = "DROPPED"


@dataclass(frozen=True, slots=True)
class OutboundFrame:
    kind: OutboundKind
    payload: Optional[dict[str, Any]] = None
    sequence: Optional[int] = None
    initial: bool = False
    close_code: Optional[int] = None


class OutboundBuffer:
    """Single-consumer bounded buffer with market-data conflation."""

    def __init__(
        self,
        capacity: int,
        on_depth_change: Optional[Callable[[int], None]] = None,
    ) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("WebSocket outbound queue capacity must be positive")
        self.capacity = capacity
        self._items: Deque[OutboundFrame] = deque()
        self._not_empty = asyncio.Event()
        self._on_depth_change = on_depth_change
        self._latest_snapshot_sequence: Optional[int] = None

    @property
    def qsize(self) -> int:
        return len(self._items)

    @property
    def latest_snapshot_sequence(self) -> Optional[int]:
        return self._latest_snapshot_sequence

    def _change_depth(self, delta: int) -> None:
        if delta and self._on_depth_change is not None:
            self._on_depth_change(delta)

    def _append(self, frame: OutboundFrame) -> None:
        self._items.append(frame)
        self._change_depth(1)
        self._not_empty.set()

    def _insert(self, index: int, frame: OutboundFrame) -> None:
        self._items.insert(index, frame)
        self._change_depth(1)
        self._not_empty.set()

    def _remove_at(self, index: int) -> OutboundFrame:
        frame = self._items[index]
        del self._items[index]
        self._change_depth(-1)
        if not self._items:
            self._not_empty.clear()
        return frame

    def enqueue_initial(
        self,
        payload: dict[str, Any],
        *,
        sequence: int,
    ) -> None:
        if self._items or self._latest_snapshot_sequence is not None:
            raise RuntimeError("Initial WebSocket snapshot is already queued")
        self._latest_snapshot_sequence = sequence
        self._append(
            OutboundFrame(
                kind=OutboundKind.MARKET_DATA,
                payload=payload,
                sequence=sequence,
                initial=True,
            )
        )

    def enqueue_snapshot(
        self,
        payload: dict[str, Any],
        *,
        sequence: int,
    ) -> EnqueueDisposition:
        latest = self._latest_snapshot_sequence
        if latest is not None and sequence <= latest:
            return EnqueueDisposition.DROPPED

        for index, frame in enumerate(self._items):
            if frame.kind == OutboundKind.MARKET_DATA and not frame.initial:
                self._items[index] = OutboundFrame(
                    kind=OutboundKind.MARKET_DATA,
                    payload=payload,
                    sequence=sequence,
                )
                self._latest_snapshot_sequence = sequence
                return EnqueueDisposition.CONFLATED

        if len(self._items) >= self.capacity:
            for index, frame in enumerate(self._items):
                if frame.kind == OutboundKind.HEARTBEAT:
                    self._remove_at(index)
                    break
            if len(self._items) >= self.capacity:
                return EnqueueDisposition.DROPPED

        heartbeat_index = next(
            (
                index
                for index, frame in enumerate(self._items)
                if frame.kind == OutboundKind.HEARTBEAT
            ),
            len(self._items),
        )
        frame = OutboundFrame(
            kind=OutboundKind.MARKET_DATA,
            payload=payload,
            sequence=sequence,
        )
        if heartbeat_index == len(self._items):
            self._append(frame)
        else:
            self._insert(heartbeat_index, frame)
        self._latest_snapshot_sequence = sequence
        return EnqueueDisposition.ENQUEUED

    def enqueue_heartbeat(
        self,
        payload: dict[str, Any],
    ) -> EnqueueDisposition:
        replacement = OutboundFrame(
            kind=OutboundKind.HEARTBEAT,
            payload=payload,
        )
        for index, frame in enumerate(self._items):
            if frame.kind == OutboundKind.HEARTBEAT:
                self._items[index] = replacement
                return EnqueueDisposition.CONFLATED
        if len(self._items) >= self.capacity:
            return EnqueueDisposition.DROPPED
        self._append(replacement)
        return EnqueueDisposition.ENQUEUED

    def enqueue_error_and_close(
        self,
        payload: dict[str, Any],
        *,
        close_code: int,
    ) -> None:
        """Prioritize a graceful fatal error after any still-pending initial."""
        initial = next(
            (frame for frame in self._items if frame.initial),
            None,
        )
        self.clear()
        if initial is not None:
            self._append(initial)
        if len(self._items) < self.capacity:
            self._append(OutboundFrame(OutboundKind.ERROR, payload=payload))
        if len(self._items) < self.capacity:
            self._append(
                OutboundFrame(
                    OutboundKind.CLOSE,
                    close_code=close_code,
                )
            )

    async def get(self) -> OutboundFrame:
        while not self._items:
            await self._not_empty.wait()
        frame = self._items.popleft()
        self._change_depth(-1)
        if not self._items:
            self._not_empty.clear()
        return frame

    def clear(self) -> None:
        depth = len(self._items)
        self._items.clear()
        self._not_empty.clear()
        self._change_depth(-depth)


@dataclass(eq=False, slots=True)
class WebSocketConnection:
    connection_id: str
    symbol: str
    websocket: Any
    outbound: OutboundBuffer
    state: ConnectionState = ConnectionState.CONNECTING
    sender_task: Optional[asyncio.Task[None]] = None
    last_successful_send_at: Optional[datetime] = None
    conflated_snapshots: int = 0
    dropped_snapshots: int = 0
    dropped_heartbeats: int = 0
    registered: bool = False
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class WebSocketRegistry:
    """Own connections, symbol indexes, sender tasks, and bounded cleanup."""

    def __init__(
        self,
        metrics: "MetricsCollector",
        *,
        queue_capacity: int,
        send_timeout_seconds: float,
    ) -> None:
        if send_timeout_seconds <= 0:
            raise ValueError("WebSocket send timeout must be positive")
        self.metrics = metrics
        self.queue_capacity = queue_capacity
        self.send_timeout_seconds = float(send_timeout_seconds)
        self._connections: dict[str, WebSocketConnection] = {}
        self._by_symbol: dict[str, dict[str, WebSocketConnection]] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        symbol: str,
        websocket: Any,
        initial_message: dict[str, Any],
        *,
        initial_sequence: int,
    ) -> WebSocketConnection:
        connection = WebSocketConnection(
            connection_id=uuid4().hex,
            symbol=symbol,
            websocket=websocket,
            outbound=OutboundBuffer(
                self.queue_capacity,
                self.metrics.adjust_websocket_queue_depth,
            ),
        )
        async with self._lock:
            connection.outbound.enqueue_initial(
                initial_message,
                sequence=initial_sequence,
            )
            self._connections[connection.connection_id] = connection
            self._by_symbol.setdefault(symbol, {})[
                connection.connection_id
            ] = connection
            connection.registered = True
            connection.state = ConnectionState.ACTIVE
            self.metrics.websocket_client_connected()
            connection.sender_task = asyncio.create_task(
                self._run_sender(connection),
                name=f"mockx-websocket-sender-{connection.connection_id}",
            )
        return connection

    async def snapshot(
        self,
        symbol: str,
    ) -> tuple[WebSocketConnection, ...]:
        async with self._lock:
            return tuple(self._by_symbol.get(symbol, {}).values())

    async def connection_count(
        self,
        symbol: Optional[str] = None,
    ) -> int:
        async with self._lock:
            if symbol is None:
                return len(self._connections)
            return len(self._by_symbol.get(symbol, {}))

    async def enqueue_market_data(
        self,
        symbol: str,
        message: dict[str, Any],
        *,
        sequence: int,
    ) -> None:
        connections = await self.snapshot(symbol)
        for connection in connections:
            if connection.state != ConnectionState.ACTIVE:
                continue
            disposition = connection.outbound.enqueue_snapshot(
                message,
                sequence=sequence,
            )
            if disposition == EnqueueDisposition.CONFLATED:
                connection.conflated_snapshots += 1
                self.metrics.record_websocket_snapshot_conflated()
            elif disposition == EnqueueDisposition.DROPPED:
                connection.dropped_snapshots += 1
                self.metrics.record_websocket_snapshot_dropped()

    def enqueue_heartbeat(
        self,
        connection: WebSocketConnection,
        message: dict[str, Any],
    ) -> EnqueueDisposition:
        if connection.state != ConnectionState.ACTIVE:
            return EnqueueDisposition.DROPPED
        disposition = connection.outbound.enqueue_heartbeat(message)
        if disposition in {
            EnqueueDisposition.CONFLATED,
            EnqueueDisposition.DROPPED,
        }:
            connection.dropped_heartbeats += 1
        return disposition

    async def _run_sender(self, connection: WebSocketConnection) -> None:
        try:
            while True:
                frame = await connection.outbound.get()
                if frame.kind == OutboundKind.CLOSE:
                    await self._close_socket(
                        connection,
                        frame.close_code or 1000,
                    )
                    await self._detach(connection)
                    connection.state = ConnectionState.CLOSED
                    return
                assert frame.payload is not None
                try:
                    await asyncio.wait_for(
                        connection.websocket.send_json(frame.payload),
                        timeout=self.send_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    self.metrics.record_websocket_send_failure()
                    self.metrics.record_websocket_slow_client_disconnect()
                    await self._cleanup_connection(
                        connection,
                        code=1013,
                        sender_is_current=True,
                    )
                    raise asyncio.CancelledError
                except (
                    WebSocketDisconnect,
                    RuntimeError,
                    ConnectionError,
                    OSError,
                ):
                    self.metrics.record_websocket_send_failure()
                    await self._cleanup_connection(
                        connection,
                        code=1011,
                        sender_is_current=True,
                    )
                    return
                connection.last_successful_send_at = datetime.now(timezone.utc)
                self.metrics.record_websocket_send_success()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metrics.record_websocket_send_failure()
            logger.exception(
                "Unexpected WebSocket sender failure",
                extra={"connection_id": connection.connection_id},
            )
            await self._cleanup_connection(
                connection,
                code=1011,
                sender_is_current=True,
            )

    async def _detach(self, connection: WebSocketConnection) -> bool:
        async with self._lock:
            if not connection.registered:
                return False
            self._connections.pop(connection.connection_id, None)
            symbol_connections = self._by_symbol.get(connection.symbol)
            if symbol_connections is not None:
                symbol_connections.pop(connection.connection_id, None)
                if not symbol_connections:
                    self._by_symbol.pop(connection.symbol, None)
            connection.registered = False
            if connection.state != ConnectionState.CLOSED:
                connection.state = ConnectionState.CLOSING
            self.metrics.websocket_client_disconnected()
            return True

    async def _close_socket(
        self,
        connection: WebSocketConnection,
        code: int,
    ) -> None:
        try:
            await asyncio.wait_for(
                connection.websocket.close(code=code),
                timeout=self.send_timeout_seconds,
            )
        except (
            asyncio.TimeoutError,
            WebSocketDisconnect,
            RuntimeError,
            ConnectionError,
            OSError,
        ):
            logger.debug(
                "WebSocket already closed or close timed out",
                extra={"connection_id": connection.connection_id},
            )

    async def _cleanup_connection(
        self,
        connection: WebSocketConnection,
        *,
        code: int,
        sender_is_current: bool,
    ) -> None:
        async with connection.cleanup_lock:
            if connection.state == ConnectionState.CLOSED:
                return
            await self._detach(connection)
            connection.outbound.clear()
            task = connection.sender_task
            if not sender_is_current and task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await self._close_socket(connection, code)
            connection.state = ConnectionState.CLOSED

    async def disconnect(
        self,
        connection: WebSocketConnection,
        *,
        code: int = 1000,
    ) -> None:
        await self._cleanup_connection(
            connection,
            code=code,
            sender_is_current=(connection.sender_task is asyncio.current_task()),
        )

    async def close_all(self, *, code: int = 1001) -> None:
        async with self._lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
            self._by_symbol.clear()
            for connection in connections:
                connection.registered = False
                connection.state = ConnectionState.CLOSING
                self.metrics.websocket_client_disconnected()
        await asyncio.gather(
            *(
                self._cleanup_connection(
                    connection,
                    code=code,
                    sender_is_current=False,
                )
                for connection in connections
            ),
            return_exceptions=True,
        )

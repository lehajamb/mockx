"""Run-scoped ITCH publisher driven by authoritative displayed-book events."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import logging
from typing import Callable, Optional

from .configuration import ITCHConfig, MoldUDP64TransportConfig
from .instruments import InstrumentCatalog
from .itch_encoder import (
    EVENT_END_OF_MARKET_HOURS,
    EVENT_END_OF_MESSAGES,
    EVENT_END_OF_SYSTEM_HOURS,
    EVENT_START_OF_MARKET_HOURS,
    EVENT_START_OF_MESSAGES,
    EVENT_START_OF_SYSTEM_HOURS,
    ITCHEncoder,
    TRADING_STATE_TRADING,
)
from .models import (
    DisplayedOrderAdded,
    DisplayedOrderDeleted,
    DisplayedOrderEvent,
    DisplayedOrderExecuted,
    DisplayedOrderReduced,
    ITCHHeartbeatCommand,
)
from .moldudp64_transport import (
    MoldSendResult,
    MoldUDP64Config,
    MoldUDP64Transport,
)


logger = logging.getLogger(__name__)


class ITCHFeedState(str, Enum):
    IDLE = "idle"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPED = "stopped"


class ITCHFeedPublisher:
    """Own encoder/transport session state without shadowing the order book."""

    HEARTBEAT_EVENT = "itch_heartbeat"

    def __init__(
        self,
        instruments: InstrumentCatalog,
        scheduler,
        itch_config: ITCHConfig,
        mold_config: MoldUDP64TransportConfig,
        metrics,
        *,
        on_state_change: Optional[Callable[[str], None]] = None,
        transport_factory: Callable[..., MoldUDP64Transport] = MoldUDP64Transport,
    ) -> None:
        locate_codes = {
            symbol: index for index, symbol in enumerate(instruments.symbols, start=1)
        }
        self.encoder = ITCHEncoder(locate_codes, instruments)
        self.scheduler = scheduler
        self.itch_config = itch_config
        self.mold_config = mold_config
        self.metrics = metrics
        self.on_state_change = on_state_change
        self.transport_factory = transport_factory
        self.state = ITCHFeedState.IDLE
        self.transport: Optional[MoldUDP64Transport] = None
        self.active_generation: Optional[int] = None
        self.active_session_name: Optional[str] = None
        self.last_error: Optional[str] = None
        self._tracking_number = 0
        self._data_sent_since_heartbeat = False
        self._sessions_started = 0
        self._sessions_ended = 0
        self._messages_sent = 0
        self._packets_sent = 0
        self._heartbeats_sent = 0
        self._failures = 0

    @staticmethod
    def session_name(prefix: str, generation: int) -> str:
        if not 1 <= generation <= 99_999_999:
            raise ValueError("ITCH run generation must be between 1 and 99999999")
        return f"{prefix.ljust(2, '_')}{generation:08d}"

    def validate_generation(self, generation: int) -> None:
        self.session_name(self.mold_config.session_prefix, generation)

    def _set_state(self, state: ITCHFeedState) -> None:
        self.state = state
        self.metrics.update_itch_state(
            state.value,
            self.sequence_number,
        )
        callback = self.on_state_change
        if callback is not None:
            callback(state.value)

    @property
    def sequence_number(self) -> int:
        transport = self.transport
        return transport.sequence_number if transport is not None else 1

    def _next_tracking(self) -> int:
        self._tracking_number = (self._tracking_number + 1) & 0xFFFF
        return self._tracking_number

    def _record_send(self, result: MoldSendResult) -> None:
        self._messages_sent += result.messages_sent
        self._packets_sent += result.packets_sent
        self._data_sent_since_heartbeat = (
            self._data_sent_since_heartbeat or result.messages_sent > 0
        )
        self.metrics.record_itch_messages(
            result.messages_sent,
            result.packets_sent,
            result.next_sequence_number,
        )

    def _transport_error(self, exc: BaseException) -> None:
        self._degrade(exc)

    def _degrade(self, exc: BaseException) -> None:
        if self.state == ITCHFeedState.DEGRADED:
            return
        self.last_error = f"{type(exc).__name__}: {exc}"
        self._failures += 1
        self.metrics.record_itch_failure(self.last_error)
        transport = self.transport
        if transport is not None:
            transport.stop()
        self._set_state(ITCHFeedState.DEGRADED)
        logger.error(
            "ITCH feed degraded for run generation %s: %s",
            self.active_generation,
            self.last_error,
        )

    def _bootstrap_messages(self, timestamp: datetime) -> list[bytes]:
        messages = [
            self.encoder.system_event(
                EVENT_START_OF_MESSAGES,
                timestamp,
                self._next_tracking(),
            )
        ]
        for symbol in self.encoder.symbols:
            messages.append(
                self.encoder.stock_directory(
                    symbol,
                    timestamp,
                    market_category=self.itch_config.market_category.encode("ascii"),
                    financial_status=self.itch_config.financial_status.encode("ascii"),
                    round_lot_size=self.itch_config.round_lot_size,
                    tracking_number=self._next_tracking(),
                )
            )
        messages.append(
            self.encoder.system_event(
                EVENT_START_OF_SYSTEM_HOURS,
                timestamp,
                self._next_tracking(),
            )
        )
        for symbol in self.encoder.symbols:
            messages.append(
                self.encoder.stock_trading_action(
                    symbol,
                    TRADING_STATE_TRADING,
                    timestamp,
                    tracking_number=self._next_tracking(),
                )
            )
        messages.append(
            self.encoder.system_event(
                EVENT_START_OF_MARKET_HOURS,
                timestamp,
                self._next_tracking(),
            )
        )
        return messages

    async def start_run(self, generation: int, timestamp: datetime) -> bool:
        recovering = self.state == ITCHFeedState.DEGRADED
        if self.transport is not None:
            self.transport.stop()
        self.active_generation = generation
        self.active_session_name = self.session_name(
            self.mold_config.session_prefix,
            generation,
        )
        self._tracking_number = 0
        self._data_sent_since_heartbeat = False
        self.transport = self.transport_factory(
            MoldUDP64Config(
                multicast_group=self.mold_config.multicast_group,
                multicast_port=self.mold_config.multicast_port,
                session_name=self.active_session_name,
                heartbeat_interval_ms=self.mold_config.heartbeat_interval_ms,
                max_payload_bytes=self.mold_config.max_payload_bytes,
                interface_address=self.mold_config.interface_address,
                ttl=self.mold_config.ttl,
            ),
            on_error=self._transport_error,
        )
        try:
            await self.transport.start()
            self._record_send(
                self.transport.send_messages(self._bootstrap_messages(timestamp))
            )
            self._sessions_started += 1
            self.metrics.record_itch_session_started(self.sequence_number)
            self._set_state(ITCHFeedState.READY)
            if recovering:
                logger.info(
                    "ITCH feed recovered on a fresh run",
                    extra={
                        "run_generation": generation,
                        "mold_session": self.active_session_name,
                    },
                )
            self.scheduler.schedule_event(
                self.HEARTBEAT_EVENT,
                ITCHHeartbeatCommand(generation),
                self.mold_config.heartbeat_interval_ms,
            )
            logger.info(
                "ITCH session started",
                extra={
                    "run_generation": generation,
                    "mold_session": self.active_session_name,
                },
            )
            return True
        except Exception as exc:
            self._degrade(exc)
            return False

    def publish(self, event: DisplayedOrderEvent) -> None:
        if (
            self.state != ITCHFeedState.READY
            or event.run_generation != self.active_generation
            or self.transport is None
        ):
            return
        try:
            tracking = self._next_tracking()
            if isinstance(event, DisplayedOrderAdded):
                message = self.encoder.add_order_no_mpid(
                    event.symbol,
                    event.order_ref_num,
                    event.side,
                    event.qty_lots,
                    event.price_ticks,
                    event.timestamp,
                    tracking_number=tracking,
                )
            elif isinstance(event, DisplayedOrderExecuted):
                if event.displayed_price_ticks == event.execution_price_ticks:
                    message = self.encoder.order_executed(
                        event.symbol,
                        event.order_ref_num,
                        event.executed_qty_lots,
                        event.match_number,
                        event.timestamp,
                        tracking_number=tracking,
                    )
                else:
                    message = self.encoder.order_executed_with_price(
                        event.symbol,
                        event.order_ref_num,
                        event.executed_qty_lots,
                        event.match_number,
                        event.execution_price_ticks,
                        True,
                        event.timestamp,
                        tracking_number=tracking,
                    )
            elif isinstance(event, DisplayedOrderDeleted):
                message = self.encoder.order_delete(
                    event.symbol,
                    event.order_ref_num,
                    event.timestamp,
                    tracking_number=tracking,
                )
            elif isinstance(event, DisplayedOrderReduced):
                message = self.encoder.order_cancel(
                    event.symbol,
                    event.order_ref_num,
                    event.canceled_qty_lots,
                    event.timestamp,
                    tracking_number=tracking,
                )
            else:
                raise TypeError(
                    f"Unsupported displayed-order event: {type(event).__name__}"
                )
            self._record_send(self.transport.send_messages([message]))
        except Exception as exc:
            self._degrade(exc)

    def handle_heartbeat(self, command: ITCHHeartbeatCommand) -> None:
        if (
            self.state != ITCHFeedState.READY
            or command.run_generation != self.active_generation
            or self.transport is None
        ):
            return
        try:
            if self._data_sent_since_heartbeat:
                self._data_sent_since_heartbeat = False
            elif self.transport.send_heartbeat():
                self._packets_sent += 1
                self._heartbeats_sent += 1
                self.metrics.record_itch_heartbeat(self.sequence_number)
            self.scheduler.schedule_event(
                self.HEARTBEAT_EVENT,
                command,
                self.mold_config.heartbeat_interval_ms,
            )
        except Exception as exc:
            self._degrade(exc)

    def _ending_messages(self, timestamp: datetime) -> list[bytes]:
        return [
            self.encoder.system_event(
                code,
                timestamp,
                self._next_tracking(),
            )
            for code in (
                EVENT_END_OF_MARKET_HOURS,
                EVENT_END_OF_SYSTEM_HOURS,
                EVENT_END_OF_MESSAGES,
            )
        ]

    def end_run(self, timestamp: datetime) -> None:
        transport = self.transport
        was_ready = self.state == ITCHFeedState.READY
        generation = self.active_generation
        session_name = self.active_session_name
        try:
            if was_ready and transport is not None:
                self._record_send(
                    transport.send_messages(self._ending_messages(timestamp))
                )
                if transport.send_end_of_session():
                    self._packets_sent += 1
                    self.metrics.record_itch_end_packet(self.sequence_number)
                self._sessions_ended += 1
                self.metrics.record_itch_session_ended()
        except Exception as exc:
            self._degrade(exc)
        finally:
            if transport is not None:
                transport.stop()
            self.transport = None
            self.active_generation = None
            self.active_session_name = None
            self._data_sent_since_heartbeat = False
            if self.state != ITCHFeedState.DEGRADED:
                self._set_state(ITCHFeedState.IDLE)
            logger.info(
                "ITCH session ended",
                extra={
                    "run_generation": generation,
                    "mold_session": session_name,
                    "healthy": (was_ready and self.state != ITCHFeedState.DEGRADED),
                },
            )

    def shutdown(self, timestamp: datetime) -> None:
        self.end_run(timestamp)
        self._set_state(ITCHFeedState.STOPPED)
        logger.info("ITCH publisher stopped")

    def diagnostics(self) -> dict[str, object]:
        return {
            "enabled": True,
            "state": self.state.value,
            "active_session": self.active_session_name,
            "next_sequence_number": self.sequence_number,
            "sessions_started": self._sessions_started,
            "sessions_ended": self._sessions_ended,
            "messages_sent": self._messages_sent,
            "packets_sent": self._packets_sent,
            "heartbeats_sent": self._heartbeats_sent,
            "failures": self._failures,
            "last_error": self.last_error,
        }

"""ITCH 5.0 + MoldUDP64 delivery integration layer.

Wires the ITCH encoder and MoldUDP64 transport to the matching engine's
observer callbacks.  Tracks per-order state to emit the correct sequence
of Add Order, Order Executed, Order Cancel, Order Delete, and Trade
messages.

This module follows the same observer-chaining pattern as
websocket_delivery.py — it decorates engine.on_execution_report,
engine.on_trade, and engine.on_market_data_update (chaining through
any existing handlers) so that existing WebSocket and REST paths are
undisturbed.
"""

from __future__ import annotations

import logging

from .itch_encoder import (
    ITCHEncoder,
    EVENT_START_OF_MESSAGES,
    EVENT_START_OF_SYSTEM_HOURS,
    EVENT_START_OF_MARKET_HOURS,
    EVENT_END_OF_MARKET_HOURS,
    EVENT_END_OF_SYSTEM_HOURS,
    EVENT_END_OF_MESSAGES,
    TRADING_STATE_TRADING,
)
from .moldudp64_transport import MoldUDP64Transport
from .models import ExecutionType, Side

logger = logging.getLogger(__name__)


class ITCHDelivery:
    """Integration layer that observes engine events and emits ITCH+MoldUDP64.

    The delivery layer maintains a mutable order-state cache keyed by
    order_id, which it uses to track the resting side, symbol, price,
    remaining quantity, and attribution of each live order.  This cache
    is deterministically updated on every observed engine event.
    """

    def __init__(
        self,
        encoder: ITCHEncoder,
        transport: MoldUDP64Transport,
    ) -> None:
        self._encoder = encoder
        self._transport = transport
        # Per-order state: order_id → (symbol, side, price_ticks, qty_lots, attribution)
        self._resting: dict[int, tuple[str, Side, int, int, str]] = {}
        # Match counter for ITCH Match Number
        self._next_match_number = 1
        # Order reference number counter (uses internal order.id)
        self._order_ref_base = 0

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def start_session(self, timestamp) -> None:
        """Start the MoldUDP64 session and emit ITCH start-of-day messages."""
        await self._transport.start()

        # System Event: Start of Messages
        self._transport.send_messages(
            [self._encoder.system_event(EVENT_START_OF_MESSAGES, timestamp)]
        )

        # Stock Directory messages for all configured symbols
        for symbol in self._encoder._locate_codes:
            self._transport.send_messages(
                [
                    self._encoder.stock_directory(
                        symbol,
                        timestamp,
                    )
                ]
            )

        # System Event: Start of System Hours
        self._transport.send_messages(
            [
                self._encoder.system_event(
                    EVENT_START_OF_SYSTEM_HOURS, timestamp
                )
            ]
        )

        # Stock Trading Action: Trading for all symbols
        for symbol in self._encoder._locate_codes:
            self._transport.send_messages(
                [
                    self._encoder.stock_trading_action(
                        symbol,
                        TRADING_STATE_TRADING,
                        timestamp,
                    )
                ]
            )

        # System Event: Start of Market Hours
        self._transport.send_messages(
            [
                self._encoder.system_event(
                    EVENT_START_OF_MARKET_HOURS, timestamp
                )
            ]
        )

        logger.info(
            "ITCH session started with %d instruments",
            len(self._encoder._locate_codes),
        )

    def end_session(self, timestamp) -> None:
        """Emit ITCH end-of-day messages and close the MoldUDP64 session."""
        try:
            # System Event: End of Market Hours
            if self._transport.is_active:
                self._transport.send_messages(
                    [
                        self._encoder.system_event(
                            EVENT_END_OF_MARKET_HOURS, timestamp
                        )
                    ]
                )

                # System Event: End of System Hours
                self._transport.send_messages(
                    [
                        self._encoder.system_event(
                            EVENT_END_OF_SYSTEM_HOURS, timestamp
                        )
                    ]
                )

                # System Event: End of Messages
                self._transport.send_messages(
                    [
                        self._encoder.system_event(
                            EVENT_END_OF_MESSAGES, timestamp
                        )
                    ]
                )

                # MoldUDP64 End of Session
                self._transport.send_end_of_session()
        finally:
            self._transport.stop()
            self._resting.clear()

    def send_heartbeat(self) -> None:
        """Send a MoldUDP64 heartbeat (called by scheduler)."""
        self._transport.send_heartbeat()

    # ------------------------------------------------------------------
    # Engine observer callbacks
    # ------------------------------------------------------------------

    def on_execution_report(self, report: ExecutionReport) -> None:
        """Handle one execution report — emit ITCH Add/Execute/Cancel/Delete."""
        if not self._transport.is_active:
            return

        timestamp = report.timestamp
        order_id = report.order_id
        symbol = report.symbol
        order_ref_num = self._order_ref_for(order_id)
        side = report.side
        leaves_qty_lots = report.leaves_qty_lots
        price_ticks = report.order_price_ticks
        exec_type = report.exec_type
        filled_qty = report.cum_qty_lots
        last_qty = report.last_qty_lots or 0
        last_px = report.last_price_ticks

        if exec_type == ExecutionType.NEW:
            # Order added to book
            if price_ticks is None or price_ticks <= 0:
                return  # MARKET orders never rest; skip
            attribution = report.participant_id or ""
            self._resting[order_id] = (
                symbol,
                side,
                price_ticks,
                leaves_qty_lots,
                attribution,
            )
            if attribution:
                msg = self._encoder.add_order_mpid(
                    symbol,
                    order_ref_num,
                    side,
                    leaves_qty_lots,
                    price_ticks,
                    timestamp,
                    attribution,
                )
            else:
                msg = self._encoder.add_order_no_mpid(
                    symbol,
                    order_ref_num,
                    side,
                    leaves_qty_lots,
                    price_ticks,
                    timestamp,
                )
            self._transport.send_messages([msg])

        elif exec_type == ExecutionType.TRADE:
            # Part of the order was executed
            resting = self._resting.get(order_id)
            if resting is not None and resting[3] > 0:
                # Update resting state
                _, rest_side, rest_px, rest_qty, rest_attr = resting
                new_qty = max(0, rest_qty - last_qty)
                if new_qty > 0:
                    self._resting[order_id] = (
                        symbol,
                        rest_side,
                        rest_px,
                        new_qty,
                        rest_attr,
                    )
                else:
                    del self._resting[order_id]

            # Determine if this is a same-price execution
            if last_px is not None and price_ticks is not None:
                if last_px == price_ticks:
                    # Same-price execution
                    match_num = self._next_match_number
                    self._next_match_number += 1
                    msg = self._encoder.order_executed(
                        symbol,
                        order_ref_num,
                        last_qty,
                        match_num,
                        timestamp,
                    )
                else:
                    # Different-price execution
                    match_num = self._next_match_number
                    self._next_match_number += 1
                    msg = self._encoder.order_executed_with_price(
                        symbol,
                        order_ref_num,
                        last_qty,
                        match_num,
                        last_px,
                        True,  # printable
                        timestamp,
                    )
            else:
                # Fallback: use same-price execution
                match_num = self._next_match_number
                self._next_match_number += 1
                msg = self._encoder.order_executed(
                    symbol,
                    order_ref_num,
                    last_qty,
                    match_num,
                    timestamp,
                )

            self._transport.send_messages([msg])

        elif exec_type == ExecutionType.CANCELED:
            resting = self._resting.pop(order_id, None)
            if resting is not None:
                # Full delete
                msg = self._encoder.order_delete(
                    symbol,
                    order_ref_num,
                    timestamp,
                )
            elif report.canceled_qty_lots > 0:
                # Partial cancel — emit Cancel message with canceled qty
                canceled_qty = report.canceled_qty_lots
                msg = self._encoder.order_cancel(
                    symbol,
                    order_ref_num,
                    canceled_qty,
                    timestamp,
                )
            else:
                # Already fully removed — emit delete anyway
                msg = self._encoder.order_delete(
                    symbol,
                    order_ref_num,
                    timestamp,
                )
            self._transport.send_messages([msg])

    def on_trade(self, trade: Trade) -> None:
        """Handle a trade — emit ITCH Trade Message (Non-Cross)."""
        if not self._transport.is_active:
            return

        match_num = self._next_match_number
        self._next_match_number += 1

        msg = self._encoder.trade_non_cross(
            trade.symbol,
            trade.qty_lots,
            trade.price_ticks,
            match_num,
            trade.timestamp,
        )
        self._transport.send_messages([msg])

    # ------------------------------------------------------------------
    # Order reference number generation
    # ------------------------------------------------------------------

    def _order_ref_for(self, order_id: int) -> int:
        """Map internal order.id to ITCH Order Reference Number.

        ITCH Order Reference Numbers are day-unique, not necessarily
        sequential.  We use the internal order ID directly, which is
        unique within a run.
        """
        return order_id

    # ------------------------------------------------------------------
    # Wire into engine observers (non-destructive chaining)
    # ------------------------------------------------------------------

    def wire_observers(
        self,
        engine,
        scheduler,
        *,
        heartbeat_interval_ms: int = 1000,
    ) -> None:
        """Register this delivery layer as a chained observer on the engine.

        Existing callbacks (WebSocket, metrics, etc.) are preserved by
        wrapping them so that both the original handler and our handler
        are called for each event.
        """
        # Chain on_execution_report
        original_er = engine.on_execution_report

        def chained_er(report):
            if original_er is not None:
                original_er(report)
            self.on_execution_report(report)

        engine.on_execution_report = chained_er

        # Chain on_trade
        original_trade = engine.on_trade

        def chained_trade(trade):
            if original_trade is not None:
                original_trade(trade)
            self.on_trade(trade)

        engine.on_trade = chained_trade

        # Register heartbeat handler
        scheduler.register_handler(
            "itch_heartbeat",
            self.send_heartbeat,
        )

        logger.info("ITCH delivery observers wired to engine")
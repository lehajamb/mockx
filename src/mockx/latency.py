"""Deterministic latency scheduler for simulated trading."""

import heapq
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Callable, Any, Optional
from dataclasses import dataclass
import asyncio
import logging

from .models import (
    ExecutionReport,
    InvalidOrderTransition,
    ProcessOrderCommand,
    ScheduledEvent,
)


logger = logging.getLogger(__name__)


class LatencyScheduler:
    """
    Manages deterministic event scheduling with simulated latencies.
    
    This scheduler maintains a priority queue of events and executes
    them in order based on their simulated timestamps. It does not
    use real time delays, allowing backtests to run at full speed.
    """
    
    def __init__(self, session_delays: Optional[Dict[str, int]] = None):
        self.event_queue: List[ScheduledEvent] = []
        self.current_time: datetime = datetime(1970, 1, 1, tzinfo=timezone.utc)
        self.event_handlers: Dict[str, Callable] = {}
        self.running = False
        self.session_delays = session_delays or {}
        self._next_sequence = 0
    
    def current_time_ns(self) -> int:
        """Get current simulated time in nanoseconds."""
        return int(self.current_time.timestamp() * 1_000_000_000)
    
    def register_handler(self, event_type: str, handler: Callable) -> None:
        """Register a handler for a specific event type."""
        self.event_handlers[event_type] = handler
        
    def schedule_event(self, event_type: str, data: object, delay_ms: int) -> None:
        """Schedule an event to occur after a specified delay."""
        event_time = self.current_time + timedelta(milliseconds=delay_ms)
        event = ScheduledEvent(
            timestamp=event_time,
            sequence=self._next_sequence,
            event_type=event_type,
            data=data
        )
        self._next_sequence += 1
        heapq.heappush(self.event_queue, event)
        logger.debug(f"Scheduled {event_type} at {event_time} (in {delay_ms}ms)")
        
    def schedule_order_processing(
        self,
        command: ProcessOrderCommand,
        network_ms: int,
        engine_ms: int,
        *,
        session_alias: Optional[str] = None,
    ) -> None:
        """Schedule order processing with network, engine, and session-specific latencies."""
        session_delay_ms = self.session_delays.get(session_alias or "", 0)

        # First, simulate network latency + session delay to reach the exchange
        arrival_delay = (network_ms // 2) + session_delay_ms
        self.schedule_event(
            "order_arrived",
            command,
            arrival_delay
        )
        
        # Then, simulate engine processing latency
        total_delay = arrival_delay + engine_ms
        self.schedule_event(
            "order_process",
            command,
            total_delay
        )
        
        if session_delay_ms > 0:
            logger.debug(f"Applied {session_delay_ms}ms session delay for {session_alias}")

    def schedule_execution_report(self, report: ExecutionReport, network_ms: int) -> None:
        """Schedule execution report delivery back to client."""
        self.schedule_event(
            "exec_report",
            report,
            network_ms // 2  # Half of round-trip
        )
        
    async def process_events(self) -> None:
        """Process all events up to current time."""
        processed = 0
        
        while self.event_queue:
            # Peek at next event
            next_event = self.event_queue[0]
            
            # If event is in the future, we're done for now
            if next_event.timestamp > self.current_time:
                break
                
            # Pop and process the event
            event = heapq.heappop(self.event_queue)
            
            # Execute handler if registered
            if event.event_type in self.event_handlers:
                try:
                    handler = self.event_handlers[event.event_type]
                    if asyncio.iscoroutinefunction(handler):
                        await handler(event.data)
                    else:
                        handler(event.data)
                    processed += 1
                except InvalidOrderTransition:
                    logger.exception(
                        "Invalid order transition while processing %s",
                        event.event_type,
                    )
                    raise
                except Exception as e:
                    logger.error(f"Error processing {event.event_type}: {e}")
            else:
                logger.warning(f"No handler for event type: {event.event_type}")
                
        if processed > 0:
            logger.debug(f"Processed {processed} events at {self.current_time}")
            
    def advance_time(self, delta_ms: int) -> None:
        """Advance simulated time by specified milliseconds."""
        self.current_time += timedelta(milliseconds=delta_ms)
        
    async def run_until(self, target_time: datetime) -> None:
        """Run all events through the target time without moving backward."""
        if target_time < self.current_time:
            return

        while self.event_queue and self.event_queue[0].timestamp <= target_time:
            next_time = self.event_queue[0].timestamp
            if next_time > self.current_time:
                self.current_time = next_time

            await self.process_events()

        self.current_time = target_time
                
    async def run_until_idle(self) -> None:
        """Process all scheduled events."""
        while self.event_queue:
            # Jump to next event time
            self.current_time = self.event_queue[0].timestamp
            await self.process_events()
            
    def clear(self) -> None:
        """Clear all scheduled events."""
        self.event_queue.clear()
        
    def get_pending_count(self) -> int:
        """Get number of pending events."""
        return len(self.event_queue)

    def get_next_event_time(self) -> datetime:
        """Get the timestamp of the next event without removing it."""
        if not self.event_queue:
            return None
        return self.event_queue[0].timestamp
        
    def set_time(self, new_time: datetime) -> None:
        """Set the current simulated time."""
        self.current_time = new_time

    def reset(self):
        """Resets the scheduler's event queue and clock."""
        self.event_queue.clear()
        self.current_time = datetime(1970, 1, 1, tzinfo=timezone.utc)
        self._next_sequence = 0
        logger.info(f"Latency scheduler reset. Time set to {self.current_time}.")


@dataclass
class LatencyProfile:
    """Profile for different latency scenarios."""
    name: str
    network_ms: int
    engine_ms: int
    jitter_pct: float = 0.0  # Percentage variation in latency
    
    def get_network_latency(self, rng: Any = None) -> int:
        """Get network latency with optional jitter."""
        if self.jitter_pct > 0 and rng:
            variation = int(self.network_ms * self.jitter_pct)
            return self.network_ms + rng.randint(-variation, variation)
        return self.network_ms
        
    def get_engine_latency(self, rng: Any = None) -> int:
        """Get engine latency with optional jitter."""
        if self.jitter_pct > 0 and rng:
            variation = int(self.engine_ms * self.jitter_pct)
            return self.engine_ms + rng.randint(-variation, variation)
        return self.engine_ms


# Common latency profiles
LATENCY_PROFILES = {
    "low": LatencyProfile("low", network_ms=10, engine_ms=1),
    "medium": LatencyProfile("medium", network_ms=50, engine_ms=5),
    "high": LatencyProfile("high", network_ms=200, engine_ms=20),
    "variable": LatencyProfile("variable", network_ms=100, engine_ms=10, jitter_pct=0.3),
}

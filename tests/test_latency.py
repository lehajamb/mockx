"""Tests for the latency scheduler."""

import pytest
import asyncio
from datetime import datetime, timedelta, timezone

from src.mockx.latency import LatencyScheduler, LatencyProfile


class TestLatencyScheduler:
    """Test latency scheduler functionality."""
    
    @pytest.mark.asyncio
    async def test_basic_scheduling(self):
        """Test basic event scheduling."""
        scheduler = LatencyScheduler()
        events_processed = []
        
        def handler(data):
            events_processed.append(data)
            
        scheduler.register_handler("test_event", handler)
        
        # Schedule events
        scheduler.schedule_event("test_event", {"id": 1}, 100)
        scheduler.schedule_event("test_event", {"id": 2}, 50)
        scheduler.schedule_event("test_event", {"id": 3}, 150)
        
        # Process all events
        await scheduler.run_until_idle()
        
        # Check order (should be processed in time order)
        assert len(events_processed) == 3
        assert events_processed[0]["id"] == 2  # 50ms
        assert events_processed[1]["id"] == 1  # 100ms
        assert events_processed[2]["id"] == 3  # 150ms

    @pytest.mark.asyncio
    async def test_equal_timestamp_events_preserve_scheduling_order(self):
        """Events at the same timestamp execute in scheduling order."""
        scheduler = LatencyScheduler()
        events_processed = []

        def handler(data):
            events_processed.append(data["id"])

        scheduler.register_handler("test_event", handler)

        for event_id in range(8):
            scheduler.schedule_event(
                "test_event",
                {"id": event_id},
                delay_ms=10,
            )

        await scheduler.run_until_idle()

        assert events_processed == list(range(8))

    @pytest.mark.asyncio
    async def test_equal_timestamp_order_is_repeatable(self):
        """Equal-timestamp scheduling produces the same FIFO order every run."""
        observed_orders = []

        for _ in range(3):
            scheduler = LatencyScheduler()
            events_processed = []

            scheduler.register_handler(
                "test_event",
                lambda data, processed=events_processed: processed.append(data["id"]),
            )

            for event_id in range(8):
                scheduler.schedule_event(
                    "test_event",
                    {"id": event_id},
                    delay_ms=10,
                )

            await scheduler.run_until_idle()
            observed_orders.append(events_processed)

        assert observed_orders == [list(range(8)) for _ in range(3)]

    @pytest.mark.asyncio
    async def test_handler_scheduled_equal_time_event_runs_after_queued_events(self):
        """A zero-delay handler event does not jump ahead of an existing peer."""
        scheduler = LatencyScheduler()
        events_processed = []

        def handler(data):
            event_id = data["id"]
            events_processed.append(event_id)
            if event_id == "A":
                scheduler.schedule_event("test_event", {"id": "C"}, delay_ms=0)

        scheduler.register_handler("test_event", handler)
        scheduler.schedule_event("test_event", {"id": "A"}, delay_ms=10)
        scheduler.schedule_event("test_event", {"id": "B"}, delay_ms=10)

        await scheduler.run_until_idle()

        assert events_processed == ["A", "B", "C"]
        
    @pytest.mark.asyncio
    async def test_time_advancement(self):
        """Test manual time advancement."""
        scheduler = LatencyScheduler()
        events_processed = []
        
        def handler(data):
            events_processed.append(data)
            
        scheduler.register_handler("test_event", handler)
        
        start_time = scheduler.current_time
        
        # Schedule event 100ms in future
        scheduler.schedule_event("test_event", {"id": 1}, 100)
        
        # Advance 50ms - event shouldn't fire
        scheduler.advance_time(50)
        await scheduler.process_events()
        assert len(events_processed) == 0
        
        # Advance another 50ms - event should fire
        scheduler.advance_time(50)
        await scheduler.process_events()
        assert len(events_processed) == 1

    @pytest.mark.asyncio
    async def test_run_until_processes_events_through_inclusive_target(self):
        """Events before and at the target run while later events remain queued."""
        scheduler = LatencyScheduler()
        events_processed = []
        start_time = scheduler.current_time
        target_time = start_time + timedelta(milliseconds=10)

        scheduler.register_handler(
            "test_event",
            lambda data: events_processed.append(data["id"]),
        )
        scheduler.schedule_event("test_event", {"id": "before"}, delay_ms=5)
        scheduler.schedule_event("test_event", {"id": "at"}, delay_ms=10)
        scheduler.schedule_event("test_event", {"id": "after"}, delay_ms=15)

        await scheduler.run_until(target_time)

        assert events_processed == ["before", "at"]
        assert scheduler.get_pending_count() == 1
        assert scheduler.get_next_event_time() == start_time + timedelta(milliseconds=15)
        assert scheduler.current_time == target_time

    @pytest.mark.asyncio
    async def test_run_until_current_time_processes_due_events(self):
        """An inclusive current-time target processes zero-delay events."""
        scheduler = LatencyScheduler()
        events_processed = []

        scheduler.register_handler(
            "test_event",
            lambda data: events_processed.append(data["id"]),
        )
        scheduler.schedule_event("test_event", {"id": "due"}, delay_ms=0)

        await scheduler.run_until(scheduler.current_time)

        assert events_processed == ["due"]
        assert scheduler.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_run_until_processes_target_event_scheduled_by_handler(self):
        """Zero-delay work scheduled by a target-time handler runs before return."""
        scheduler = LatencyScheduler()
        events_processed = []
        target_time = scheduler.current_time + timedelta(milliseconds=10)

        def handler(data):
            event_id = data["id"]
            events_processed.append(event_id)
            if event_id == "first":
                scheduler.schedule_event(
                    "test_event",
                    {"id": "second"},
                    delay_ms=0,
                )

        scheduler.register_handler("test_event", handler)
        scheduler.schedule_event("test_event", {"id": "first"}, delay_ms=10)

        await scheduler.run_until(target_time)

        assert events_processed == ["first", "second"]
        assert scheduler.get_pending_count() == 0
        assert scheduler.current_time == target_time

    @pytest.mark.asyncio
    async def test_run_until_empty_scheduler_advances_to_target(self):
        """The simulated clock reaches the target even without pending events."""
        scheduler = LatencyScheduler()
        target_time = scheduler.current_time + timedelta(milliseconds=25)

        await scheduler.run_until(target_time)

        assert scheduler.current_time == target_time

    @pytest.mark.asyncio
    async def test_run_until_earlier_target_does_not_move_time_backward(self):
        """An earlier target leaves the clock and pending events unchanged."""
        scheduler = LatencyScheduler()
        scheduler.advance_time(10)
        current_time = scheduler.current_time
        scheduler.schedule_event("test_event", {"id": "pending"}, delay_ms=0)

        await scheduler.run_until(current_time - timedelta(milliseconds=1))

        assert scheduler.current_time == current_time
        assert scheduler.get_pending_count() == 1
        
    @pytest.mark.asyncio
    async def test_order_processing_schedule(self):
        """Test order processing scheduling."""
        scheduler = LatencyScheduler()
        events = []
        
        def arrived_handler(data):
            events.append(("arrived", data["order_id"]))
            
        def process_handler(data):
            events.append(("process", data["order_id"]))
            
        scheduler.register_handler("order_arrived", arrived_handler)
        scheduler.register_handler("order_process", process_handler)
        
        # Schedule order with network and engine latency
        scheduler.schedule_order_processing(
            {"order_id": 1},
            network_ms=100,
            engine_ms=20
        )
        
        # Process all events
        await scheduler.run_until_idle()
        
        # Check events occurred in correct order
        assert len(events) == 2
        assert events[0] == ("arrived", 1)   # After network/2
        assert events[1] == ("process", 1)   # After network/2 + engine
        
    @pytest.mark.asyncio
    async def test_async_handler(self):
        """Test async event handlers."""
        scheduler = LatencyScheduler()
        events_processed = []
        
        async def async_handler(data):
            await asyncio.sleep(0.001)  # Simulate async work
            events_processed.append(data)
            
        scheduler.register_handler("async_event", async_handler)
        scheduler.schedule_event("async_event", {"id": 1}, 10)
        
        await scheduler.run_until_idle()
        
        assert len(events_processed) == 1
        assert events_processed[0]["id"] == 1
        
    def test_pending_count(self):
        """Test pending event counting."""
        scheduler = LatencyScheduler()
        
        assert scheduler.get_pending_count() == 0
        
        scheduler.schedule_event("test", {}, 100)
        scheduler.schedule_event("test", {}, 200)
        
        assert scheduler.get_pending_count() == 2

        scheduler.advance_time(50)
        time_before_clear = scheduler.current_time
        scheduler.clear()

        assert scheduler.get_pending_count() == 0
        assert scheduler.current_time == time_before_clear

        scheduler.schedule_event("test", {}, 100)
        assert scheduler.event_queue[0].sequence == 2

    @pytest.mark.asyncio
    async def test_reset_clears_queue_clock_and_scheduling_sequence(self):
        """Reset restores the epoch and restarts event sequencing from zero."""
        scheduler = LatencyScheduler()
        events_processed = []
        scheduler.register_handler(
            "test_event",
            lambda data: events_processed.append(data["id"]),
        )

        scheduler.advance_time(25)
        scheduler.schedule_event("test_event", {"id": "discarded"}, delay_ms=10)

        scheduler.reset()

        assert scheduler.get_pending_count() == 0
        assert scheduler.current_time == datetime(1970, 1, 1, tzinfo=timezone.utc)

        scheduler.schedule_event("test_event", {"id": "after-reset"}, delay_ms=0)
        assert scheduler.event_queue[0].sequence == 0

        await scheduler.run_until(scheduler.current_time)

        assert events_processed == ["after-reset"]


class TestLatencyProfile:
    """Test latency profile functionality."""
    
    def test_basic_profile(self):
        """Test basic latency profile."""
        profile = LatencyProfile("test", network_ms=100, engine_ms=10)
        
        assert profile.name == "test"
        assert profile.get_network_latency() == 100
        assert profile.get_engine_latency() == 10
        
    def test_jitter(self):
        """Test latency jitter."""
        from random import Random
        rng = Random(42)
        
        profile = LatencyProfile(
            "test",
            network_ms=100,
            engine_ms=10,
            jitter_pct=0.2  # 20% jitter
        )
        
        # Get multiple samples
        network_samples = [profile.get_network_latency(rng) for _ in range(100)]
        engine_samples = [profile.get_engine_latency(rng) for _ in range(100)]
        
        # Check within expected range
        assert all(80 <= n <= 120 for n in network_samples)
        assert all(8 <= e <= 12 for e in engine_samples)
        
        # Check we get variation
        assert len(set(network_samples)) > 1
        assert len(set(engine_samples)) > 1

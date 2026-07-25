"""Tests for the latency scheduler."""

import pytest
import asyncio
from datetime import datetime, timedelta

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
        
        scheduler.clear()
        assert scheduler.get_pending_count() == 0


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

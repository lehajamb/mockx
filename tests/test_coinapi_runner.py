"""Driver lifecycle, pacing and independent actor regressions without sockets."""

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from examples.coinapi_replay.controller import ControllerConfig, DepthLevel
from examples.coinapi_replay.runner import ReplayError, ReplayOptions, ReplayRunner
from examples.coinapi_replay.source import SourceEvent, SourceOrder, source_digest
from examples.fix_support import FixReport
from src.mockx.configuration import execution_config_fingerprint, validate_config
from src.mockx.models import Side
from tests.api_helpers import api_config
from tests.exact_helpers import TEST_INSTRUMENT


SESSIONS = {role: f"FIX.4.4:MOCKX1->{role}"
            for role in ("replay_buy", "replay_sell", "controller")}


def configuration(*, latency_ms=0, same_participant=False):
    values = api_config(fix_enabled=True)
    values["latency"] = {"network_ms": 0, "engine_ms": latency_ms}
    values["fix"]["session_participants"] = {
        session: "same" if same_participant else role for role, session in SESSIONS.items()
    }
    return validate_config(values, environ={})


def prepared(events=()):
    snapshot = (SourceOrder("bid", Side.BUY, 99, 10),
                SourceOrder("ask", Side.SELL, 101, 10))
    return SimpleNamespace(snapshot=snapshot, snapshot_digest=source_digest(snapshot),
                           source_sha256="test-hash", source_symbol="TEST_SOURCE",
                           start_ns=0, events=lambda: iter(events),
                           assert_exact_runtime_precision=lambda: None)


def event(index, source_id, *, when=0, operation="ADD", side=Side.BUY, price=98):
    return SourceEvent(index, when, operation, source_id, side, price, 1, 1, 0, 204, "unused")


class FakeVenue:
    """Public transport fake: advance-time is the only trigger for delayed FIX."""

    def __init__(self, *, latency_ms=0, drop=False, controller_delay=0):
        self.current = datetime(1970, 1, 1, tzinfo=timezone.utc)
        self.latency_ms, self.drop = latency_ms, drop
        self.controller_delay = controller_delay
        self.orders = {}
        self.scheduled = []
        self.sent = []
        self.started = self.closed = self.active = False
        self.receive = None
        self.run_id = None
        self.generation = 1
        self.fingerprint = execution_config_fingerprint(configuration(latency_ms=latency_ms))
        self.requests = []
        self.controller_completed_ns = None
        self.http = httpx.AsyncClient(base_url="http://mockx.test",
                                      transport=httpx.MockTransport(self.request))

    async def start(self, receive, roles, timeout):
        self.started, self.receive = True, receive
        return {role: SESSIONS[role] for role in roles}

    async def close(self):
        self.closed = True

    async def drain(self, timeout):
        await asyncio.sleep(0)

    async def send(self, intent):
        self.sent.append((intent, time.monotonic_ns()))
        if self.drop:
            return
        if intent.role == "controller":
            asyncio.get_running_loop().call_later(self.controller_delay, self.deliver, intent)
        elif self.latency_ms:
            self.scheduled.append((self.current + timedelta(milliseconds=self.latency_ms), intent))
        else:
            self.deliver(intent)

    def deliver(self, intent):
        controller = intent.role == "controller"
        if controller:
            self.controller_completed_ns = time.monotonic_ns()
        elif intent.kind == "NEW" and intent.tif == "GTC":
            self.orders[intent.cl_ord_id] = (
                intent.side, intent.price_ticks, intent.quantity_lots)
        self.receive(FixReport(
            message_type="ExecutionReport", sender_comp_id=intent.role,
            cl_ord_id=intent.cl_ord_id, orig_cl_ord_id=intent.orig_cl_ord_id,
            status="CANCELLED" if controller or intent.tif == "IOC" else "NEW",
            exec_type="4" if controller or intent.tif == "IOC" else "0",
            leaves_qty="0" if controller or intent.tif == "IOC" else str(intent.quantity_lots),
            last_price=None, last_qty=None, text=None, order_id="OID-" + intent.cl_ord_id,
            exec_id="EXEC-" + intent.cl_ord_id, cum_qty="0",
            quantity=str(intent.quantity_lots), received_ns=time.monotonic_ns(),
        ))

    def request(self, request):
        self.requests.append((request.method, request.url.path))
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/api/v1/session/start":
            self.active, self.run_id = True, body["run_id"]
            seeds = body.get("snapshot", [])
            for seed in seeds:
                self.orders[seed["cl_ord_id"]] = (
                    seed["side"], int(float(seed["price"])), int(float(seed["quantity"])))
            payload = {"run_generation": 1, "starting_time": self.current.isoformat(),
                       "execution_config_fingerprint": self.fingerprint,
                       "seeded_orders": [{"cl_ord_id": seed["cl_ord_id"],
                                          "order_id": "OID-" + seed["cl_ord_id"]}
                                         for seed in seeds]}
        elif path == "/admin/advance-time":
            self.current = datetime.fromisoformat(body["target_time"])
            scheduled, self.scheduled = self.scheduled, []
            for due, intent in scheduled:
                if due <= self.current:
                    self.deliver(intent)
                else:
                    self.scheduled.append((due, intent))
            payload = {"current_time": self.current.isoformat()}
        elif path == "/book/TEST":
            levels = defaultdict(lambda: [0, 0])
            for side, price, quantity in self.orders.values():
                levels[side, price][0] += quantity
                levels[side, price][1] += 1
            payload = {"sequence": len(self.orders), "bids": [], "asks": []}
            for (side, price), (quantity, count) in levels.items():
                payload["bids" if side == "BUY" else "asks"].append(
                    {"price": str(price), "qty": str(quantity), "order_count": count})
        elif path == "/stats":
            payload = {"run_id": self.run_id, "run_active": self.active,
                       "run_generation": self.generation}
        elif path == "/api/v1/session/end":
            self.active = False
            payload = {}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=payload)


@pytest.mark.asyncio
async def test_interactive_fix_seeding_pumps_nonzero_latency_before_starting_replay():
    venue = FakeVenue(latency_ms=2)
    runner = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(latency_ms=2),
                          venue.http, venue, options=ReplayOptions(
                              profile="interactive", seed_mode="fix", timeout=0.05))
    try:
        result = await runner.run()
        assert result["status"] == "completed"
        assert len(venue.orders) == 2
        assert venue.current > datetime(1970, 1, 1, tzinfo=timezone.utc)
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_ready_source_rows_keep_global_order_when_dispatch_accumulates():
    venue = FakeVenue()
    runner = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(), venue.http, venue,
                          options=ReplayOptions(profile="interactive"))
    venue.receive = runner.receive_report
    first = event(1, "a", operation="MATCH", side=Side.SELL, price=101)
    second = event(2, "b", operation="MATCH", side=Side.SELL, price=101)
    third = event(3, "a", operation="MATCH", side=Side.SELL, price=101)
    runner.queued = {"a": deque([first, third]), "b": deque([second])}
    try:
        await runner._flush_queued()
        assert [intent.event_index for intent, _ in venue.sent] == [1, 2, 3]
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_controller_pending_does_not_block_replay_and_eof_drains_it():
    # Allow several Windows timer quanta for the independent actors to run.
    venue = FakeVenue(controller_delay=0.3)
    events = (event(1, "extra-bid"), event(2, "later-bid", when=150_000_000, price=97))
    runner = ReplayRunner(prepared(events), TEST_INSTRUMENT, configuration(),
                          venue.http, venue, options=ReplayOptions(
                              profile="interactive", timeout=1.5),
                          controller_config=ControllerConfig(enabled=True, reaction_interval_ns=1))
    observations = []
    observe_target = runner.controller.observe_target

    def capture_target(target, observed_ns):
        observations.append(observed_ns)
        observe_target(target, observed_ns)

    runner.controller.observe_target = capture_target

    async def feed():
        runner.controller.observe_book(
            (DepthLevel(99, 10), DepthLevel(98, 10)),
            (DepthLevel(101, 10), DepthLevel(103, 10)), time.monotonic_ns())
        while not runner._eof:
            await asyncio.sleep(0.001)

    runner._feed = feed
    try:
        result = await runner.run()
        controller_sends = [sent for intent, sent in venue.sent if intent.role == "controller"]
        later_send = next(sent for intent, sent in venue.sent if intent.event_index == 2)
        assert len(controller_sends) == 1
        assert controller_sends[0] < later_send < venue.controller_completed_ns
        # Initial snapshot target is released first, then each due increment.
        assert observations[2] >= runner._wall_start + 150_000_000
        assert result["completed_requests"] == 3
        assert not runner.pending and runner.controller.stopped and venue.closed
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_missing_response_fails_eof_drain_and_cleans_up():
    venue = FakeVenue(drop=True)
    runner = ReplayRunner(prepared([event(1, "new")]), TEST_INSTRUMENT,
                          configuration(), venue.http, venue,
                          options=ReplayOptions(profile="interactive", timeout=0.03))
    try:
        with pytest.raises(ReplayError, match="timeout|timed out"):
            await runner.run()
        assert venue.closed and not venue.active and runner.controller.stopped
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_exact_preflight_rejects_latency_before_opening_any_transport():
    venue = FakeVenue()
    runner = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(latency_ms=1),
                          venue.http, venue)
    try:
        with pytest.raises(ReplayError, match="zero delays"):
            await runner.run()
        assert not venue.started and not venue.requests
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_same_participant_is_rejected_before_creating_a_run():
    venue = FakeVenue()
    runner = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(same_participant=True),
                          venue.http, venue)
    try:
        with pytest.raises(ReplayError, match="distinct participants"):
            await runner.run()
        assert venue.started and venue.closed and not venue.requests
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_existing_active_run_is_never_replaced_by_replay_start():
    venue = FakeVenue()
    venue.active, venue.run_id = True, "somebody-elses-run"
    runner = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(), venue.http, venue)
    try:
        with pytest.raises(ReplayError, match="active"):
            await runner.run()
        assert venue.active and venue.run_id == "somebody-elses-run"
        assert all(method != "POST" for method, _ in venue.requests)
        assert venue.closed
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_controller_ids_are_unique_across_runs_even_with_explicit_source_prefix():
    venue = FakeVenue()
    options = ReplayOptions(profile="interactive", id_prefix="fixed-source-prefix")
    try:
        first = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(), venue.http, venue,
                             options=options, controller_config=ControllerConfig(enabled=True))
        second = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(), venue.http, venue,
                              options=options, controller_config=ControllerConfig(enabled=True))
        assert first.controller.config.id_prefix != second.controller.config.id_prefix
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_replay_never_advances_a_replacement_generation():
    venue = FakeVenue()
    runner = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(), venue.http, venue)
    runner._generation = 1
    runner._current_time = venue.current
    venue.active, venue.run_id, venue.generation = True, runner.options.run_id, 2
    try:
        with pytest.raises(ReplayError, match="generation"):
            await runner._advance(venue.current)
        assert ("POST", "/admin/advance-time") not in venue.requests
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_cleanup_leaves_same_named_replacement_generation_active():
    venue = FakeVenue()
    runner = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(), venue.http, venue)

    async def replaced():
        venue.generation = 2
        raise ReplayError("original failure")

    runner._exact = replaced
    try:
        with pytest.raises(ReplayError, match="original failure"):
            await runner.run()
        assert venue.active
        assert ("POST", "/api/v1/session/end") not in venue.requests
        assert venue.closed
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_mismatched_server_settings_cannot_qualify_an_empty_interval():
    venue = FakeVenue()
    venue.fingerprint = execution_config_fingerprint(configuration(latency_ms=1))
    runner = ReplayRunner(prepared(), TEST_INSTRUMENT, configuration(), venue.http, venue)
    try:
        with pytest.raises(ReplayError, match="configuration"):
            await runner.run()
        assert not venue.active and venue.closed
    finally:
        await venue.http.aclose()


@pytest.mark.asyncio
async def test_eof_waits_for_passive_report_on_other_session():
    class DelayedMaker(FakeVenue):
        async def send(self, intent):
            await super().send(intent)
            if intent.tif == "IOC":
                self.maker_received = asyncio.Event()
                seed = next(key for key, (side, _, _) in self.orders.items() if side == "SELL")

                def deliver_maker():
                    assert not self.closed, "Passive report arrived after transport close"
                    self.receive(FixReport(
                        message_type="ExecutionReport", sender_comp_id="replay_sell",
                        cl_ord_id=seed, orig_cl_ord_id=None, status="PARTIAL", exec_type="F",
                        leaves_qty="9", last_price="101", last_qty="1", text=None,
                        order_id="OID-" + seed, exec_id="delayed-maker", cum_qty="1",
                        quantity="10", received_ns=time.monotonic_ns()))
                    self.maker_received.set()

                self.handle = asyncio.get_running_loop().call_later(0.03, deliver_maker)

        async def drain(self, timeout):
            await asyncio.wait_for(self.maker_received.wait(), timeout)

    venue = DelayedMaker()
    runner = ReplayRunner(prepared([event(1, "ask", operation="MATCH", side=Side.SELL, price=101)]),
                          TEST_INSTRUMENT, configuration(), venue.http, venue,
                          options=ReplayOptions(profile="interactive", timeout=0.2))
    try:
        await runner.run()
        assert runner.ledger.orders_snapshot()["ask"].leaves_qty_lots == 9
        assert venue.maker_received.is_set() and venue.closed
    finally:
        venue.handle.cancel()
        await venue.http.aclose()



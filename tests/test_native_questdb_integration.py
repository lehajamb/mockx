"""Opt-in real QuestDB/WAL tests; use a fresh native-schema test database."""

import asyncio
import os
from uuid import uuid4

import httpx
import pytest

from src.mockx.configuration import validate_config
from src.mockx.journal_reader import NativeJournalReader
from src.mockx.main import MockExchange
from src.mockx.models import SessionIdentity
from src.mockx.questdb_writer import QuestDBMetricsSink
from tests.api_helpers import api_config, NullFIXGateway, FIX_CONFIG
from tests.test_fix_ingress_pipeline import _new_order, _session
from tests.test_itch_integration import (
    CapturingMoldTransport,
    decode_packets,
    replay_displayed_book,
    authoritative_wire_book,
)

URL = os.environ.get("MOCKX_TEST_QUESTDB_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="Set MOCKX_TEST_QUESTDB_URL for real QuestDB integration"
)


class RecordingSink:
    def __init__(self, sink):
        self.sink = sink
        self.records = []

    async def write(self, records):
        await self.sink.write(records)
        self.records.extend(records)

    async def health_check(self):
        await self.sink.health_check()

    async def flush(self):
        await self.sink.flush()

    async def close(self):
        await self.sink.close()


def query(client, sql):
    response = client.get(URL + "/exec", params={"query": sql})
    response.raise_for_status()
    data = response.json()
    assert "error" not in data, data
    columns = [c["name"] for c in data["columns"]]
    return [dict(zip(columns, row)) for row in data["dataset"]]


async def wait_rows(client, run_id, expected):
    for _ in range(200):
        result = query(
            client,
            f"SELECT * FROM exchange_events WHERE run_id='{run_id}' ORDER BY event_sequence",
        )
        if len(result) == expected:
            return result
        await asyncio.sleep(0.05)
    pytest.fail(f"WAL visibility did not reach {expected} rows")


@pytest.mark.asyncio
async def test_two_venues_native_book_and_idempotent_http_retries():
    credentials = (
        os.environ.get("MOCKX_TEST_QUESTDB_USERNAME", "admin"),
        os.environ.get("MOCKX_TEST_QUESTDB_PASSWORD", "quest"),
    )
    client = httpx.Client(auth=credentials, timeout=10)
    exchanges = []
    run_ids = []
    try:
        for venue in ("A", "B"):
            raw = api_config()
            raw["exchange_name"] = "integration-" + venue + "-" + str(uuid4())
            raw["fix"]["session_participants"] = {
                "FIX.4.4:MOCKX->CLIENT": "liquidity",
                "FIX.4.4:MOCKX->CLIENT2": "strategy",
            }
            raw["questdb"].update(
                url=URL,
                health_url=os.environ.get("MOCKX_TEST_QUESTDB_HEALTH_URL", URL),
                username=credentials[0],
                password=credentials[1],
            )
            sink = RecordingSink(
                QuestDBMetricsSink(
                    url=URL,
                    health_url=raw["questdb"]["health_url"],
                    username=credentials[0],
                    password=credentials[1],
                    request_timeout_ms=10000,
                )
            )
            exchange = MockExchange(
                validate_config(raw, environ={}),
                str(FIX_CONFIG),
                persistence_sink=sink,
                fix_gateway_factory=NullFIXGateway,
            )
            exchanges.append(exchange)
            packets = []
            exchange.itch_feed.transport_factory = (
                lambda config, on_error, packets=packets: CapturingMoldTransport(
                    config, on_error=on_error, packets=packets
                )
            )
            await exchange.start_components()
            delivered = []
            exchange.fix_app.transport_sender = (
                lambda transport, report: delivered.append(report)
            )
            for name in ("CLIENT", "CLIENT2"):
                session = _session(name)
                exchange.fix_app.register_session(
                    SessionIdentity(
                        session.toString(),
                        sender_comp_id="MOCKX",
                        target_comp_id=name,
                        alias="strategy_bot" if name == "CLIENT2" else "liquidity_bot",
                    ),
                    object(),
                )
            await exchange.lifecycle.start_session("same-scenario")
            run_id = exchange.lifecycle.run_id
            run_ids.append(run_id)
            for message, session in (
                (_new_order("passive", 1, side="2", qty=7), _session("CLIENT")),
                (_new_order("taker", 1, qty=4), _session("CLIENT2")),
            ):
                exchange.fix_app.fromApp(message, session)
                await exchange.fix_app.wait_for_ingress()
                await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
                assert (await exchange.metrics.flush()).successful
                visible = await wait_rows(client, run_id, len(sink.records))
                reader = NativeJournalReader()
                reader.apply_rows(visible)
                native = [
                    (o.logical_order_id, o.qty_lots, o.liquidity_category)
                    for o in reader.book.orders.values()
                ]
                live = [
                    (o.id, o.leaves_qty_lots, o.liquidity_category.value)
                    for level in exchange.engine.books["TEST"].asks
                    for o in level.orders
                ]
                assert native == live
                assert replay_displayed_book(decode_packets(packets)[1])[
                    0
                ] == authoritative_wire_book(exchange)
            await exchange.lifecycle.end_session()
            # Replay an already acknowledged HTTP transaction, preserving every identity.
            await sink.sink.write(tuple(sink.records))
            visible = await wait_rows(client, run_id, len(sink.records))
            reader = NativeJournalReader()
            reader.apply_rows(visible)
            assert reader.run_ended and not reader.run_failed
            assert sum(r["kind"] == "INSTRUMENT" for r in visible) == 1
            assert all(r["venue_id"] == raw["exchange_name"] for r in visible)
        assert len(set(run_ids)) == 2
    finally:
        for exchange in exchanges:
            await exchange.stop_components()
        client.close()

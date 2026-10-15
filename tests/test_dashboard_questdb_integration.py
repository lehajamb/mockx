"""Opt-in QuestDB 9.4.3 array/projector/dashboard SQL integration."""

import json
import os
from pathlib import Path
import time
from uuid import uuid4

import pytest
from questdb.ingress import Sender, TimestampNanos

from examples.dashboard.projector import Projector, QuestDBProjectionStore, sql_literal
from src.mockx.questdb_writer import QUESTDB_SCHEMAS


@pytest.mark.skipif(not os.getenv("MOCKX_DASHBOARD_TEST_URL"), reason="isolated QuestDB URL required")
def test_projector_array_ingestion_and_all_dashboard_queries():
    url = os.environ["MOCKX_DASHBOARD_TEST_URL"]
    run_id = str(uuid4())
    venue = "DASHBOARD_TEST"

    class ScopedStore(QuestDBProjectionStore):
        def query(self, query):
            if query == "SELECT run_id, max(event_sequence) AS head FROM exchange_events GROUP BY run_id":
                query = query.replace(" GROUP BY", f" WHERE run_id={sql_literal(run_id)} GROUP BY")
            return super().query(query)

    store = ScopedStore(url, os.getenv("MOCKX_DASHBOARD_TEST_USER", "admin"), os.getenv("MOCKX_DASHBOARD_TEST_PASSWORD", "quest"))
    store.query(QUESTDB_SCHEMAS[0].ddl)
    store.ensure_schema()
    start = 1760000000000000000
    payloads = [
        ("RUN_STARTED", 1, 0, 0, {"run_label": "dashboard-integration"}),
        ("INSTRUMENT", 1, 1, 0, {"symbol": "TEST", "tick_size": "0.01", "lot_size": "0.001", "price_display_precision": 2, "quantity_display_precision": 3}),
        ("COMMAND_COMPLETE", 1, 2, 0, {"event_count": 2}),
        ("BOOK_ADDED", 2, 0, 1, {"order_id": 1, "symbol": "TEST", "side": "BUY", "price_ticks": 100, "qty_lots": 5}),
        ("BOOK_ADDED", 2, 1, 1, {"order_id": 2, "symbol": "TEST", "side": "SELL", "price_ticks": 101, "qty_lots": 1}),
        ("BOOK_ADDED", 2, 2, 1, {"order_id": 3, "symbol": "TEST", "side": "SELL", "price_ticks": 102, "qty_lots": 2}),
        ("COMMAND_COMPLETE", 2, 3, 1, {"event_count": 3}),
        ("TRADE", 3, 0, 2, {"symbol": "TEST", "price_ticks": 101, "qty_lots": 1, "notional_tick_lots": 101}),
        ("BOOK_EXECUTED", 3, 1, 2, {"symbol": "TEST", "order_id": 2, "qty_lots": 1, "remaining_qty_lots": 0}),
        ("COMMAND_COMPLETE", 3, 2, 2, {"event_count": 2}),
        ("TRADE", 4, 0, 2, {"symbol": "TEST", "price_ticks": 102, "qty_lots": 2, "notional_tick_lots": 204}),
        ("BOOK_EXECUTED", 4, 1, 2, {"symbol": "TEST", "order_id": 3, "qty_lots": 2, "remaining_qty_lots": 0}),
        ("COMMAND_COMPLETE", 4, 2, 2, {"event_count": 2}),
    ]
    sender = Sender(*store.sender_args, **store.sender_options)
    try:
        sender.establish()
        types = dict(QUESTDB_SCHEMAS[0].columns)
        for sequence, (kind, command, ordinal, seconds, fields) in enumerate(payloads, 1):
            symbols = {"run_id": run_id, "venue_id": venue, "kind": kind}
            columns = {"event_sequence": sequence, "event_timestamp_ns": start+seconds*1000000000, "command_sequence": command, "ordinal": ordinal}
            for name, value in fields.items():
                if types[name] == "VARCHAR" and isinstance(value, int):
                    value = str(value)
                (symbols if types[name] == "SYMBOL" else columns)[name] = value
            sender.row("exchange_events", symbols=symbols, columns=columns, at=TimestampNanos(columns["event_timestamp_ns"]))
        sender.flush()
    finally:
        sender.close(flush=False)
    projector = Projector(store, from_start=True)
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        projector.tick()
        rows = store.query(f"SELECT bids,asks,source_sequence FROM dashboard_depth WHERE run_id={sql_literal(run_id)} ORDER BY source_sequence DESC LIMIT 1")
        if rows and rows[0]["source_sequence"] == 13:
            break
        time.sleep(0.05)
    assert rows[0]["bids"][0][0] == 1.0
    assert rows[0]["bids"][1][0] == 0.005
    assert all(value is None for value in rows[0]["asks"][0])
    dashboard = json.loads((Path(__file__).resolve().parents[1] / "examples/dashboard/dashboards/mockx-native.json").read_text())
    replacement = {
        "${venue:sqlstring}": sql_literal(venue), "${run:sqlstring}": sql_literal(run_id),
        "${symbol:sqlstring}": "'TEST'", "$__toTime": "cast(1760001000000000 AS timestamp)",
        "$__timeFilter(timestamp)": "timestamp >= cast(1760000000000000 AS timestamp) AND timestamp <= cast(1760001000000000 AS timestamp)",
    }
    try:
        for mode in ("Live", "Historical"):
            results = {}
            for panel in dashboard["panels"]:
                query = panel["targets"][0]["rawSql"].replace("${mode:sqlstring}", sql_literal(mode))
                for original, value in replacement.items():
                    query = query.replace(original, value)
                results[panel["id"]] = store.query(query)
                assert results[panel["id"]], panel["title"]
            assert sum(row["Volume"] for row in results[6]) == pytest.approx(0.003)
            assert results[7][0]["Open"] == pytest.approx(1.01)
            assert results[7][-1]["Close"] == pytest.approx(1.02)
            assert results[8][-1]["VWAP"] == pytest.approx(305/300)
    finally:
        store.close()

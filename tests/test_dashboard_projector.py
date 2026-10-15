"""Completed source cuts, exact native reduction and bounded display projections."""

import json
import math
from pathlib import Path
import re
import subprocess
import shutil

import pytest

from examples.dashboard.projector import (
    CursorProjection, DepthProjection, Projector, decode_rows,
    QuestDBProjectionStore, DEPTH_COLUMNS, DEPTH_KEYS, CURSOR_COLUMNS,
)


def row(sequence, kind, command=1, ordinal=0, timestamp=10, **fields):
    return dict(run_id="run-a", venue_id="VENUE", event_sequence=sequence,
                event_timestamp_ns=timestamp, kind=kind, command_sequence=command,
                ordinal=ordinal, **fields)


def source_rows():
    return [
        row(1, "RUN_STARTED", run_label="demo"),
        row(2, "INSTRUMENT", ordinal=1, symbol="TEST", tick_size="0.01", lot_size="0.001",
            price_display_precision=2, quantity_display_precision=3),
        row(3, "COMMAND_COMPLETE", ordinal=2, event_count=2),
        row(4, "BOOK_ADDED", command=2, timestamp=20, order_id=1,
            symbol="TEST", side="BUY", price_ticks=100, qty_lots=5),
        row(5, "COMMAND_COMPLETE", command=2, ordinal=1, timestamp=20, event_count=1),
    ]


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.writes = []
        self.fail_write = False
        self.queries = []

    def query(self, query):
        self.queries.append(query)
        if "GROUP BY run_id" in query:
            return [{"run_id": "run-a", "head": max(row["event_sequence"] for row in self.rows)}]
        cursor = int(re.search(r"event_sequence>(\d+)", query).group(1))
        limit = int(re.search(r"LIMIT (\d+)", query).group(1))
        return [row for row in self.rows if row["event_sequence"] > cursor][:limit]

    def write(self, rows):
        self.writes.append(rows)
        if self.fail_write:
            raise RuntimeError("HTTP response lost")


def projector(store, **kwargs):
    return Projector(store, from_start=True, clock=lambda: 0, wall_clock=lambda: 1000, **kwargs)


def depth(store):
    return [row for batch in store.writes for row in batch if isinstance(row, DepthProjection)]


def test_partial_command_cannot_enter_depth_and_every_side_has_40_slots():
    store = FakeStore(source_rows()[:-1])
    p = projector(store, page_size=2)
    p.tick()
    snapshot = depth(store)[-1]
    assert snapshot.source_sequence == 3
    assert len(snapshot.bids) == 2 and len(snapshot.bids[0]) == 40
    assert all(math.isnan(price) for price in snapshot.bids[0])
    store.rows.append(source_rows()[-1])
    p.tick()
    snapshot = depth(store)[-1]
    assert snapshot.source_sequence == 5
    assert snapshot.bids[0][0] == 1.0
    assert snapshot.bids[1][0] == 0.005
    assert math.isnan(snapshot.asks[0][0])


def test_old_delivery_timestamp_never_creates_a_backdated_book_snapshot():
    store = FakeStore(source_rows()+[
        row(6, "REPORT_DELIVERED", command=3, timestamp=1),
        row(7, "COMMAND_COMPLETE", command=3, ordinal=1, timestamp=1, event_count=1),
    ])
    p = projector(store)
    p.tick()
    snapshot = depth(store)[-1]
    assert snapshot.source_sequence == 5
    assert snapshot.source_timestamp_ns == 20
    cursor = store.writes[-1][-1]
    assert isinstance(cursor, CursorProjection)
    assert cursor.source_sequence == 7
    assert cursor.book_sequence == 5
    assert cursor.state == "LIVE"
    p.tick()
    assert len(depth(store)) == 1


def test_failed_write_retries_identical_cut_before_reading_new_events():
    store = FakeStore(source_rows())
    p = projector(store)
    store.fail_write = True
    with pytest.raises(RuntimeError):
        p.tick()
    pending = p.runs["run-a"].pending
    store.rows.extend([
        row(6, "BOOK_DELETED", command=3, timestamp=30, order_id=1, symbol="TEST"),
        row(7, "COMMAND_COMPLETE", command=3, ordinal=1, timestamp=30, event_count=1),
    ])
    store.fail_write = False
    p.tick()
    assert store.writes[-1] is pending
    assert p.runs["run-a"].reader.seen_sequence == 5
    p.tick()
    assert depth(store)[-1].source_sequence == 7
    assert math.isnan(depth(store)[-1].bids[0][0])


def test_invalid_completed_book_command_keeps_valid_prefix_and_exposes_failure():
    rows = source_rows()+[
        row(6, "BOOK_ADDED", command=3, timestamp=30, order_id=2,
            symbol="TEST", side="SELL", price_ticks=99, qty_lots=1),
        row(7, "COMMAND_COMPLETE", command=3, ordinal=1, timestamp=30, event_count=1),
    ]
    store = FakeStore(rows)
    p = projector(store)
    p.tick()
    assert depth(store)[-1].source_sequence == 5
    cursor = store.writes[-1][-1]
    assert cursor.state == "INVALID"
    assert "crossed" in cursor.error
    assert list(p.runs["run-a"].reader.book.orders) == [1]


def test_http_json_preserves_large_native_sequences():
    sequence = 9007199254740993
    assert decode_rows({"columns": [{"name": "event_sequence"}], "dataset": [[sequence]]}) == [{"event_sequence": sequence}]
    with pytest.raises(RuntimeError):
        decode_rows({"error": "table missing"})


@pytest.mark.parametrize("changed", [
    {"walEnabled": False}, {"partitionBy": "NONE"},
    {"dedup": False}, {"designatedTimestamp": "published_at_ns"},
])
def test_projection_schema_rejects_incompatible_table_metadata(changed):
    store = QuestDBProjectionStore.__new__(QuestDBProjectionStore)

    def query(sql):
        table = "dashboard_depth" if "dashboard_depth" in sql else "dashboard_cursor"
        if sql.startswith("CREATE TABLE"):
            return []
        if "table_columns" in sql:
            columns = DEPTH_COLUMNS if table == "dashboard_depth" else CURSOR_COLUMNS
            keys = DEPTH_KEYS if table == "dashboard_depth" else {"timestamp", "run_id"}
            return [{"column": name, "type": kind, "upsertKey": name in keys} for name, kind in columns.items()]
        metadata = {"table_name": table, "designatedTimestamp": "timestamp", "partitionBy": "DAY", "walEnabled": True, "dedup": True}
        metadata.update(changed)
        return [metadata]

    store.query = query
    with pytest.raises(ValueError, match="metadata"):
        store.ensure_schema()


def test_provisioned_dashboard_selects_native_run_and_simulated_time():
    root = Path(__file__).resolve().parents[1]
    dashboard = json.loads((root / "examples/dashboard/dashboards/mockx-native.json").read_text())
    assert dashboard["refresh"] == "250ms"
    assert {item["name"] for item in dashboard["templating"]["list"]} == {"venue", "run", "symbol", "mode"}
    sql = "\n".join(target["rawSql"] for panel in dashboard["panels"] for target in panel["targets"])
    assert "ORDER BY source_sequence DESC" in sql
    assert "'COMMAND_COMPLETE'" in sql
    assert "dateadd('h',-1,clock.last_simulated_time)" in sql
    assert "max(timestamp) AS last_simulated_time" in sql
    assert "${mode:sqlstring}='Historical'" in sql
    assert "notional_tick_lots" in sql
    assert "ORDER BY t.timestamp,t.event_sequence" in sql
    assert "dashboard_cursor" in sql
    script = (root / "examples/dashboard/market-depth.js").read_text()
    assert next(panel for panel in dashboard["panels"] if panel["id"] == 4)["options"]["script"] == script


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for chart script execution")
def test_plotly_script_handles_current_grafana_arrays_and_empty_books():
    root = Path(__file__).resolve().parents[1]
    script = (root / "examples/dashboard/market-depth.js").read_text()
    program = """
const fs=require('fs'); const script=fs.readFileSync(0,'utf8');
const run=new Function('data',script);
const fields=['bprices','bvolumes','bcumvolumes','aprices','avolumes','acumvolumes'];
const values=[[100,null],[2,null],[2,null],[101,null],[3,null],[3,null]];
for (const vector of [false,true]) {
 const data={series:[{fields:fields.map((name,i)=>({name,values:vector?{get:()=>values[i]}:[values[i]]}))}]};
 const result=run(data); if(result.data[0].x[0]!==100 || result.data[1].x[0]!==101) process.exit(2);
}
if(run({series:[]}).data.length!==0) process.exit(3);
const oneSide={series:[{fields:fields.map((name,i)=>({name,values:[i<3?values[i]:[]]}))}]};
if(run(oneSide).layout.shapes.some(shape=>shape.line.color==='white')) process.exit(4);
console.log('chart scripts passed');
"""
    result = subprocess.run(["node", "-e", program], input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr

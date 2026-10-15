"""Compare native FIX submission paths through MockX's required stack.

This is an in-process producer benchmark: FIX parsing, matching, journal
admission and ITCH encoding/packetization are real. FIX network transport is
replaced by a report collector, and encoded UDP packets are counted rather
than transmitted. GLIMPSE uses its real listener. Dashboard load consumes
acknowledged journal rows through the external reconstruction reader, without
requesting engine snapshots. The reader shares this process for measurement,
so process CPU includes its work; this does not measure browser rendering.

Run from the repository root after installing MockX:
    python scripts/benchmark_native_exchange.py --depths 100,1000 --orders 500
    python scripts/benchmark_native_exchange.py --orders 1000 --questdb-url http://localhost:9000
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mockx.configuration import validate_config
from src.mockx.gateway import fix
from src.mockx.main import MockExchange
from src.mockx.metrics import ENGINE_COMMAND_DURATION
from src.mockx.models import ExecutionReport, ExecutionType, SessionIdentity
from src.mockx.moldudp64_transport import MoldUDP64Transport
from src.mockx.questdb_writer import QuestDBMetricsSink
from src.mockx.journal_reader import NativeJournalReader


SESSION = "FIX.4.4:MOCKX->BENCH"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class ProducerGateway:
    """Replace the network acceptor without bypassing FIX application parsing."""

    def __init__(self, *args, application=None, **kwargs):
        self.application = application
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


class PacketCounter:
    def __init__(self):
        self.packets = 0
        self.bytes = 0

    def sendto(self, data, address):
        self.packets += 1
        self.bytes += len(data)

    def close(self):
        return None


class CountedMoldTransport(MoldUDP64Transport):
    async def start(self):
        self._next_sequence = 1
        self._transport = PacketCounter()
        self._session_active = True


class CountedSink:
    """Optional real QuestDB, otherwise acknowledged in-memory writes."""

    def __init__(self, *, delay_ms=0, delegate=None, capture_rows=False):
        self.delay_ms = delay_ms
        self.delegate = delegate
        self.rows = self.bytes = self.writes = 0
        self.capture_rows = capture_rows
        self.records = []

    async def health_check(self):
        if self.delegate is not None:
            await self.delegate.health_check()

    async def write(self, records):
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        if self.delegate is not None:
            await self.delegate.write(records)
        self.rows += len(records)
        self.bytes += sum(record.encoded_size_bytes for record in records)
        self.writes += 1
        if self.capture_rows:
            self.records.extend({**record.symbol_map, **record.column_map} for record in records)

    async def flush(self):
        if self.delegate is not None:
            await self.delegate.flush()

    async def close(self):
        if self.delegate is not None:
            await self.delegate.close()


def order_message(sequence, item):
    cl_ord_id, side, price, quantity = item
    message = fix.Message()
    message.getHeader().setField(fix.MsgType("D"))
    message.getHeader().setField(fix.MsgSeqNum(sequence))
    for tag, value in [(11, cl_ord_id), (55, "BENCH"), (54, side),
                       (38, str(quantity)), (40, "2"), (44, str(price)), (59, "1")]:
        message.setField(fix.StringField(tag, value))
    return message


def list_message(sequence, items, list_id):
    message = fix.Message()
    message.getHeader().setField(fix.MsgType("E"))
    message.getHeader().setField(fix.MsgSeqNum(sequence))
    for tag, value in [(66, list_id), (394, "3"), (433, "1"), (414, "3"), (68, str(len(items)))]:
        message.setField(fix.StringField(tag, value))
    for ordinal, item in enumerate(items, 1):
        cl_ord_id, side, price, quantity = item
        group = fix.Group(73, 11)
        for tag, value in [(11, cl_ord_id), (67, str(ordinal)), (55, "BENCH"),
                           (54, side), (38, str(quantity)), (40, "2"), (44, str(price)), (59, "1")]:
            group.setField(fix.StringField(tag, value))
        message.addGroup(group)
    return message


async def measure_case(*, mode, depth, orders, window, delay_ms, dashboard_every, questdb_url):
    questdb_environment = {
        name: os.environ[name]
        for name in ("QUESTDB_HEALTH_URL", "QUESTDB_USERNAME", "QUESTDB_PASSWORD",
                     "QUESTDB_REQUEST_TIMEOUT_MS")
        if questdb_url and name in os.environ
    }
    config = validate_config({
        "exchange_name": "NATIVE-BENCH",
        "instruments": {"BENCH": {"tick_size": "1", "lot_size": "1",
                                  "price_display_precision": 0, "quantity_display_precision": 0}},
        "latency": {"network_ms": 0, "engine_ms": 0},
        "execution": {"reject_prob": 0.0, "random_seed": 42},
        "admin_api": {"host": "127.0.0.1", "port": free_port()},
        "fix": {"session_participants": {SESSION: "benchmark"}, "max_orders_per_list": window},
        "scheduler": {"mode": "external", "processing_batch_size": 128},
        "questdb": {"url": questdb_url or "http://127.0.0.1:9000",
                    "health_url": questdb_url or "http://127.0.0.1:9000",
                    "flush_batch_size": 256, "flush_batch_bytes": 262144,
                    "max_buffer_records": 4096, "max_buffer_bytes": 4194304},
        "moldudp64": {"multicast_port": free_port()},
        "soupbintcp": {"port": free_port(), "credentials": {"BENCH": "PASSWORD"}},
        "prometheus": {"enabled": False},
    }, environ=questdb_environment)
    sink = CountedSink(delay_ms=delay_ms, capture_rows=bool(dashboard_every and not questdb_url), delegate=(
        QuestDBMetricsSink(url=config.questdb.url, health_url=config.questdb.health_url,
                          username=config.questdb.username, password=config.questdb.password,
                          request_timeout_ms=config.questdb.request_timeout_ms)
        if questdb_url else None
    ))
    exchange = MockExchange(config, str(ROOT / "configs/mockx1.cfg"),
                            persistence_sink=sink, fix_gateway_factory=ProducerGateway)
    exchange.itch_feed.transport_factory = CountedMoldTransport
    session = fix.SessionID("FIX.4.4", "MOCKX", "BENCH")
    acknowledgments = set()
    def collect(_transport, report):
        if isinstance(report, ExecutionReport):
            if report.exec_type == ExecutionType.REJECTED:
                raise RuntimeError(f"Benchmark order rejected: {report.text}")
            if report.exec_type == ExecutionType.NEW:
                acknowledgments.add(report.cl_ord_id)
        return True
    exchange.fix_app.transport_sender = collect
    next_sequence = 1
    list_serial = 0
    engine_cpu_ns = 0
    original_cpu_observer = exchange.engine.on_cpu_duration
    def cpu_observer(metric, order_id, session_id, duration_ns, timestamp, *extra, **kwargs):
        nonlocal engine_cpu_ns
        if original_cpu_observer is not None:
            original_cpu_observer(metric, order_id, session_id, duration_ns, timestamp, *extra, **kwargs)
        if metric == ENGINE_COMMAND_DURATION:
            engine_cpu_ns += duration_ns
    exchange.engine.on_cpu_duration = cpu_observer
    dashboard_reader = NativeJournalReader() if dashboard_every else None
    dashboard_client = httpx.AsyncClient(
        base_url=questdb_url, timeout=config.questdb.request_timeout_ms / 1000,
        auth=(config.questdb.username, config.questdb.password)
        if config.questdb.username is not None else None,
    ) if dashboard_every and questdb_url else None

    async def poll_dashboard(run_id):
        """Read persistence only; never ask the exchange to serialize its book."""
        if dashboard_client is not None:
            while True:
                response = await dashboard_client.get("/exec", params={
                    "query": f"SELECT * FROM exchange_events WHERE run_id='{run_id}' "
                             f"AND event_sequence>{dashboard_reader.seen_sequence} ORDER BY event_sequence LIMIT 10000"
                })
                response.raise_for_status()
                payload = response.json()
                if "error" in payload:
                    raise RuntimeError(payload["error"])
                columns = [column["name"] for column in payload["columns"]]
                rows = [dict(zip(columns, values)) for values in payload["dataset"]]
                dashboard_reader.apply_rows(rows)
                if len(rows) < 10000:
                    break
        else:
            rows = sink.records[dashboard_reader.seen_sequence:]
            dashboard_reader.apply_rows(rows)
        return dashboard_reader.book.depth("BENCH", limit=40)
    peak_rows = peak_bytes = 0
    original_append = exchange.metrics_persistence._append_group
    def counted_append(records):
        nonlocal peak_rows, peak_bytes
        outcome = original_append(records)
        peak_rows = max(peak_rows, exchange.metrics_persistence.buffered_count)
        peak_bytes = max(peak_bytes, exchange.metrics_persistence.buffered_bytes)
        return outcome
    exchange.metrics_persistence._append_group = counted_append

    async def submit_chunk(items, use_list):
        nonlocal next_sequence, list_serial
        if use_list:
            list_serial += 1
            exchange.fix_app.fromApp(list_message(next_sequence, items, f"BENCH-LIST-{list_serial}"), session)
            next_sequence += 1
        else:
            for item in items:
                exchange.fix_app.fromApp(order_message(next_sequence, item), session)
                next_sequence += 1
        await exchange.fix_app.wait_for_ingress()
        await exchange.lifecycle.advance_to(exchange.scheduler.current_time)
        if exchange.lifecycle.run_failure is not None:
            raise RuntimeError(exchange.lifecycle.run_failure.message)
        if not {item[0] for item in items} <= acknowledgments:
            raise RuntimeError("Not every benchmark order received its NEW report")

    try:
        await exchange.start_components()
        exchange.fix_app.register_session(SessionIdentity(
            SESSION, sender_comp_id="MOCKX", target_comp_id="BENCH",
            alias="BENCH", participant_id="benchmark"), session)
        result = await exchange.lifecycle.start_session(f"benchmark:{mode}:{depth}")
        seed = [(f"SEED-B-{index}", "1", 10000 + index, 10) for index in range(depth)]
        seed += [(f"SEED-A-{index}", "2", 20000 + index, 10) for index in range(depth)]
        for offset in range(0, len(seed), window):
            await submit_chunk(seed[offset:offset + window], True)
        if not (await exchange.metrics.flush()).successful:
            raise RuntimeError("Benchmark seed persistence did not acknowledge")
        if dashboard_reader is not None:
            await poll_dashboard(result.run_id)
        baseline_rows, baseline_bytes, baseline_writes = sink.rows, sink.bytes, sink.writes
        baseline_packets = exchange.itch_feed._packets_sent
        baseline_messages = exchange.itch_feed._messages_sent
        engine_cpu_ns = peak_rows = peak_bytes = 0
        physical_before = next_sequence
        workload = [(f"LOAD-{index}", "1", 10000 + index % depth, 1) for index in range(orders)]
        chunk_size = 1 if mode == "sequential_D" else window
        viewer_polls = 0
        next_viewer = dashboard_every if dashboard_every else orders + 1
        cpu_start = time.process_time_ns()
        wall_start = time.perf_counter()
        for offset in range(0, orders, chunk_size):
            chunk = workload[offset:offset + chunk_size]
            await submit_chunk(chunk, mode == "native_E")
            completed = offset + len(chunk)
            while completed >= next_viewer:
                await poll_dashboard(result.run_id)
                viewer_polls += 1
                next_viewer += dashboard_every
        if not (await exchange.metrics.flush()).successful:
            raise RuntimeError("Timed persistence did not acknowledge")
        wall_seconds = time.perf_counter() - wall_start
        process_cpu_ns = time.process_time_ns() - cpu_start
        book = exchange.engine.books["BENCH"]
        book.assert_invariants()
        if exchange.engine.active_order_count() != 2 * depth + orders:
            raise RuntimeError("Submission modes produced unexpected live order counts")
        signature = [
            (side, level.price_ticks, [(order.cl_ord_id, order.leaves_qty_lots) for order in level.orders])
            for side, levels in [("B", book.bids), ("A", book.asks)] for level in levels
        ]
        digest = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
        return {
            "mode": mode, "depth_per_side": depth, "orders": orders, "window": window,
            "sink_delay_ms": delay_ms, "dashboard_every_orders": dashboard_every,
            "run_id": result.run_id, "orders_per_second": orders / wall_seconds,
            "wall_seconds_including_flush": wall_seconds,
            "process_cpu_ms": process_cpu_ns / 1e6, "engine_command_cpu_ms": engine_cpu_ns / 1e6,
            "physical_fix_messages": next_sequence - physical_before,
            "journal_rows": sink.rows - baseline_rows,
            "journal_logical_bytes": sink.bytes - baseline_bytes, "sink_writes": sink.writes - baseline_writes,
            "itch_packets": exchange.itch_feed._packets_sent - baseline_packets,
            "itch_messages": exchange.itch_feed._messages_sent - baseline_messages,
            "peak_buffer_rows": peak_rows, "peak_buffer_bytes": peak_bytes,
            "external_projection_polls": viewer_polls,
            "dashboard_event_lag_before_final_read": exchange.journal.sequence - dashboard_reader.sequence
            if dashboard_reader is not None else None,
            "final_book_sha256": digest,
            "backend": "questdb" if questdb_url else "acknowledged_memory",
        }
    finally:
        try:
            if exchange.lifecycle.run_id is not None:
                await exchange.lifecycle.end_session()
        finally:
            if dashboard_client is not None:
                await dashboard_client.aclose()
            await exchange.stop_components()


def positive_values(value):
    values = [int(part) for part in value.split(",")]
    if not values or any(number <= 0 for number in values):
        raise argparse.ArgumentTypeError("Expected comma-separated positive integers")
    return values


def nonnegative_values(value):
    values = [int(part) for part in value.split(",")]
    if not values or any(number < 0 for number in values):
        raise argparse.ArgumentTypeError("Expected comma-separated nonnegative integers")
    return values


async def run(args):
    results = []
    for depth in args.depths:
        for delay in args.sink_delays_ms:
            for dashboard in args.dashboard_every:
                fingerprints = set()
                for mode in ("sequential_D", "pipelined_D", "native_E"):
                    result = await measure_case(mode=mode, depth=depth, orders=args.orders,
                                                window=args.window, delay_ms=delay,
                                                dashboard_every=dashboard, questdb_url=args.questdb_url)
                    fingerprints.add(result["final_book_sha256"])
                    results.append(result)
                    print(json.dumps(result), flush=True)
                if len(fingerprints) != 1:
                    raise RuntimeError("Submission modes produced different final books")
    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depths", type=positive_values, default=[100, 1000])
    parser.add_argument("--orders", type=int, default=500)
    parser.add_argument("--window", type=int, default=100)
    parser.add_argument("--sink-delays-ms", type=nonnegative_values, default=[0, 5])
    parser.add_argument("--dashboard-every", type=nonnegative_values, default=[0, 100])
    parser.add_argument("--questdb-url", help="Use real QuestDB instead of the acknowledged memory sink")
    parser.add_argument("--output", help="Optionally write the result array to this JSON file")
    args = parser.parse_args()
    if args.orders <= 0 or not 1 <= args.window <= 1000:
        parser.error("orders must be positive and window must be between 1 and 1000")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

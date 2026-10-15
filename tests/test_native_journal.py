"""Completed-command durability, bounded admission, and native reconstruction."""

import asyncio
from datetime import datetime, timezone

import pytest

from src.mockx.effects import (
    BookCheckpoint,
    BookOrderState,
    CommandEffect,
    CommandEffects,
)
from src.mockx.journal import (
    ExchangeJournal,
    JournalAdmissionError,
    JournalFinalizationError,
)
from src.mockx.journal_reader import NativeJournalReader
from src.mockx.metrics import (
    BufferedMetricsWriter,
    InMemoryMetricsSink,
    MetricRecord,
    MetricsSinkError,
    PersistenceAddOutcome,
    PersistenceBackpressureTimeout,
)
from src.mockx.models import (
    DisplayedOrderAdded,
    DisplayedOrderExecuted,
    DisplayedOrderReduced,
    Side,
)
from tests.exact_helpers import TEST_INSTRUMENT


NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def record(sequence, text=""):
    return MetricRecord.create(
        "exchange_events",
        {"kind": "TEST"},
        {"text": text},
        run_id="run",
        event_sequence=sequence,
        event_timestamp_ns=sequence,
    )


def rows(sink):
    return [{**r.symbol_map, **r.column_map} for batch in sink.writes for r in batch]


def add(order_id, price=100, qty=5, side=Side.BUY):
    return DisplayedOrderAdded(
        1, order_id, "TEST", side, price, qty, NOW, logical_order_id=order_id
    )


async def setup(**kwargs):
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, **kwargs)
    journal = ExchangeJournal(writer, "VENUE")
    await journal.start_run("run", "scenario", (TEST_INSTRUMENT,), NOW)
    return sink, writer, journal


@pytest.mark.asyncio
async def test_command_is_never_split_at_row_or_byte_flush_limit():
    sink = InMemoryMetricsSink()
    sample = record(1)
    writer = BufferedMetricsWriter(
        sink, flush_batch_size=2, flush_batch_bytes=sample.encoded_size_bytes * 2
    )
    first = tuple(record(i) for i in range(1, 4))
    second = tuple(record(i) for i in range(4, 6))
    assert (
        await writer.add_group(first) == PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED
    )
    await writer.add_group(second)
    assert (await writer.flush()).successful
    assert sink.writes == [first, second]
    await writer.close()


@pytest.mark.asyncio
async def test_reader_waits_for_marker_and_applies_one_validated_sweep():
    sink, writer, journal = await setup()
    await journal.admit_command(
        CommandEffects(
            "new",
            NOW,
            (
                CommandEffect("book_change", add(1)),
                CommandEffect("book_change", add(2)),
            ),
        )
    )
    await writer.flush()
    all_rows = rows(sink)
    reader = NativeJournalReader()
    reader.apply_rows(all_rows[:3])
    assert reader.book.depth("TEST") == ((), ())
    reader.apply_rows(all_rows[3:-1])
    assert reader.book.depth("TEST") == ((), ())
    reader.apply_rows(all_rows[-1:])
    assert reader.book.depth("TEST") == (((100, 10),), ())
    assert list(reader.book.orders) == [1, 2]
    await journal.admit_command(
        CommandEffects(
            "sweep",
            NOW,
            (
                CommandEffect(
                    "book_change",
                    DisplayedOrderExecuted(
                        1, 1, "TEST", 100, 100, 5, 1, NOW, remaining_qty_lots=0
                    ),
                ),
                CommandEffect(
                    "book_change",
                    DisplayedOrderExecuted(
                        1, 2, "TEST", 100, 100, 2, 2, NOW, remaining_qty_lots=3
                    ),
                ),
            ),
        )
    )
    await writer.flush()
    reader.apply_rows(rows(sink)[len(all_rows) :])
    assert list(reader.book.orders) == [2]
    assert reader.book.depth("TEST") == (((100, 3),), ())
    await writer.close()


@pytest.mark.asyncio
async def test_failed_write_retains_entire_group_and_original_identities():
    class Recovering(InMemoryMetricsSink):
        failed = True
        attempts = []

        async def write(self, records):
            self.attempts.append(tuple(records))
            if self.failed:
                raise MetricsSinkError("outage")
            await super().write(records)

    sink = Recovering()
    writer = BufferedMetricsWriter(sink, flush_batch_size=10)
    original = (record(1), record(2))
    await writer.add_group(original)
    assert not (await writer.flush()).successful
    await writer.add_group((record(3),))
    sink.failed = False
    assert (await writer.flush()).successful
    assert sink.attempts[1][:2] == original
    assert all(a is b for a, b in zip(original, sink.attempts[1]))
    await writer.close()


@pytest.mark.asyncio
async def test_group_too_large_rejects_every_row_and_reserves_sequence():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(sink, flush_batch_size=1, max_buffer_records=3)
    journal = ExchangeJournal(writer, "VENUE")
    await journal.start_run("run", "scenario", (TEST_INSTRUMENT,), NOW)
    await writer.flush()
    before = journal.sequence
    with pytest.raises(JournalAdmissionError) as error:
        await journal.admit_command(
            CommandEffects(
                "large",
                NOW,
                tuple(CommandEffect("book_change", add(i)) for i in range(1, 5)),
            )
        )
    assert error.value.outcome == PersistenceAddOutcome.TOO_LARGE
    assert writer.buffered_count == 0
    assert journal.sequence == before + 5
    assert len(rows(sink)) == before
    await writer.close()


@pytest.mark.asyncio
async def test_headroom_progress_flushes_partial_batches_without_retry_bypass():
    sink = InMemoryMetricsSink()
    writer = BufferedMetricsWriter(
        sink, flush_batch_size=10, max_buffer_records=10, flush_interval_ms=10_000
    )
    calls = []
    writer.before_wait = lambda: calls.append("flush feed")
    await writer.add_group(tuple(record(i) for i in range(1, 9)))
    assert await writer.wait_for_pressure_relief(timeout_ms=500)
    assert writer.buffered_count <= 5
    assert calls == ["flush feed"]
    await writer.close()


@pytest.mark.asyncio
async def test_inflight_capacity_waits_until_progress_and_preserves_group():
    class Blocking(InMemoryMetricsSink):
        def __init__(self):
            super().__init__()
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def write(self, records):
            self.started.set()
            await self.release.wait()
            await super().write(records)

    sink = Blocking()
    writer = BufferedMetricsWriter(sink, flush_batch_size=1, max_buffer_records=3)
    first = (record(1), record(2))
    await writer.add_group(first)
    await sink.started.wait()
    second = (record(3), record(4))
    pending = asyncio.create_task(writer.add_group(second))
    await asyncio.sleep(0)
    assert writer.buffered_count == 2
    assert not pending.done()
    sink.release.set()
    assert await pending == PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED
    await writer.flush()
    assert sink.writes == [first, second]
    await writer.close()


@pytest.mark.asyncio
async def test_pressure_timeout_retains_admitted_records():
    class Failing(InMemoryMetricsSink):
        async def write(self, records):
            raise MetricsSinkError("outage")

    writer = BufferedMetricsWriter(
        Failing(), flush_batch_size=10, max_buffer_records=10
    )
    group = tuple(record(i) for i in range(1, 9))
    await writer.add_group(group)
    with pytest.raises(PersistenceBackpressureTimeout):
        await writer.wait_for_pressure_relief(timeout_ms=20)
    assert writer.buffered_count == len(group)
    assert not (await writer.close()).successful


@pytest.mark.asyncio
async def test_checkpoints_chunk_and_bootstrap_fifo_logical_identity_and_category():
    sink, writer, journal = await setup(flush_batch_size=3)
    await journal.admit_command(
        CommandEffects(
            "seed",
            NOW,
            tuple(CommandEffect("book_change", add(i, qty=i)) for i in range(1, 7)),
        )
    )
    snapshot = BookCheckpoint(
        "run",
        NOW,
        20,
        tuple(
            BookOrderState(i, "TEST", Side.BUY, 100, i, "market", 100 + i)
            for i in range(1, 7)
        ),
    )
    await journal.checkpoint(snapshot)
    await writer.flush()
    persisted = rows(sink)
    checkpoint = [
        row for row in persisted if str(row["kind"]).startswith("CHECKPOINT_")
    ]
    reader = NativeJournalReader()
    reader.bootstrap_checkpoint(
        checkpoint,
        metadata_rows=[
            row for row in persisted if row["kind"] in {"RUN_STARTED", "INSTRUMENT"}
        ],
    )
    assert reader.book.depth("TEST") == (((100, 21),), ())
    assert list(reader.book.orders) == list(range(1, 7))
    assert reader.book.orders[1].logical_order_id == 101
    assert reader.next_itch_sequence == 20
    assert reader.instruments["TEST"].tick_size == TEST_INSTRUMENT.tick_size
    incomplete = NativeJournalReader()
    incomplete.seen_sequence = int(checkpoint[0]["event_sequence"]) - 1
    incomplete.sequence = int(checkpoint[0]["cut_sequence"])
    incomplete.apply_rows(checkpoint[:-1])
    assert not incomplete.book.orders
    await writer.close()


def test_effect_capture_freezes_request_metadata():
    original = {"order_id": 1, "price_ticks": 100}
    effect = CommandEffect("order_submitted", original)
    original["price_ticks"] = 200
    assert effect.payload["price_ticks"] == 100
    with pytest.raises(TypeError):
        effect.payload["price_ticks"] = 300


@pytest.mark.asyncio
async def test_complete_checkpoint_cannot_hide_missing_or_wrong_book_history():
    sink, writer, journal = await setup()
    await journal.admit_command(
        CommandEffects("seed", NOW, (CommandEffect("book_change", add(1)),))
    )
    await journal.checkpoint(
        BookCheckpoint("run", NOW, 2, (BookOrderState(1, "TEST", Side.BUY, 100, 4),))
    )
    await writer.flush()
    reader = NativeJournalReader()
    with pytest.raises(ValueError, match="disagrees"):
        reader.apply_rows(rows(sink))
    assert reader.book.orders[1].qty_lots == 5
    await writer.close()


@pytest.mark.asyncio
async def test_end_does_not_admit_terminal_marker_before_prefix_acknowledgment():
    class Failing(InMemoryMetricsSink):
        async def write(self, records):
            raise MetricsSinkError("prefix outage")

    sink = Failing()
    writer = BufferedMetricsWriter(sink)
    journal = ExchangeJournal(writer, "VENUE")
    await journal.start_run("run", "scenario", (TEST_INSTRUMENT,), NOW)
    before = journal.sequence
    with pytest.raises(RuntimeError, match="prefix acknowledgment"):
        await journal.end_run(NOW)
    assert journal.sequence == before
    assert all(
        entry.record.symbol_map["kind"] != "RUN_ENDED" for entry in writer._buffer
    )
    await writer.close()


@pytest.mark.asyncio
async def test_unconfirmed_terminal_marker_has_distinct_fatal_failure():
    class FailTerminal(InMemoryMetricsSink):
        async def write(self, records):
            if any(record.symbol_map["kind"] == "RUN_ENDED" for record in records):
                raise MetricsSinkError("terminal outage")
            await super().write(records)

    sink = FailTerminal()
    writer = BufferedMetricsWriter(sink)
    journal = ExchangeJournal(writer, "VENUE")
    await journal.start_run("run", "scenario", (TEST_INSTRUMENT,), NOW)
    with pytest.raises(JournalFinalizationError) as error:
        await journal.end_run(NOW)
    assert error.value.code == "JOURNAL_END_UNCONFIRMED"
    assert journal._runs["run"].failed
    assert writer.buffered_count == 2
    await writer.close()


@pytest.mark.asyncio
async def test_late_delivery_keeps_original_run_scope_after_reset():
    sink, writer, journal = await setup()
    await journal.end_run(NOW)
    journal.detach_run()
    await journal.start_run("run2", "scenario", (TEST_INSTRUMENT,), NOW)
    first_cut = journal._runs["run"].sequence
    await journal.admit_command(
        CommandEffects(
            "delivery",
            NOW,
            (CommandEffect("report_delivered", {"exec_id": "E1"}),),
            run_id="run",
        )
    )
    await writer.flush()
    late = [
        row
        for row in rows(sink)
        if row["run_id"] == "run" and row["event_sequence"] > first_cut
    ]
    assert len(late) == 2
    assert late[0]["exec_id"] == "E1"
    assert journal.active_run_id == "run2"
    await writer.close()


@pytest.mark.asyncio
async def test_reader_rejects_gap_and_invalid_group_without_changing_book():
    sink, writer, journal = await setup()
    await journal.admit_command(
        CommandEffects("new", NOW, (CommandEffect("book_change", add(1)),))
    )
    await writer.flush()
    reader = NativeJournalReader()
    reader.apply_rows(rows(sink))
    cut = reader.sequence
    with pytest.raises(ValueError, match="gap"):
        reader.apply_rows([{**rows(sink)[-1], "event_sequence": cut + 2}])
    assert reader.sequence == cut
    bad = {
        **rows(sink)[-2],
        "event_sequence": cut + 1,
        "command_sequence": 3,
        "ordinal": 0,
        "kind": "BOOK_EXECUTED",
        "qty_lots": 100,
    }
    marker = {
        **rows(sink)[-1],
        "event_sequence": cut + 2,
        "command_sequence": 3,
        "event_count": 1,
        "ordinal": 1,
    }
    with pytest.raises(ValueError, match="exceeds"):
        reader.apply_rows([bad, marker])
    assert reader.book.orders[1].qty_lots == 5
    assert reader.sequence == cut
    await writer.close()

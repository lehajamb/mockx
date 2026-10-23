"""Local L3 source preflight, exact conversions and prepared replay integrity."""

import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import io
import json

import pytest

from examples.coinapi_replay.source import (
    SourceFormatError,
    SourceOrder,
    prepare_csv,
    source_digest,
)
from src.mockx.instruments import Instrument
from src.mockx.models import Side


SYMBOL = "COINBASE_SPOT_BTC_USD"
HEADER = "time_exchange,time_coinapi,is_buy,update_type,entry_px,entry_sx,order_id".split(",")
T0 = "2026-10-20T12:00:00.123456789Z"
T1 = "2026-10-20T12:00:00.123457789Z"
T2 = "2026-10-20T12:00:00.123458789Z"


@pytest.fixture
def instrument():
    return Instrument("BTC-USD", Decimal("1"), Decimal("0.1"), 0, 1)


def row(operation, source_id="a1", *, side="0", price="101", quantity="0.5", time=T1, received=None):
    return [time, received or time, side, operation, price, quantity, source_id]


def snapshot():
    return [
        row("SNAPSHOT", "b1", side="1", price="99", quantity="1.0", time=T0),
        row("SNAPSHOT", quantity="2.0", time=T0),
    ]


def write_csv(tmp_path, rows, *, delimiter=",", compressed=False, filename=None, header=None):
    path = tmp_path / (filename or ("source.csv.gz" if compressed else "source.csv"))
    path.parent.mkdir(parents=True, exist_ok=True)
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter)
    writer.writerow(header or HEADER)
    writer.writerows(rows)
    data = output.getvalue().encode("utf-8")
    path.write_bytes(gzip.compress(data, mtime=0) if compressed else data)
    return path


@pytest.mark.parametrize("compressed,delimiter", [(False, ","), (False, ";"), (True, ","), (True, ";")])
def test_prepare_exact_orders_events_digest_and_raw_file_hash(tmp_path, instrument, compressed, delimiter):
    path = write_csv(tmp_path, snapshot() + [
        row("MATCH"),
        row("SUB", "b1", side="1", price="99", quantity="0.2", time=T2),
        row("SET", quantity="2.1", time=T2),
        row("DELETE", "b1", side="1", price="99", quantity="0", time=T2),
        row("ADD", "b2", side="1", price="98", quantity="0.7", time=T2),
        row("SET", quantity="0", time=T2),
    ], compressed=compressed, delimiter=delimiter)
    with prepare_csv(path, instrument, symbol=SYMBOL) as prepared:
        assert prepared.snapshot == (
            SourceOrder("b1", Side.BUY, 99, 10), SourceOrder("a1", Side.SELL, 101, 20),
        )
        assert prepared.source_symbol == SYMBOL
        assert prepared.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert prepared.snapshot_digest == source_digest(prepared.snapshot)
        assert prepared.event_count == 6
        assert prepared.start_ns % 1_000_000_000 == 123456789
        assert prepared.end_ns - prepared.start_ns == 2000
        prepared.assert_exact_runtime_precision()
        events = list(prepared.events())
        assert events == list(prepared.events())
        assert [event.index for event in events] == [3, 4, 5, 6, 7, 8]
        assert [event.source_remaining_lots for event in events] == [15, 8, 21, 0, 7, 0]
        assert [event.historical_matched_lots for event in events] == [5, 0, 5, 0, 0, 5]
        assert [event.target_mid_twice_ticks for event in events] == [200, 200, 200, None, 199, None]
        assert events[0].coinapi_time_ns == events[0].time_ns
        assert events[0].expected_digest == source_digest([
            SourceOrder("b1", Side.BUY, 99, 10), SourceOrder("a1", Side.SELL, 101, 15),
        ])
        assert events[-1].expected_digest == source_digest([SourceOrder("b2", Side.BUY, 98, 7)])
        temporary_path = prepared._event_path
    assert not temporary_path.exists()
    with pytest.raises(RuntimeError, match="closed"):
        list(prepared.events())


def test_digest_has_independent_canonical_representation_and_ignores_input_fifo():
    first = SourceOrder("b", Side.BUY, 98, 2)
    second = SourceOrder("a", Side.BUY, 98, 5)
    expected = hashlib.sha256(b'["a","BUY",98,5]\n["b","BUY",98,2]\n').hexdigest()
    assert source_digest([first, second]) == expected
    assert source_digest({"b": first, "a": second}) == expected
    with pytest.raises(ValueError, match="unique"):
        source_digest([first, first])
    with pytest.raises(ValueError, match="positive"):
        source_digest([SourceOrder("a", Side.BUY, 98, 0)])


@pytest.mark.parametrize("bad_row,match", [
    (row("MATCH", ""), "L3 order_id"),
    (row("MATCH", side="BUY"), "is_buy"),
    (row("UPDATE"), "Unsupported update_type"),
    (row("5"), "Unsupported update_type"),
    (row("MATCH", quantity="NaN"), "finite"),
    (row("MATCH", quantity="-1"), "greater than zero"),
    (row("MATCH", quantity="0"), "greater than zero"),
    (row("SUB", quantity="2.1"), "underflow"),
    (row("MATCH", quantity="2.1"), "underflow"),
    (row("MATCH", quantity="0.15"), "exact multiple"),
    (row("MATCH", price="101.5"), "exact multiple"),
    (row("MATCH", "unknown"), "Unknown"),
    (row("SET", side="1"), "changed side/price"),
    (row("SET", price="102"), "changed side/price"),
    (row("ADD", "a1", quantity="1"), "duplicate live"),
    (row("ADD", "new", side="1", price="102", quantity="1"), "locked or crossed"),
    (row("MATCH", time="2026-10-20T12:00:00.123456788Z"), "Chronological regression"),
    (row("MATCH", time="2026-10-20T12:00:00.1234567890Z"), "nanoseconds"),
])
def test_preflight_rejects_unqualified_l3_inputs_with_row_identity(tmp_path, instrument, bad_row, match):
    path = write_csv(tmp_path, snapshot() + [bad_row])
    with pytest.raises(SourceFormatError, match=f"row 3: .*{match}"):
        prepare_csv(path, instrument, symbol=SYMBOL)


@pytest.mark.parametrize("operation", ["DELETE", "SET", "MATCH", "SUB"])
def test_exact_boundary_removal_and_reused_ids_rejected(tmp_path, instrument, operation):
    quantity = "2" if operation in {"MATCH", "SUB"} else "0"
    rows = snapshot() + [row(operation, quantity=quantity)]
    path = write_csv(tmp_path, rows)
    with prepare_csv(path, instrument, symbol=SYMBOL) as prepared:
        event = next(prepared.events())
        assert event.source_remaining_lots == 0
        assert event.target_mid_twice_ticks is None
        assert event.expected_digest == source_digest([SourceOrder("b1", Side.BUY, 99, 10)])
    path = write_csv(tmp_path, rows + [row("ADD", quantity="1", time=T2)])
    with pytest.raises(SourceFormatError, match="row 4: reused order_id"):
        prepare_csv(path, instrument, symbol=SYMBOL)


def test_numeric_operations_match_documented_mapping(tmp_path, instrument):
    path = write_csv(tmp_path, snapshot() + [
        row("0", "new", price="102", quantity="1"),
        row("1", "new", price="102", quantity="0.2"),
        row("2", "new", price="102", quantity="0.9"),
        row("4", "new", price="102", quantity="0.3"),
        row("3", "new", price="102", quantity="0"),
    ])
    with prepare_csv(path, instrument, symbol=SYMBOL) as prepared:
        assert [event.operation for event in prepared.events()] == ["ADD", "SUB", "SET", "MATCH", "DELETE"]


@pytest.mark.parametrize("rows,match", [
    ([], "completeness"),
    ([row("ADD")], "File must start"),
    (snapshot(), "following increment"),
    (snapshot() + [row("MATCH"), row("SNAPSHOT", time=T2)], "Later SNAPSHOT"),
    ([snapshot()[0], row("SNAPSHOT", quantity="2", time=T1), row("MATCH")], "identical exchange"),
    ([snapshot()[0], row("SNAPSHOT", quantity="2", time=T0, received=T1), row("MATCH")], "identical exchange"),
    ([snapshot()[0], snapshot()[0], row("MATCH")], "duplicate live"),
])
def test_snapshot_framing_is_explicit_and_not_guessed(tmp_path, instrument, rows, match):
    path = write_csv(tmp_path, rows)
    with pytest.raises(SourceFormatError, match=match):
        prepare_csv(path, instrument, symbol=SYMBOL)


def test_snapshot_cross_rejected_only_after_whole_snapshot_group(tmp_path, instrument):
    rows = snapshot()
    rows[1][4] = "99"
    path = write_csv(tmp_path, rows + [row("DELETE", quantity="0")])
    with pytest.raises(SourceFormatError, match="locked or crossed"):
        prepare_csv(path, instrument, symbol=SYMBOL)


def test_timestamp_nanoseconds_and_submicro_runtime_gate(tmp_path, instrument):
    path = write_csv(tmp_path, snapshot() + [row("MATCH", time="2026-10-20T12:00:00.123456790Z")])
    with prepare_csv(path, instrument, symbol=SYMBOL) as prepared:
        assert next(prepared.events()).time_ns - prepared.start_ns == 1
        with pytest.raises(SourceFormatError, match="row 3: .*submicrosecond"):
            prepared.assert_exact_runtime_precision()


@pytest.mark.parametrize("explicit_date,partition", [(date(2026, 10, 20), None), ("2026-10-20", None), (None, "D-20261020"), (None, "D-2026102012")])
def test_time_only_requires_trusted_calendar_date(tmp_path, instrument, explicit_date, partition):
    rows = snapshot() + [row("MATCH")]
    for record in rows:
        record[0] = record[0].split("T")[1]
        record[1] = record[1].split("T")[1]
    directory = tmp_path / partition if partition else tmp_path
    path = write_csv(directory, rows)
    with prepare_csv(path, instrument, symbol=SYMBOL, date=explicit_date) as prepared:
        epoch = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        assert prepared.start_ns == int(epoch.timestamp()) * 1_000_000_000 + 123456789
    if not partition:
        with pytest.raises(SourceFormatError, match="requires date"):
            prepare_csv(path, instrument, symbol=SYMBOL)


def test_timestamp_offset_normalization_is_integer_exact(tmp_path, instrument):
    rows = snapshot() + [row("MATCH")]
    for record in rows:
        record[0] = record[0].replace("T12:", "T13:").replace("Z", "+01:00")
    path = write_csv(tmp_path, rows)
    with prepare_csv(path, instrument, symbol=SYMBOL) as prepared:
        assert next(prepared.events()).coinapi_time_ns == next(prepared.events()).time_ns


def test_source_symbol_must_be_explicit_or_present_in_metadata(tmp_path, instrument):
    path = write_csv(tmp_path, snapshot() + [row("MATCH")])
    with pytest.raises(SourceFormatError, match="Explicit source symbol"):
        prepare_csv(path, instrument)
    named = write_csv(tmp_path, snapshot() + [row("MATCH")], filename=f"IDDI-1+SC-{SYMBOL}+S-BTC-USD.csv")
    with prepare_csv(named, instrument) as prepared:
        assert prepared.source_symbol == SYMBOL
    with pytest.raises(SourceFormatError, match="disagrees"):
        prepare_csv(named, instrument, symbol="OTHER_SOURCE")
    records = [record + [SYMBOL] for record in snapshot() + [row("MATCH")]]
    labeled = write_csv(tmp_path, records, header=HEADER + ["symbol_id"])
    with prepare_csv(labeled, instrument) as prepared:
        assert prepared.source_symbol == SYMBOL
    records[-1][-1] = "OTHER_SOURCE"
    mislabeled = write_csv(tmp_path, records, header=HEADER + ["symbol_id"])
    with pytest.raises(SourceFormatError, match="row 3: symbol_id"):
        prepare_csv(mislabeled, instrument)


def test_date_and_schema_conflicts_fail_without_guessing(tmp_path, instrument):
    path = write_csv(tmp_path / "D-20261020", snapshot() + [row("MATCH")])
    with pytest.raises(SourceFormatError, match="Conflicting"):
        prepare_csv(path, instrument, symbol=SYMBOL, date="2026-10-21")
    with pytest.raises(SourceFormatError, match="calendar date"):
        prepare_csv(path, instrument, symbol=SYMBOL, date=datetime(2026, 10, 20))
    malformed = write_csv(tmp_path, snapshot() + [row("MATCH")], header=HEADER[:-1] + ["id"])
    with pytest.raises(SourceFormatError, match="CSV header"):
        prepare_csv(malformed, instrument, symbol=SYMBOL)
    with pytest.raises(SourceFormatError, match="Only local"):
        prepare_csv("https://example.invalid/orders.csv", instrument, symbol=SYMBOL)


def test_prepared_events_are_individual_records_not_stored_book_snapshots(tmp_path, instrument):
    rows = snapshot() + [row("SET", quantity="2.0") for _ in range(25)]
    path = write_csv(tmp_path, rows)
    with prepare_csv(path, instrument, symbol=SYMBOL) as prepared:
        payloads = [json.loads(line) for line in prepared._event_path.read_text().splitlines()]
        assert len(payloads) == 25
        assert all(isinstance(payload["expected_digest"], str) for payload in payloads)
        assert all("snapshot" not in payload and "orders" not in payload for payload in payloads)


def test_truncated_gzip_is_a_source_validation_error(tmp_path, instrument):
    path = write_csv(tmp_path, snapshot() + [row("MATCH")], compressed=True)
    path.write_bytes(path.read_bytes()[:-6])
    with pytest.raises(SourceFormatError, match="Cannot read local source"):
        prepare_csv(path, instrument, symbol=SYMBOL)


def test_prepared_close_releases_partly_consumed_event_streams(tmp_path, instrument):
    path = write_csv(tmp_path, snapshot() + [row("MATCH"), row("SET", quantity="1.0", time=T2)])
    with prepare_csv(path, instrument, symbol=SYMBOL) as prepared:
        iterator = prepared.events()
        next(iterator)
        temporary_path = prepared._event_path
    assert not temporary_path.exists()
    iterator.close()

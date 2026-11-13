"""Raw capture replay retains source order and exact integer quantities."""

import gzip

import pytest

from examples.coinapi_replay.source import SourceFormatError, open_source
from tests.exact_helpers import TEST_INSTRUMENT


HEADER = "time_exchange;time_coinapi;update_type;is_buy;entry_px;entry_sx;order_id\n"
SEED = "00:00:00.0000001;00:00:00.0000002;SNAPSHOT;1;99;10;b\n00:00:00.0000001;00:00:00.0000002;SNAPSHOT;0;101;10;a\n"


def write_source(tmp_path, rows, *, compressed=False):
    path = tmp_path / (
        "capture_D-2026071000_SC-COINBASE_SPOT_TEST_S-TEST.csv"
        + (".gz" if compressed else "")
    )
    if compressed:
        with gzip.open(path, "wt") as stream:
            stream.write(HEADER + SEED + rows)
    else:
        path.write_text(HEADER + SEED + rows)
    return path


@pytest.mark.parametrize("compressed", [False, True])
def test_raw_lifetimes_matches_equal_times_and_nanoseconds(tmp_path, compressed):
    path = write_source(
        tmp_path,
        "00:00:00.0000003;00:00:00.0000004;4;0;101;3;a\n00:00:00.0000003;00:00:00.0000004;3;0;101;0;a\n00:00:00.0000003;00:00:00.0000004;0;0;102;5;a\n",
        compressed=compressed,
    )
    with open_source(path, TEST_INSTRUMENT) as source:
        assert len(source.snapshot) == 2
        events = list(source.events())
        assert [e.index for e in events] == [3, 4, 5]
        assert events[0].time_ns - source.start_ns == 200
        assert events[0].source_remaining_lots == 7
        assert events[0].historical_matched_lots == 3
        assert events[0].source_id == events[1].source_id
        assert events[2].source_id != events[1].source_id
        assert events[1].target_mid_twice_ticks is None
        assert events[2].target_mid_twice_ticks == 201


@pytest.mark.parametrize(
    "row,match",
    [
        ("00:00:01;00:00:01;4;0;101;11;a\n", "underflow"),
        ("00:00:01;00:00:01;3;0;101;0;unknown\n", "Unknown"),
        ("00:00:01;00:00:01;0;1;102;1;x\n", "crossed"),
        ("00:00:01;00:00:01;0;0;101;1;a\n", "Duplicate"),
        ("00:00:01;00:00:01;SNAPSHOT;0;101;1;x\n", "snapshot"),
    ],
)
def test_bad_increment_fails_at_its_row(tmp_path, row, match):
    with pytest.raises(SourceFormatError, match=match):
        with open_source(write_source(tmp_path, row), TEST_INSTRUMENT) as source:
            list(source.events())


def test_snapshot_only_rejected_and_date_must_agree(tmp_path):
    path = write_source(tmp_path, "")
    with pytest.raises(SourceFormatError, match="increment"):
        with open_source(path, TEST_INSTRUMENT):
            pass
    with pytest.raises(SourceFormatError, match="date"):
        with open_source(path, TEST_INSTRUMENT, date="2026-07-11"):
            pass


def test_reference_depth_has_no_future_state_and_source_consumes_once(tmp_path):
    path = write_source(
        tmp_path, "00:00:01;00:00:01;2;0;101;6;a\n00:00:02;00:00:02;1;0;101;2;a\n"
    )
    with open_source(path, TEST_INSTRUMENT) as source:
        assert source.reference_depth() == (((99, 10),), ((101, 10),))
        stream = source.events()
        first = next(stream)
        assert first.source_remaining_lots == 6
        assert source.reference_depth() == (((99, 10),), ((101, 6),))
        next(stream)
        assert source.reference_depth() == (((99, 10),), ((101, 4),))
        with pytest.raises(RuntimeError, match="once"):
            list(source.events())


def test_comma_full_timestamps_and_nine_fractional_digits(tmp_path):
    path = tmp_path / "capture.csv"
    path.write_text(
        (HEADER + SEED + "00:00:00.000000301;00:00:00.000000401;4;0;101;1;a\n")
        .replace(";", ",")
        .replace("00:00:", "2026-07-10T00:00:")
    )
    with open_source(path, TEST_INSTRUMENT, source_symbol="SOURCE") as source:
        event = next(source.events())
        assert event.time_ns - source.start_ns == 201


@pytest.mark.parametrize("missing_row", [1, 3, 4])
def test_missing_optional_csv_field_is_rejected_with_original_row(
    tmp_path, missing_row
):
    path = tmp_path / "capture_D-2026071000.csv"
    rows = (
        SEED + "00:00:01;00:00:01;2;0;101;6;a\n" "00:00:02;00:00:02;1;0;101;2;a\n"
    ).splitlines()
    path.write_text(
        HEADER.rstrip("\n")
        + ";symbol_id\n"
        + "\n".join(
            row if index == missing_row else row + ";SOURCE"
            for index, row in enumerate(rows, start=1)
        )
        + "\n"
    )
    with pytest.raises(
        SourceFormatError, match=f"row {missing_row}: Incorrect field count"
    ):
        with open_source(path, TEST_INSTRUMENT) as source:
            list(source.events())

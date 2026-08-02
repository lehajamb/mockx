"""GLIMPSE 5.0 snapshot encoding tests."""

from datetime import datetime, timezone
from decimal import Decimal
import struct

import pytest

from src.mockx.glimpse_encoder import (
    GlimpseEncoder,
    GlimpseOrderSnapshot,
    GlimpseSnapshot,
)
from src.mockx.instruments import Instrument, InstrumentCatalog
from src.mockx.itch_encoder import ITCHEncoder
from src.mockx.models import Side


NOW = datetime(2025, 1, 15, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def encoder() -> GlimpseEncoder:
    instruments = InstrumentCatalog(
        {
            "TEST": Instrument("TEST", Decimal("0.01"), Decimal("1"), 2, 0),
            "AAPL": Instrument("AAPL", Decimal("0.01"), Decimal("1"), 2, 0),
        }
    )
    return GlimpseEncoder(
        ITCHEncoder({"TEST": 1, "AAPL": 2}, instruments),
        market_category=b" ",
        financial_status=b" ",
        round_lot_size=1,
    )


def test_end_snapshot_exact_layout_and_bounds(encoder):
    message = encoder.end_snapshot(42)
    assert len(message) == 21
    assert message == b"G" + b"                  42"

    assert encoder.end_snapshot(0xFFFFFFFFFFFFFFFF)[1:] == b"18446744073709551615"
    with pytest.raises(ValueError, match="between 1"):
        encoder.end_snapshot(0)
    with pytest.raises(ValueError, match="between 1"):
        encoder.end_snapshot(1 << 64)


def test_spin_orders_directory_actions_adds_then_marker(encoder):
    snapshot = GlimpseSnapshot(
        run_generation=7,
        timestamp=NOW,
        itch_sequence=99,
        orders=(
            GlimpseOrderSnapshot(11, "TEST", Side.BUY, 3, 100),
            GlimpseOrderSnapshot(12, "AAPL", Side.SELL, 2, 101),
        ),
    )
    messages = encoder.encode(snapshot)

    assert [message[:1] for message in messages] == [b"R", b"R", b"H", b"H", b"A", b"A", b"G"]
    assert [len(message) for message in messages] == [39, 39, 25, 25, 36, 36, 21]
    assert [struct.unpack(">H", message[3:5])[0] for message in messages[:-1]] == [1, 2, 3, 4, 5, 6]
    assert struct.unpack(">Q", messages[4][11:19])[0] == 11
    assert struct.unpack(">I", messages[4][20:24])[0] == 3
    assert messages[-1] == b"G" + b"                  99"


def test_spin_tracking_number_wraps_without_changing_order(encoder):
    orders = tuple(
        GlimpseOrderSnapshot(index + 1, "TEST", Side.BUY, 1, 100)
        for index in range(65_532)
    )
    messages = encoder.encode(GlimpseSnapshot(1, NOW, 1, orders))
    assert struct.unpack(">H", messages[-2][3:5])[0] == 0
    assert messages[-1][:1] == b"G"


def test_directory_and_action_messages_follow_locate_code_order():
    instruments = InstrumentCatalog(
        {
            "TEST": Instrument("TEST", Decimal("0.01"), Decimal("1"), 2, 0),
            "AAPL": Instrument("AAPL", Decimal("0.01"), Decimal("1"), 2, 0),
        }
    )
    encoder = GlimpseEncoder(
        ITCHEncoder({"TEST": 2, "AAPL": 1}, instruments),
        market_category=b" ",
        financial_status=b" ",
        round_lot_size=1,
    )
    messages = encoder.encode(GlimpseSnapshot(1, NOW, 1, ()))
    assert messages[0][11:19] == b"AAPL    "
    assert messages[1][11:19] == b"TEST    "
    assert struct.unpack(">H", messages[2][1:3])[0] == 1
    assert struct.unpack(">H", messages[3][1:3])[0] == 2

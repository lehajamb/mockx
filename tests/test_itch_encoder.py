"""Unit tests for ITCH 5.0 binary message encoder."""

from datetime import datetime, timezone
from decimal import Decimal
import struct

import pytest

from src.mockx.itch_encoder import (
    ITCHEncoder,
    MSG_SYSTEM_EVENT,
    MSG_STOCK_DIRECTORY,
    MSG_STOCK_TRADING_ACTION,
    MSG_ADD_ORDER_NO_MPID,
    MSG_ADD_ORDER_MPID,
    MSG_ORDER_EXECUTED,
    MSG_ORDER_EXECUTED_WITH_PRICE,
    MSG_ORDER_CANCEL,
    MSG_ORDER_DELETE,
    MSG_ORDER_REPLACE,
    MSG_TRADE_NON_CROSS,
    MSG_CROSS_TRADE,
    MSG_BROKEN_TRADE,
    EVENT_START_OF_MESSAGES,
    EVENT_START_OF_SYSTEM_HOURS,
    EVENT_END_OF_MESSAGES,
    TRADING_STATE_TRADING,
    TRADING_STATE_HALTED,
    CROSS_TYPE_OPENING,
    _pad_ascii,
    _price_to_itch,
    _shares_from_lots,
    _nanos_since_midnight,
)
from src.mockx.instruments import Instrument, InstrumentCatalog
from src.mockx.models import Side


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def instruments() -> InstrumentCatalog:
    return InstrumentCatalog(
        {
            "TEST": Instrument(
                symbol="TEST",
                tick_size=Decimal("0.01"),
                lot_size=Decimal("1"),
                price_display_precision=2,
                quantity_display_precision=0,
            ),
            "AAPL": Instrument(
                symbol="AAPL",
                tick_size=Decimal("0.01"),
                lot_size=Decimal("1"),
                price_display_precision=2,
                quantity_display_precision=0,
            ),
        }
    )


@pytest.fixture
def locate_codes() -> dict[str, int]:
    return {"TEST": 1, "AAPL": 2}


@pytest.fixture
def encoder(locate_codes, instruments) -> ITCHEncoder:
    return ITCHEncoder(locate_codes, instruments)


@pytest.fixture
def sample_timestamp() -> datetime:
    return datetime(2025, 1, 15, 9, 30, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestPadASCII:
    def test_short_string(self):
        result = _pad_ascii("ABC", 8)
        assert len(result) == 8
        assert result == b"ABC     "

    def test_exact_length(self):
        result = _pad_ascii("ABCDEFGH", 8)
        assert result == b"ABCDEFGH"

    def test_too_long(self):
        with pytest.raises(ValueError, match="longer than"):
            _pad_ascii("LONGSYMBOL", 8)

    def test_non_ascii(self):
        with pytest.raises(ValueError, match="only ASCII"):
            _pad_ascii("CAFÉ", 8)


class TestPriceToITCH:
    def test_tick_size_001(self):
        # tick_size=0.01 means scaling factor = 0.01 * 10000 = 100
        assert _price_to_itch(10050, Decimal("0.01")) == 1005000

    def test_tick_size_0001(self):
        # tick_size=0.0001 means scaling factor = 0.0001 * 10000 = 1
        assert _price_to_itch(1234567, Decimal("0.0001")) == 1234567

    def test_tick_size_1(self):
        assert _price_to_itch(42, Decimal("1")) == 420000

    def test_nonrepresentable_tick_and_overflow(self):
        with pytest.raises(ValueError, match="not exactly representable"):
            _price_to_itch(1, Decimal("0.00001"))
        with pytest.raises(ValueError, match="unsigned 32-bit"):
            _price_to_itch(0x100000000, Decimal("0.0001"))


class TestSharesFromLots:
    def test_lot_size_1(self):
        assert _shares_from_lots(100) == 100

    def test_fractional_external_lot_size_does_not_change_wire_shares(self):
        assert _shares_from_lots(5) == 5

    def test_overflow_rejected(self):
        with pytest.raises(ValueError, match="unsigned 32-bit"):
            _shares_from_lots(1 << 32)


class TestNanosSinceMidnight:
    def test_midnight(self):
        ts = datetime(2025, 1, 15, 0, 0, 0, 0, tzinfo=timezone.utc)
        assert _nanos_since_midnight(ts) == 0

    def test_0930(self):
        ts = datetime(2025, 1, 15, 9, 30, 0, 0, tzinfo=timezone.utc)
        expected_ns = 9 * 3600 * 1_000_000_000 + 30 * 60 * 1_000_000_000
        assert _nanos_since_midnight(ts) == expected_ns

    def test_requires_timezone(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            _nanos_since_midnight(datetime(2025, 1, 15, 9, 30))


# ---------------------------------------------------------------------------
# System Event Message tests
# ---------------------------------------------------------------------------


class TestSystemEvent:
    def test_start_of_messages(self, encoder, sample_timestamp):
        msg = encoder.system_event(EVENT_START_OF_MESSAGES, sample_timestamp)
        assert len(msg) == 12
        assert msg[0:1] == MSG_SYSTEM_EVENT
        # Stock Locate = 0
        locate = struct.unpack(">H", msg[1:3])[0]
        assert locate == 0

    def test_event_code_at_offset_11(self, encoder, sample_timestamp):
        msg = encoder.system_event(EVENT_START_OF_SYSTEM_HOURS, sample_timestamp)
        assert msg[11:12] == EVENT_START_OF_SYSTEM_HOURS

    def test_end_of_messages(self, encoder, sample_timestamp):
        msg = encoder.system_event(EVENT_END_OF_MESSAGES, sample_timestamp)
        assert msg[11:12] == EVENT_END_OF_MESSAGES


# ---------------------------------------------------------------------------
# Stock Directory Message tests
# ---------------------------------------------------------------------------


class TestStockDirectory:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.stock_directory("TEST", sample_timestamp)
        assert msg[0:1] == MSG_STOCK_DIRECTORY
        assert len(msg) == 39

    def test_stock_locate(self, encoder, sample_timestamp):
        msg = encoder.stock_directory("AAPL", sample_timestamp)
        locate = struct.unpack(">H", msg[1:3])[0]
        assert locate == 2  # AAPL's locate code

    def test_symbol_padded(self, encoder, sample_timestamp):
        msg = encoder.stock_directory("TEST", sample_timestamp)
        symbol_bytes = msg[11:19]
        assert symbol_bytes == b"TEST    "

    def test_default_fields(self, encoder, sample_timestamp):
        msg = encoder.stock_directory("TEST", sample_timestamp)
        # Market Category at offset 19
        assert msg[19:20] == b" "
        # Financial Status at offset 20
        assert msg[20:21] == b" "
        # Round Lot Size at offset 21 (4 bytes big-endian)
        round_lot = struct.unpack(">I", msg[21:25])[0]
        assert round_lot == 1

    def test_fixed_byte_fields_require_ascii(self, encoder, sample_timestamp):
        with pytest.raises(ValueError, match="only ASCII bytes"):
            encoder.stock_directory("TEST", sample_timestamp, market_category=b"\xff")


# ---------------------------------------------------------------------------
# Stock Trading Action tests
# ---------------------------------------------------------------------------


class TestStockTradingAction:
    def test_trading_resumed(self, encoder, sample_timestamp):
        msg = encoder.stock_trading_action(
            "TEST", TRADING_STATE_TRADING, sample_timestamp
        )
        assert len(msg) == 25
        assert msg[0:1] == MSG_STOCK_TRADING_ACTION
        assert msg[19:20] == TRADING_STATE_TRADING

    def test_halted(self, encoder, sample_timestamp):
        msg = encoder.stock_trading_action(
            "TEST", TRADING_STATE_HALTED, sample_timestamp
        )
        assert msg[19:20] == TRADING_STATE_HALTED


# ---------------------------------------------------------------------------
# Add Order Message tests
# ---------------------------------------------------------------------------


class TestAddOrderNoMPID:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.add_order_no_mpid(
            "TEST", 12345, Side.BUY, 100, 10050, sample_timestamp
        )
        assert len(msg) == 36
        assert msg[0:1] == MSG_ADD_ORDER_NO_MPID

    def test_buy_order(self, encoder, sample_timestamp):
        msg = encoder.add_order_no_mpid(
            "TEST", 1, Side.BUY, 100, 10050, sample_timestamp
        )
        assert msg[19:20] == b"B"

    def test_sell_order(self, encoder, sample_timestamp):
        msg = encoder.add_order_no_mpid(
            "TEST", 1, Side.SELL, 100, 10050, sample_timestamp
        )
        assert msg[19:20] == b"S"

    def test_price_encoding(self, encoder, sample_timestamp):
        # price_ticks=10050, tick_size=0.01 → itch_price = 10050 * 100 = 1,005,000
        msg = encoder.add_order_no_mpid(
            "TEST", 1, Side.BUY, 100, 10050, sample_timestamp
        )
        price = struct.unpack(">I", msg[32:36])[0]
        assert price == 1_005_000

    def test_shares_encoding(self, encoder, sample_timestamp):
        # 100 lots × 1 share/lot = 100 shares
        msg = encoder.add_order_no_mpid(
            "TEST", 1, Side.BUY, 100, 10050, sample_timestamp
        )
        shares = struct.unpack(">I", msg[20:24])[0]
        assert shares == 100

    def test_order_ref_number(self, encoder, sample_timestamp):
        msg = encoder.add_order_no_mpid(
            "TEST", 999888, Side.BUY, 100, 10050, sample_timestamp
        )
        ref = struct.unpack(">Q", msg[11:19])[0]
        assert ref == 999888


class TestAddOrderMPID:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.add_order_mpid(
            "TEST", 1, Side.BUY, 100, 10050, sample_timestamp, "ABCD"
        )
        assert len(msg) == 40
        assert msg[0:1] == MSG_ADD_ORDER_MPID

    def test_attribution(self, encoder, sample_timestamp):
        msg = encoder.add_order_mpid(
            "TEST", 1, Side.BUY, 100, 10050, sample_timestamp, "ABCD"
        )
        attr = msg[36:40]
        assert attr == b"ABCD"

    def test_attribution_is_not_silently_truncated(self, encoder, sample_timestamp):
        with pytest.raises(ValueError, match="longer than"):
            encoder.add_order_mpid(
                "TEST", 1, Side.BUY, 100, 10050, sample_timestamp, "LONGNAME"
            )


# ---------------------------------------------------------------------------
# Modify Order Message tests
# ---------------------------------------------------------------------------


class TestOrderExecuted:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.order_executed("TEST", 42, 50, 1001, sample_timestamp)
        assert len(msg) == 31
        assert msg[0:1] == MSG_ORDER_EXECUTED

    def test_match_number(self, encoder, sample_timestamp):
        msg = encoder.order_executed("TEST", 42, 50, 1001, sample_timestamp)
        match = struct.unpack(">Q", msg[23:31])[0]
        assert match == 1001

    def test_executed_shares(self, encoder, sample_timestamp):
        # 50 lots × 1 share/lot = 50 shares
        msg = encoder.order_executed("TEST", 42, 50, 1001, sample_timestamp)
        shares = struct.unpack(">I", msg[19:23])[0]
        assert shares == 50


class TestOrderExecutedWithPrice:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.order_executed_with_price(
            "TEST", 42, 50, 1001, 10050, True, sample_timestamp
        )
        assert len(msg) == 36
        assert msg[0:1] == MSG_ORDER_EXECUTED_WITH_PRICE

    def test_printable(self, encoder, sample_timestamp):
        msg = encoder.order_executed_with_price(
            "TEST", 42, 50, 1001, 10050, True, sample_timestamp
        )
        assert msg[31:32] == b"Y"

    def test_non_printable(self, encoder, sample_timestamp):
        msg = encoder.order_executed_with_price(
            "TEST", 42, 50, 1001, 10050, False, sample_timestamp
        )
        assert msg[31:32] == b"N"


class TestOrderCancel:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.order_cancel("TEST", 42, 25, sample_timestamp)
        assert len(msg) == 23
        assert msg[0:1] == MSG_ORDER_CANCEL

    def test_cancelled_shares(self, encoder, sample_timestamp):
        msg = encoder.order_cancel("TEST", 42, 25, sample_timestamp)
        shares = struct.unpack(">I", msg[19:23])[0]
        assert shares == 25


class TestOrderDelete:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.order_delete("TEST", 42, sample_timestamp)
        assert len(msg) == 19
        assert msg[0:1] == MSG_ORDER_DELETE

    def test_order_ref_number(self, encoder, sample_timestamp):
        msg = encoder.order_delete("TEST", 123456, sample_timestamp)
        ref = struct.unpack(">Q", msg[11:19])[0]
        assert ref == 123456


class TestOrderReplace:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.order_replace("TEST", 100, 200, 50, 10500, sample_timestamp)
        assert len(msg) == 35
        assert msg[0:1] == MSG_ORDER_REPLACE

    def test_old_and_new_refs(self, encoder, sample_timestamp):
        msg = encoder.order_replace("TEST", 100, 200, 50, 10500, sample_timestamp)
        old_ref = struct.unpack(">Q", msg[11:19])[0]
        new_ref = struct.unpack(">Q", msg[19:27])[0]
        assert old_ref == 100
        assert new_ref == 200


# ---------------------------------------------------------------------------
# Trade Message tests
# ---------------------------------------------------------------------------


class TestTradeNonCross:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.trade_non_cross("TEST", 100, 10050, 5000, sample_timestamp)
        assert len(msg) == 44
        assert msg[0:1] == MSG_TRADE_NON_CROSS

    def test_order_ref_is_zero(self, encoder, sample_timestamp):
        msg = encoder.trade_non_cross("TEST", 100, 10050, 5000, sample_timestamp)
        ref = struct.unpack(">Q", msg[11:19])[0]
        assert ref == 0

    def test_buy_sell_is_always_b(self, encoder, sample_timestamp):
        msg = encoder.trade_non_cross("TEST", 100, 10050, 5000, sample_timestamp)
        assert msg[19:20] == b"B"


class TestCrossTrade:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.cross_trade(
            "TEST", 1000, 10050, 7000, CROSS_TYPE_OPENING, sample_timestamp
        )
        assert len(msg) == 40
        assert msg[0:1] == MSG_CROSS_TRADE

    def test_cross_type(self, encoder, sample_timestamp):
        msg = encoder.cross_trade(
            "TEST", 1000, 10050, 7000, CROSS_TYPE_OPENING, sample_timestamp
        )
        assert msg[39:40] == CROSS_TYPE_OPENING


class TestBrokenTrade:
    def test_message_type(self, encoder, sample_timestamp):
        msg = encoder.broken_trade("TEST", 7000, sample_timestamp)
        assert len(msg) == 19
        assert msg[0:1] == MSG_BROKEN_TRADE


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestEncoderErrors:
    def test_unknown_symbol(self, encoder, sample_timestamp):
        with pytest.raises(KeyError, match="No ITCH locate code"):
            encoder.add_order_no_mpid(
                "UNKNOWN", 1, Side.BUY, 100, 10050, sample_timestamp
            )

    def test_no_timezone(self, encoder):
        with pytest.raises(ValueError, match="timezone-aware"):
            encoder.add_order_no_mpid(
                "TEST", 1, Side.BUY, 100, 10050, datetime(2025, 1, 15, 9, 30)
            )

    def test_nonrepresentable_price_tick_is_rejected(self, sample_timestamp):
        instruments = InstrumentCatalog(
            {"TEST": Instrument("TEST", Decimal("0.00001"), Decimal("0.001"), 5, 3)}
        )
        encoder = ITCHEncoder({"TEST": 1}, instruments)
        with pytest.raises(ValueError, match="exactly representable"):
            encoder.add_order_no_mpid("TEST", 1, Side.BUY, 1, 1, sample_timestamp)

    def test_fractional_lot_size_still_encodes_one_share(self, sample_timestamp):
        instruments = InstrumentCatalog(
            {"TEST": Instrument("TEST", Decimal("0.01"), Decimal("0.001"), 2, 3)}
        )
        encoder = ITCHEncoder({"TEST": 1}, instruments)
        message = encoder.add_order_no_mpid(
            "TEST", 1, Side.BUY, 1, 100, sample_timestamp
        )
        assert struct.unpack(">I", message[20:24])[0] == 1

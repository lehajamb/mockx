"""Optional bounded midpoint controller implemented as an ordinary FIX actor."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Sequence

from .tracker import FixIntent


@dataclass(frozen=True, slots=True)
class DepthLevel:
    price_ticks: int
    quantity_lots: int


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    enabled: bool = False
    max_order_qty_lots: int = 10_000
    max_total_qty_lots: int = 1_000_000
    max_depth_levels: int = 10
    reaction_interval_ns: int = 100_000_000
    max_book_age_ns: int = 5_000_000_000
    max_target_age_ns: int = 5_000_000_000
    max_replay_lag_ns: int = 1_000_000_000
    id_prefix: str = "CTRL"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not self.id_prefix:
            raise ValueError("Controller requires a boolean enabled flag and ID prefix")
        for name in (
            "max_order_qty_lots", "max_total_qty_lots", "max_depth_levels",
            "reaction_interval_ns", "max_book_age_ns", "max_target_age_ns",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (isinstance(self.max_replay_lag_ns, bool)
                or not isinstance(self.max_replay_lag_ns, int)
                or self.max_replay_lag_ns < 0):
            raise ValueError("max_replay_lag_ns must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class ControllerDecision:
    intent: FixIntent
    error_before: int
    error_after: int
    mid_before_twice_ticks: int
    mid_after_twice_ticks: int
    target_mid_twice_ticks: int
    consumed_levels: int


class MidController:
    """Select the smallest improving full-level sweep from observed depth.

    All timestamps are caller-supplied monotonic nanoseconds. The actor trades
    total displayed liquidity, including strategy orders, through normal market
    orders. Predictions are conditional on the observed book still being valid;
    actual fills and the next feed snapshot determine subsequent decisions.
    """

    def __init__(self, symbol: str, config: ControllerConfig):
        if not symbol:
            raise ValueError("Controller symbol is required")
        self.symbol = symbol
        self.config = config
        self._lock = threading.RLock()
        self._bids: tuple[DepthLevel, ...] = ()
        self._asks: tuple[DepthLevel, ...] = ()
        self._book_ns: int | None = None
        self._book_revision = 0
        self._last_action_book_revision: int | None = None
        self._input_revision = 0
        self._evaluated_revision = -1
        self._reaction_due_ns: int | None = None
        self._target: int | None = None
        self._target_ns: int | None = None
        self._last_action_ns: int | None = None
        self._pending: FixIntent | None = None
        self._stopped = False
        self._counter = 0
        self._quantity_used = 0

    @property
    def pending_intent(self) -> FixIntent | None:
        with self._lock:
            return self._pending

    @property
    def inflight_cl_ord_id(self) -> str | None:
        with self._lock:
            return self._pending.cl_ord_id if self._pending else None

    @property
    def in_flight(self) -> bool:
        return self.pending_intent is not None

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    @staticmethod
    def _validate_levels(levels: Sequence[DepthLevel], *, bids: bool) -> tuple[DepthLevel, ...]:
        result = tuple(levels)
        previous: int | None = None
        for level in result:
            if (isinstance(level.price_ticks, bool) or not isinstance(level.price_ticks, int)
                    or isinstance(level.quantity_lots, bool)
                    or not isinstance(level.quantity_lots, int)
                    or level.price_ticks <= 0 or level.quantity_lots <= 0):
                raise ValueError("Controller depth requires positive integer ticks and lots")
            if previous is not None and (
                (bids and level.price_ticks >= previous)
                or (not bids and level.price_ticks <= previous)
            ):
                raise ValueError("Controller depth must be sorted with unique price levels")
            previous = level.price_ticks
        return result

    def observe_book(self, bids: Sequence[DepthLevel], asks: Sequence[DepthLevel],
                     observed_ns: int, *, received_ns: int | None = None) -> None:
        """Retain the book's age while reacting only after it was received."""
        normalized_bids = self._validate_levels(bids, bids=True)
        normalized_asks = self._validate_levels(asks, bids=False)
        if normalized_bids and normalized_asks and (
            normalized_bids[0].price_ticks >= normalized_asks[0].price_ticks
        ):
            raise ValueError("Controller book must be uncrossed")
        with self._lock:
            if self._book_ns is not None and observed_ns < self._book_ns:
                return
            self._bids, self._asks = normalized_bids, normalized_asks
            self._book_ns = observed_ns
            self._book_revision += 1
            self._input_revision += 1
            if not self._bids or not self._asks:
                self._reaction_due_ns = None
            self._arm_reaction(observed_ns if received_ns is None else received_ns)

    def observe_target(self, target_mid_twice_ticks: int | None, observed_ns: int) -> None:
        if target_mid_twice_ticks is not None and (
            isinstance(target_mid_twice_ticks, bool)
            or not isinstance(target_mid_twice_ticks, int)
            or target_mid_twice_ticks <= 0
        ):
            raise ValueError("Target midpoint must be positive integer twice-ticks")
        with self._lock:
            if self._target_ns is not None and observed_ns < self._target_ns:
                return
            self._target = target_mid_twice_ticks
            self._target_ns = observed_ns
            self._input_revision += 1
            if self._target is None:
                self._reaction_due_ns = None
            self._arm_reaction(observed_ns)

    def _arm_reaction(self, now_ns: int) -> None:
        """Start one reaction timer; coalesced observations retain its deadline."""
        if (self.config.enabled and not self._stopped and self._pending is None
                and self._bids and self._asks and self._target is not None
                and self._reaction_due_ns is None
                and self._input_revision != self._evaluated_revision
                and self._last_action_book_revision != self._book_revision):
            self._reaction_due_ns = now_ns + self.config.reaction_interval_ns

    def decide(self, now_ns: int, replay_lag_ns: int = 0) -> ControllerDecision | None:
        with self._lock:
            config = self.config
            if (not config.enabled or self._stopped or self._pending is not None
                    or not self._bids or not self._asks or self._target is None
                    or self._book_ns is None or self._target_ns is None):
                return None
            if self._last_action_book_revision == self._book_revision:
                return None
            self._arm_reaction(now_ns)
            if self._reaction_due_ns is None or now_ns < self._reaction_due_ns:
                return None
            if (not 0 <= now_ns - self._book_ns <= config.max_book_age_ns
                    or not 0 <= now_ns - self._target_ns <= config.max_target_age_ns
                    or not 0 <= replay_lag_ns <= config.max_replay_lag_ns):
                return None
            if (self._last_action_ns is not None
                    and now_ns - self._last_action_ns < config.reaction_interval_ns):
                return None
            self._reaction_due_ns = None
            self._evaluated_revision = self._input_revision
            budget = min(config.max_order_qty_lots,
                         config.max_total_qty_lots - self._quantity_used)
            if budget <= 0:
                return None
            before = self._bids[0].price_ticks + self._asks[0].price_ticks
            error_before = abs(self._target - before)
            if not error_before:
                return None
            buy = self._target > before
            levels = self._asks if buy else self._bids
            unchanged_best = self._bids[0].price_ticks if buy else self._asks[0].price_ticks
            candidates: list[tuple[int, int, int, int]] = []
            quantity = 0
            # Always leave the final observed level on the consumed side.
            for count in range(1, min(config.max_depth_levels, len(levels) - 1) + 1):
                quantity += levels[count - 1].quantity_lots
                if quantity > budget:
                    break
                after = unchanged_best + levels[count].price_ticks
                error_after = abs(self._target - after)
                if error_after < error_before:
                    candidates.append((error_after, quantity, count, after))
            if not candidates:
                return None
            error_after, quantity, count, after = min(candidates)
            self._counter += 1
            intent = FixIntent(
                "NEW", "controller", f"{config.id_prefix}-{self._counter:012d}",
                None, self.symbol, "BUY" if buy else "SELL", quantity, None,
                "IOC", None, None,
            )
            self._pending = intent
            self._last_action_ns = now_ns
            self._last_action_book_revision = self._book_revision
            self._quantity_used += quantity
            return ControllerDecision(intent, error_before, error_after, before,
                                      after, self._target, count)

    def complete(self, cl_ord_id: str | None = None, *, now_ns: int | None = None) -> bool:
        with self._lock:
            if self._pending is None:
                return False
            if cl_ord_id is not None and self._pending.cl_ord_id != cl_ord_id:
                return False
            self._pending = None
            if now_ns is not None:
                self._arm_reaction(now_ns)
            return True

    def stop(self) -> None:
        """Disable further submission; normal FIX delivery still settles any order."""
        with self._lock:
            self._stopped = True
            self._reaction_due_ns = None


"""Bounded midpoint correction through an ordinary observed-feed FIX actor."""

from __future__ import annotations

from dataclasses import dataclass

from .tracker import FixIntent


@dataclass(frozen=True, slots=True)
class DepthLevel:
    price_ticks: int
    quantity_lots: int


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    enabled: bool = True
    max_order_qty_lots: int = 10_000_000
    max_total_qty_lots: int = 10_000_000_000
    max_depth_levels: int = 10
    reaction_interval_ns: int = 100_000_000
    deadband_twice_ticks: int = 2
    max_book_age_ns: int = 1_000_000_000
    max_target_age_ns: int = 1_000_000_000
    max_replay_lag_ns: int = 250_000_000
    id_prefix: str = "CTRL"

    def __post_init__(self):
        if not isinstance(self.enabled, bool) or not self.id_prefix:
            raise ValueError("Controller requires a boolean enabled flag and ID prefix")
        for name in (
            "max_order_qty_lots",
            "max_total_qty_lots",
            "max_depth_levels",
            "reaction_interval_ns",
            "max_book_age_ns",
            "max_target_age_ns",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("deadband_twice_ticks", "max_replay_lag_ns"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")


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
    """Choose the closest improving sweep, preferring less size on equal error.

    All calls belong to the runner loop. Targets become available only when the
    corresponding source event is released. A new action requires a fresh book
    revision after the preceding action, preventing repeated stale-book sweeps.
    """

    def __init__(self, symbol, config: ControllerConfig):
        self.symbol, self.config = symbol, config
        self._bids, self._asks = (), ()
        self._book_ns = self._target_ns = self._target = None
        self._book_revision = 0
        self._action_revision = -1
        self._due = self._last_action = None
        self._pending = None
        self._stopped = False
        self._counter = 0
        self.quantity_used = 0

    @property
    def pending_intent(self):
        return self._pending

    @property
    def in_flight(self):
        return self._pending is not None

    @property
    def inflight_cl_ord_id(self):
        return self._pending.cl_ord_id if self._pending else None

    @staticmethod
    def _validate(levels, *, bids):
        result = tuple(levels)
        previous = None
        for level in result:
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in (level.price_ticks, level.quantity_lots)
            ):
                raise ValueError(
                    "Depth requires positive integer prices and quantities"
                )
            if previous is not None and (
                (bids and level.price_ticks >= previous)
                or (not bids and level.price_ticks <= previous)
            ):
                raise ValueError("Depth must be strictly price sorted")
            previous = level.price_ticks
        return result

    def _arm(self, now):
        if (
            self._due is None
            and self._bids
            and self._asks
            and self._target is not None
            and not self.in_flight
            and not self._stopped
            and self._book_revision != self._action_revision
        ):
            self._due = (
                max(now, self._last_action or now) + self.config.reaction_interval_ns
            )

    def observe_book(self, bids, asks, observed_ns, *, received_ns=None):
        bids, asks = self._validate(bids, bids=True), self._validate(asks, bids=False)
        if bids and asks and bids[0].price_ticks >= asks[0].price_ticks:
            raise ValueError("Observed book must be uncrossed")
        if self._book_ns is not None and observed_ns < self._book_ns:
            return
        self._bids, self._asks, self._book_ns = bids, asks, observed_ns
        self._book_revision += 1
        if not bids or not asks:
            self._due = None
        self._arm(observed_ns if received_ns is None else received_ns)

    def observe_target(self, target_mid_twice_ticks, observed_ns):
        if target_mid_twice_ticks is not None and (
            isinstance(target_mid_twice_ticks, bool)
            or not isinstance(target_mid_twice_ticks, int)
            or target_mid_twice_ticks <= 0
        ):
            raise ValueError("Target must be positive integer twice-ticks")
        if self._target_ns is not None and observed_ns < self._target_ns:
            return
        self._target, self._target_ns = target_mid_twice_ticks, observed_ns
        if self._target is None:
            self._due = None
        self._arm(observed_ns)

    def decide(self, now_ns, replay_lag_ns=0):
        c = self.config
        if (
            not c.enabled
            or self._stopped
            or self.in_flight
            or not self._bids
            or not self._asks
            or self._target is None
            or self._book_revision == self._action_revision
        ):
            return None
        self._arm(now_ns)
        if self._due is None or now_ns < self._due:
            return None
        if not (
            0 <= now_ns - self._book_ns <= c.max_book_age_ns
            and 0 <= now_ns - self._target_ns <= c.max_target_age_ns
            and 0 <= replay_lag_ns <= c.max_replay_lag_ns
        ):
            return None
        before = self._bids[0].price_ticks + self._asks[0].price_ticks
        error = abs(self._target - before)
        if error <= c.deadband_twice_ticks:
            return None
        buy = self._target > before
        levels = self._asks if buy else self._bids
        other = self._bids[0].price_ticks if buy else self._asks[0].price_ticks
        budget = min(c.max_order_qty_lots, c.max_total_qty_lots - self.quantity_used)
        quantity, candidates = 0, []
        for count in range(1, min(c.max_depth_levels, len(levels) - 1) + 1):
            quantity += levels[count - 1].quantity_lots
            if quantity > budget:
                break
            after = other + levels[count].price_ticks
            after_error = abs(self._target - after)
            if after_error < error:
                candidates.append((after_error, quantity, count, after))
        if not candidates:
            return None
        after_error, quantity, count, after = min(candidates)
        self._counter += 1
        intent = FixIntent(
            "NEW",
            "controller",
            f"{c.id_prefix}-{self._counter:012d}",
            None,
            self.symbol,
            "BUY" if buy else "SELL",
            quantity,
            levels[count - 1].price_ticks,
            "IOC",
            None,
            None,
        )
        self._pending, self._last_action = intent, now_ns
        self._action_revision = self._book_revision
        self._due = None
        self.quantity_used += quantity
        return ControllerDecision(
            intent, error, after_error, before, after, self._target, count
        )

    def complete(self, cl_ord_id=None, *, now_ns=None):
        if self._pending is None or (
            cl_ord_id is not None and cl_ord_id != self._pending.cl_ord_id
        ):
            return False
        self._pending = None
        if now_ns is not None:
            self._arm(now_ns)
        return True

    def stop(self):
        self._stopped, self._due = True, None

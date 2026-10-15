#!/usr/bin/env python3
"""Drive a continuously moving MockX market through two FIX sessions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path
import random
import signal
import sys
import threading
import time
from types import FrameType
from typing import Callable, Iterable, Optional

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.fix_support import (
    FixReport,
    MultiSessionFIXApplication,
    TERMINAL_STATUSES,
    fix,
    require_quickfix,
    send_cancel,
    send_limit_order,
    send_market_order,
)
from src.mockx.configuration import ConfigurationError, load_config
from src.mockx.instruments import Instrument


MAKER_ROLE = "liquidity_bot"
TAKER_ROLE = "taker_bot"


@dataclass(frozen=True, order=True)
class PlannedLevel:
    side: str
    price_ticks: int


@dataclass(frozen=True)
class LiveQuote:
    cl_ord_id: str
    level: PlannedLevel
    quantity_lots: int
    leaves_qty_lots: int
    status: str = "PENDING"


def build_ladder(
    midpoint_ticks: int,
    *,
    levels: int,
    inside_spread_ticks: int = 5,
    level_spacing_ticks: int = 5,
) -> tuple[PlannedLevel, ...]:
    """Return a symmetric, strictly non-crossing maker ladder."""

    if levels <= 0:
        raise ValueError("levels must be positive")
    if inside_spread_ticks <= 0 or level_spacing_ticks <= 0:
        raise ValueError("spread and spacing ticks must be positive")
    bids = tuple(
        PlannedLevel(
            "BUY",
            midpoint_ticks
            - inside_spread_ticks
            - index * level_spacing_ticks,
        )
        for index in range(levels)
    )
    asks = tuple(
        PlannedLevel(
            "SELL",
            midpoint_ticks
            + inside_spread_ticks
            + index * level_spacing_ticks,
        )
        for index in range(levels)
    )
    if bids[-1].price_ticks <= 0:
        raise ValueError("ladder would create a non-positive bid")
    if max(level.price_ticks for level in bids) >= min(
        level.price_ticks for level in asks
    ):
        raise ValueError("ladder must remain non-crossing")
    return bids + asks


def next_midpoint(
    current_ticks: int,
    *,
    initial_ticks: int,
    band_ticks: int,
    rng: random.Random,
) -> int:
    """Take one deterministic bounded random-walk step."""

    candidate = current_ticks + rng.choice((-3, -2, -1, 0, 1, 2, 3))
    lower = initial_ticks - band_ticks
    upper = initial_ticks + band_ticks
    return min(max(candidate, lower), upper)


class LiveMarketState:
    """Thread-safe order/report state shared with QuickFIX callbacks."""

    def __init__(self, instrument: Instrument) -> None:
        self.instrument = instrument
        self._condition = threading.Condition()
        self._quotes: dict[str, LiveQuote] = {}
        self._statuses: dict[str, str] = {}

    def register_quote(self, quote: LiveQuote) -> None:
        with self._condition:
            if quote.cl_ord_id in self._quotes:
                raise ValueError(f"duplicate quote id: {quote.cl_ord_id}")
            self._quotes[quote.cl_ord_id] = quote
            self._statuses[quote.cl_ord_id] = quote.status

    def record_report(self, report: FixReport) -> None:
        with self._condition:
            self._statuses[report.cl_ord_id] = report.status
            target_id = report.orig_cl_ord_id or report.cl_ord_id
            if report.orig_cl_ord_id is not None:
                self._statuses[target_id] = report.status
            quote = self._quotes.get(target_id)
            if quote is not None:
                if report.status in TERMINAL_STATUSES:
                    self._quotes.pop(target_id, None)
                else:
                    leaves = quote.leaves_qty_lots
                    if report.leaves_qty is not None:
                        leaves = self.instrument.quantity_to_lots(
                            report.leaves_qty
                        )
                    self._quotes[target_id] = replace(
                        quote,
                        leaves_qty_lots=leaves,
                        status=report.status,
                    )
            self._condition.notify_all()

    def active_quotes(self) -> tuple[LiveQuote, ...]:
        with self._condition:
            return tuple(self._quotes.values())

    def status(self, cl_ord_id: str) -> Optional[str]:
        with self._condition:
            return self._statuses.get(cl_ord_id)

    def wait_for_notification(self, timeout: float) -> None:
        with self._condition:
            self._condition.wait(timeout)


class DemoAdmin:
    """Small typed boundary around the lifecycle and clock REST routes."""

    def __init__(self, base_url: str, timeout: float) -> None:
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
        )

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            body = response.json()
        except ValueError:
            body = response.text
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path} "
            f"failed with HTTP {response.status_code}: {body}"
        )

    def wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                response = self.client.get("/readyz")
                if response.status_code == 200:
                    return
                last_error = RuntimeError(response.text)
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(0.1)
        raise RuntimeError(f"MockX did not become ready: {last_error}")

    def start_session(self, scenario_label: str) -> dict:
        response = self.client.post(
            "/api/v1/session/start",
            json={"scenario_label": scenario_label},
        )
        self._raise(response)
        return response.json()

    def end_session_if_owned(self, run_id: str) -> bool:
        stats = self.client.get("/stats")
        self._raise(stats)
        current = stats.json()
        if not current["run_active"] or current["run_id"] != run_id:
            return False
        response = self.client.post("/api/v1/session/end")
        self._raise(response)
        return True

    def advance(self, milliseconds: int) -> dict:
        response = self.client.post(
            "/admin/advance-time",
            json={"milliseconds": milliseconds},
        )
        self._raise(response)
        return response.json()

    def book(self, symbol: str) -> dict:
        response = self.client.get(f"/book/{symbol}")
        self._raise(response)
        return response.json()

    def close(self) -> None:
        self.client.close()


class LiveMarket:
    """Reconcile maker quotes and submit bounded taker flow."""

    def __init__(
        self,
        *,
        app: MultiSessionFIXApplication,
        state: LiveMarketState,
        admin: DemoAdmin,
        instrument: Instrument,
        levels: int,
        quote_quantity_lots: int,
        advance_ms: int,
        timeout: float,
        rng: random.Random,
        id_prefix: str,
    ) -> None:
        self.app = app
        self.state = state
        self.admin = admin
        self.instrument = instrument
        self.levels = levels
        self.quote_quantity_lots = quote_quantity_lots
        self.advance_ms = advance_ms
        self.timeout = timeout
        self.rng = rng
        self.id_prefix = id_prefix
        self._next_id = 1

    def _id(self, prefix: str) -> str:
        value = f"{self.id_prefix}-{prefix}-{self._next_id:08d}"
        self._next_id += 1
        return value

    def _pump_until(
        self,
        predicate: Callable[[], bool],
        *,
        description: str,
    ) -> None:
        deadline = time.monotonic() + self.timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for {description}")
            self.admin.advance(self.advance_ms)
            self.state.wait_for_notification(
                min(0.05, max(deadline - time.monotonic(), 0))
            )

    def _wait_for_statuses(
        self,
        cl_ord_ids: Iterable[str],
        allowed: frozenset[str],
        description: str,
    ) -> None:
        expected = tuple(cl_ord_ids)
        self._pump_until(
            lambda: all(
                self.state.status(cl_ord_id) in allowed
                for cl_ord_id in expected
            ),
            description=description,
        )

    def reconcile(self, midpoint_ticks: int) -> None:
        desired = frozenset(
            build_ladder(midpoint_ticks, levels=self.levels)
        )
        active = self.state.active_quotes()
        cancel_ids: list[str] = []
        maker_session = self.app.session(MAKER_ROLE)
        for quote in active:
            needs_refresh = (
                quote.level not in desired
                or quote.leaves_qty_lots != quote.quantity_lots
                or quote.status == "PARTIAL"
            )
            if not needs_refresh:
                continue
            cancel_id = self._id("CXL")
            send_cancel(
                maker_session,
                cl_ord_id=cancel_id,
                orig_cl_ord_id=quote.cl_ord_id,
                symbol=self.instrument.symbol,
                side=(
                    fix.Side_BUY
                    if quote.level.side == "BUY"
                    else fix.Side_SELL
                ),
                quantity=self.instrument.format_quantity(
                    quote.quantity_lots
                ),
            )
            cancel_ids.append(cancel_id)
        if cancel_ids:
            self._wait_for_statuses(
                cancel_ids,
                TERMINAL_STATUSES,
                "quote cancellations",
            )

        current_levels = {
            quote.level for quote in self.state.active_quotes()
        }
        new_ids: list[str] = []
        for level in sorted(desired - current_levels):
            cl_ord_id = self._id("MM")
            quote = LiveQuote(
                cl_ord_id=cl_ord_id,
                level=level,
                quantity_lots=self.quote_quantity_lots,
                leaves_qty_lots=self.quote_quantity_lots,
            )
            self.state.register_quote(quote)
            send_limit_order(
                maker_session,
                cl_ord_id=cl_ord_id,
                symbol=self.instrument.symbol,
                side=(
                    fix.Side_BUY
                    if level.side == "BUY"
                    else fix.Side_SELL
                ),
                quantity=self.instrument.format_quantity(
                    self.quote_quantity_lots
                ),
                price=self.instrument.format_price(level.price_ticks),
            )
            new_ids.append(cl_ord_id)
        if new_ids:
            self._wait_for_statuses(
                new_ids,
                frozenset({"NEW", "REJECTED"}),
                "new quote acknowledgements",
            )
            rejected = [
                cl_ord_id
                for cl_ord_id in new_ids
                if self.state.status(cl_ord_id) == "REJECTED"
            ]
            if rejected:
                raise RuntimeError(f"Maker quotes rejected: {rejected}")

    def take_liquidity(self) -> str:
        side = self.rng.choice((fix.Side_BUY, fix.Side_SELL))
        lower = max(1, self.quote_quantity_lots // 2)
        upper = max(lower, self.quote_quantity_lots * 2)
        quantity_lots = self.rng.randint(lower, upper)
        cl_ord_id = self._id("TAKER")
        send_market_order(
            self.app.session(TAKER_ROLE),
            cl_ord_id=cl_ord_id,
            symbol=self.instrument.symbol,
            side=side,
            quantity=self.instrument.format_quantity(quantity_lots),
        )
        self._wait_for_statuses(
            (cl_ord_id,),
            TERMINAL_STATUSES,
            "taker terminal report",
        )
        return cl_ord_id


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a continuous two-session FIX market against the example "
            "MockX configuration."
        )
    )
    parser.add_argument(
        "--config",
        default="examples/live_exchange.yml",
        help="MockX YAML configuration used by the running server",
    )
    parser.add_argument(
        "--fix-config",
        default="examples/live_client.cfg",
        help="QuickFIX initiator configuration",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Admin API base URL (defaults to the YAML host and port)",
    )
    parser.add_argument(
        "--dashboard-url",
        default="http://localhost:3000/d/mockx-native/mockx-native-exchange",
        help="External Grafana dashboard URL printed after startup",
    )
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--scenario-label", default="live-fix-demo")
    parser.add_argument("--mid-price", default="100.00")
    parser.add_argument("--quote-quantity", default="1.000")
    parser.add_argument("--levels", type=int, default=8)
    parser.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="Number of cycles; 0 runs until Ctrl+C",
    )
    parser.add_argument("--interval", type=float, default=0.75)
    parser.add_argument("--advance-ms", type=int, default=25)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--verbose-fix", action="store_true")
    return parser.parse_args(argv)


def _base_url(config, override: Optional[str]) -> str:
    if override:
        return override.rstrip("/")
    host = config.admin_api.host
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return f"http://{host}:{config.admin_api.port}"


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_quickfix()
        if args.levels <= 0:
            raise ValueError("--levels must be positive")
        if args.cycles < 0:
            raise ValueError("--cycles cannot be negative")
        if args.interval < 0:
            raise ValueError("--interval cannot be negative")
        if args.advance_ms <= 0 or args.timeout <= 0:
            raise ValueError("--advance-ms and --timeout must be positive")

        config = load_config(args.config)
        if config.scheduler.mode != "external":
            raise ValueError("live demo requires scheduler.mode=external")
        metadata = config.instruments.get(args.symbol.strip().upper())
        if metadata is None:
            raise ValueError(f"unknown configured symbol: {args.symbol}")
        instrument = Instrument.from_config(
            args.symbol.strip().upper(),
            metadata,
        )
        initial_midpoint = instrument.price_to_ticks(args.mid_price)
        quote_quantity_lots = instrument.quantity_to_lots(
            args.quote_quantity
        )
    except (ConfigurationError, RuntimeError, ValueError) as exc:
        print(f"Invalid live demo configuration: {exc}", file=sys.stderr)
        return 2

    admin_base_url = _base_url(config, args.base_url)
    admin = DemoAdmin(admin_base_url, args.timeout)
    initiator = None
    started_session = False
    owned_run_id = None
    interrupted = False
    try:
        app = MultiSessionFIXApplication(
            print_reports=args.verbose_fix
        )
        state = LiveMarketState(instrument)
        app.add_listener(state.record_report)
        settings = fix.SessionSettings(args.fix_config)
        store_factory = fix.MemoryStoreFactory()
        log_factory = fix.ScreenLogFactory(settings)
        initiator = fix.SocketInitiator(
            app,
            store_factory,
            settings,
            log_factory,
        )
        admin.wait_ready(args.timeout)
        initiator.start()
        if not app.wait_for_sessions(
            (MAKER_ROLE, TAKER_ROLE),
            args.timeout,
        ):
            raise TimeoutError(
                "Timed out waiting for liquidity_bot and taker_bot logons"
            )
        started = admin.start_session(args.scenario_label)
        started_session = True
        owned_run_id = started['session_id']
        print(
            f"Started {started['session_id']} generation "
            f"{started['run_generation']}"
        )
        print(f"Open {args.dashboard_url}")

        rng = random.Random(args.seed)
        market = LiveMarket(
            app=app,
            state=state,
            admin=admin,
            instrument=instrument,
            levels=args.levels,
            quote_quantity_lots=quote_quantity_lots,
            advance_ms=args.advance_ms,
            timeout=args.timeout,
            rng=rng,
            id_prefix=f"G{started['run_generation']:08d}",
        )
        midpoint = initial_midpoint
        cycle = 0
        while args.cycles == 0 or cycle < args.cycles:
            cycle += 1
            market.reconcile(midpoint)
            taker_id = market.take_liquidity()
            book = admin.book(instrument.symbol)
            best_bid = book["bids"][0]["price"] if book["bids"] else "-"
            best_ask = book["asks"][0]["price"] if book["asks"] else "-"
            print(
                f"Cycle {cycle}: mid={instrument.format_price(midpoint)} "
                f"bid={best_bid} ask={best_ask} taker={taker_id}"
            )
            midpoint = next_midpoint(
                midpoint,
                initial_ticks=initial_midpoint,
                band_ticks=200,
                rng=rng,
            )
            if args.interval:
                time.sleep(args.interval)  # Wall-clock pacing only.
        return 0
    except KeyboardInterrupt:
        interrupted = True
        print("Stopping live demo...")
        return 130
    except (httpx.HTTPError, RuntimeError, TimeoutError, fix.ConfigError) as exc:
        print(f"Live demo failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if started_session:
            try:
                if admin.end_session_if_owned(owned_run_id):
                    print(f"Ended simulation session {owned_run_id}")
            except Exception as exc:
                print(
                    f"Failed to end simulation session: {exc}",
                    file=sys.stderr,
                )
        try:
            if initiator is not None:
                initiator.stop()
        finally:
            admin.close()
        if interrupted:
            sys.stdout.flush()


def _termination_requested(
    signal_number: int,
    frame: Optional[FrameType],
) -> None:
    """Translate container termination into the normal cleanup path."""

    del signal_number, frame
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    previous_handler = signal.signal(
        signal.SIGTERM,
        _termination_requested,
    )
    try:
        return run(argv)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())

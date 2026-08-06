#!/usr/bin/env python3
"""Reset and seed the two venues used by ``mockx_sor`` over FIX."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import partial
import json
from pathlib import Path
import sys
import threading
import time
from typing import Callable, Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from examples.fix_support import (  # noqa: E402
    FixReport,
    MultiSessionFIXApplication,
    fix,
    require_quickfix,
    send_limit_order,
)


SEED_ROLES = ("SEED_A", "SEED_B")


class BootstrapError(RuntimeError):
    """The SOR environment could not be prepared."""


class JsonClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]: ...


class FixDriver(Protocol):
    def start(self) -> None: ...

    def wait_for_sessions(
        self,
        roles: Iterable[str],
        timeout: float,
    ) -> bool: ...

    def submit(self, orders: tuple["FixOrder", ...]) -> None: ...

    def wait_for_new(
        self,
        cl_ord_ids: Iterable[str],
        timeout: float,
    ) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class FixOrder:
    role: str
    cl_ord_id: str
    symbol: str
    side: str
    quantity: str
    price: str
    time_in_force: str


@dataclass(frozen=True)
class OrderSpec:
    level_id: str
    price: str
    quantity: str

    def materialize(self, role: str, generation: int) -> FixOrder:
        venue = role.removeprefix("SEED_")
        return FixOrder(
            role=role,
            cl_ord_id=(f"SEED-{venue}-G{generation:08d}-{self.level_id}"),
            symbol="BTC-USDT",
            side="SELL",
            quantity=self.quantity,
            price=self.price,
            time_in_force="GTC",
        )


@dataclass(frozen=True)
class VenueSpec:
    name: str
    base_url: str
    run_id: str
    role: str
    orders: tuple[OrderSpec, ...]
    expected_asks: tuple[tuple[str, str, int], ...]


def venue_specs(venue_a_url: str, venue_b_url: str) -> tuple[VenueSpec, ...]:
    return (
        VenueSpec(
            name="Venue A",
            base_url=venue_a_url.rstrip("/"),
            run_id="sor-a",
            role="SEED_A",
            orders=(
                OrderSpec("100", "100.00", "0.002"),
                OrderSpec("102", "102.00", "0.009"),
            ),
            expected_asks=(("100.00", "0.002", 1), ("102.00", "0.009", 1)),
        ),
        VenueSpec(
            name="Venue B",
            base_url=venue_b_url.rstrip("/"),
            run_id="sor-b",
            role="SEED_B",
            orders=(
                OrderSpec("99", "99.00", "0.003"),
                OrderSpec("100", "100.00", "0.001"),
            ),
            expected_asks=(("99.00", "0.003", 1), ("100.00", "0.001", 1)),
        ),
    )


class HttpJsonClient:
    def __init__(self, request_timeout_seconds: float) -> None:
        self.request_timeout_seconds = request_timeout_seconds

    def request(
        self,
        method: str,
        url: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        encoded = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            serialized = json.dumps(payload, separators=(",", ":"))
            encoded = serialized.encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=encoded, headers=headers, method=method)
        try:
            with urlopen(
                request,
                timeout=self.request_timeout_seconds,
            ) as response:
                body = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BootstrapError(
                f"{method} {url} returned HTTP {exc.code}: {detail}"
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise BootstrapError(f"{method} {url} failed: {exc}") from exc

        if not body:
            return {}
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            message = f"{method} {url} returned invalid JSON"
            raise BootstrapError(message) from exc
        if not isinstance(decoded, dict):
            message = f"{method} {url} returned a non-object JSON body"
            raise BootstrapError(message)
        return decoded


class SeedReportState:
    """Wait for one NEW Execution Report per seeded order."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._expected: frozenset[str] = frozenset()
        self._accepted: set[str] = set()
        self._failure: str | None = None

    def expect(self, cl_ord_ids: Iterable[str]) -> None:
        expected = frozenset(cl_ord_ids)
        if not expected:
            raise ValueError("at least one seed order is required")
        with self._condition:
            self._expected = expected
            self._accepted.clear()
            self._failure = None

    def record(self, report: FixReport) -> None:
        if report.message_type != "ExecutionReport":
            return
        with self._condition:
            if report.cl_ord_id not in self._expected:
                return
            if report.status == "NEW":
                self._accepted.add(report.cl_ord_id)
            else:
                detail = f"seed order {report.cl_ord_id}"
                detail += f" returned {report.status}"
                if report.text:
                    detail = f"{detail}: {report.text}"
                self._failure = detail
            self._condition.notify_all()

    def wait_for_new(self, cl_ord_ids: Iterable[str], timeout: float) -> None:
        expected = frozenset(cl_ord_ids)
        deadline = time.monotonic() + timeout
        with self._condition:
            while not expected <= self._accepted:
                if self._failure is not None:
                    raise BootstrapError(self._failure)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = sorted(expected - self._accepted)
                    raise BootstrapError(
                        "timed out waiting for NEW Execution Reports: "
                        + ", ".join(missing)
                    )
                self._condition.wait(remaining)


class QuickFixSeedDriver:
    """One-shot two-session QuickFIX initiator for passive liquidity."""

    def __init__(self, fix_config: str) -> None:
        require_quickfix()
        assert fix is not None
        self.application = MultiSessionFIXApplication(print_reports=True)
        self.reports = SeedReportState()
        self.application.add_listener(self.reports.record)
        self._started = False
        try:
            settings = fix.SessionSettings(fix_config)
            self.initiator = fix.SocketInitiator(
                self.application,
                fix.MemoryStoreFactory(),
                settings,
                fix.ScreenLogFactory(settings),
            )
        except Exception as exc:
            raise BootstrapError(f"FIX setup failed: {exc}") from exc

    def start(self) -> None:
        self._started = True
        try:
            self.initiator.start()
        except Exception as exc:
            raise BootstrapError(f"FIX initiator start failed: {exc}") from exc

    def wait_for_sessions(
        self,
        roles: Iterable[str],
        timeout: float,
    ) -> bool:
        return self.application.wait_for_sessions(roles, timeout)

    def submit(self, orders: tuple[FixOrder, ...]) -> None:
        require_quickfix()
        assert fix is not None
        self.reports.expect(order.cl_ord_id for order in orders)
        for order in orders:
            if order.side != "SELL" or order.time_in_force != "GTC":
                message = "seed orders must be GTC SELL limit orders"
                raise BootstrapError(message)
            try:
                send_limit_order(
                    self.application.session(order.role),
                    cl_ord_id=order.cl_ord_id,
                    symbol=order.symbol,
                    side=fix.Side_SELL,
                    quantity=order.quantity,
                    price=order.price,
                    tif=fix.TimeInForce_GOOD_TILL_CANCEL,
                )
            except (RuntimeError, ValueError) as exc:
                raise BootstrapError(
                    f"failed to submit {order.cl_ord_id}: {exc}"
                ) from exc
            print(
                f"Submitted FIX seed order {order.cl_ord_id}: "
                f"SELL {order.quantity} {order.symbol} @ {order.price}",
                flush=True,
            )

    def wait_for_new(
        self,
        cl_ord_ids: Iterable[str],
        timeout: float,
    ) -> None:
        self.reports.wait_for_new(cl_ord_ids, timeout)

    def stop(self) -> None:
        if not getattr(self, "_started", False):
            return
        self._started = False
        try:
            self.initiator.stop()
        except Exception as exc:
            raise BootstrapError(f"FIX initiator stop failed: {exc}") from exc


def _wait_for(
    client: JsonClient,
    *,
    description: str,
    url: str,
    predicate: Callable[[dict[str, object]], bool],
    deadline: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    last_problem = "condition was not met"
    while True:
        try:
            response = client.request("GET", url)
            if predicate(response):
                return
            last_problem = f"last response was {response!r}"
        except BootstrapError as exc:
            last_problem = str(exc)
        if monotonic() >= deadline:
            message = f"timed out waiting for {description}: {last_problem}"
            raise BootstrapError(message)
        sleep(poll_interval_seconds)


def _book_matches(response: dict[str, object], venue: VenueSpec) -> bool:
    if response.get("bids") != []:
        return False
    asks = response.get("asks")
    if not isinstance(asks, list) or len(asks) != len(venue.expected_asks):
        return False
    actual = tuple(
        (level.get("price"), level.get("qty"), level.get("order_count"))
        for level in asks
        if isinstance(level, dict)
    )
    return actual == venue.expected_asks


def _run_generation(response: dict[str, object], venue: VenueSpec) -> int:
    generation = response.get("run_generation")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise BootstrapError(
            f"{venue.name} session start returned invalid run_generation"
        )
    if generation <= 0:
        raise BootstrapError(
            f"{venue.name} session start returned non-positive run_generation"
        )
    return generation


def _traffic_ready(response: dict[str, object]) -> bool:
    return response.get("traffic_ready") is True


def bootstrap(
    venues: tuple[VenueSpec, ...],
    client: JsonClient,
    fix_driver: FixDriver,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = monotonic() + timeout_seconds

    try:
        for venue in venues:
            _wait_for(
                client,
                description=f"{venue.name} readiness",
                url=f"{venue.base_url}/readyz",
                predicate=_traffic_ready,
                deadline=deadline,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
            )

        for venue in venues:
            client.request("POST", f"{venue.base_url}/api/v1/session/end")

        generations: dict[str, int] = {}
        for venue in venues:
            response = client.request(
                "POST",
                f"{venue.base_url}/api/v1/session/start",
                {"run_id": venue.run_id},
            )
            generations[venue.role] = _run_generation(response, venue)

        fix_driver.start()
        if not fix_driver.wait_for_sessions(SEED_ROLES, timeout_seconds):
            message = "FIX logon timed out waiting for SEED_A and SEED_B"
            raise BootstrapError(message)

        orders = tuple(
            order.materialize(venue.role, generations[venue.role])
            for venue in venues
            for order in venue.orders
        )
        fix_driver.submit(orders)
        fix_driver.wait_for_new(
            (order.cl_ord_id for order in orders),
            timeout_seconds,
        )

        for venue in venues:
            _wait_for(
                client,
                description=f"{venue.name} seeded book",
                url=f"{venue.base_url}/book/BTC-USDT",
                predicate=partial(_book_matches, venue=venue),
                deadline=deadline,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
            )
    finally:
        fix_driver.stop()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix-config",
        default="examples/cpp_sor/seed_books.cfg",
    )
    parser.add_argument("--venue-a-url", default="http://127.0.0.1:8101")
    parser.add_argument("--venue-b-url", default="http://127.0.0.1:8102")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=3.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.timeout_seconds <= 0:
        message = "bootstrap error: --timeout-seconds must be positive"
        print(message, file=sys.stderr)
        return 2
    if args.request_timeout_seconds <= 0:
        print(
            "bootstrap error: --request-timeout-seconds must be positive",
            file=sys.stderr,
        )
        return 2
    if args.poll_interval_seconds < 0:
        print(
            "bootstrap error: --poll-interval-seconds cannot be negative",
            file=sys.stderr,
        )
        return 2

    venues = venue_specs(args.venue_a_url, args.venue_b_url)
    try:
        fix_driver = QuickFixSeedDriver(args.fix_config)
        bootstrap(
            venues,
            HttpJsonClient(args.request_timeout_seconds),
            fix_driver,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    except BootstrapError as exc:
        print(f"SOR bootstrap failed: {exc}", file=sys.stderr, flush=True)
        return 1

    print(
        "SOR books ready: "
        "Venue A asks 0.002@100.00, 0.009@102.00; "
        "Venue B asks 0.003@99.00, 0.001@100.00",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

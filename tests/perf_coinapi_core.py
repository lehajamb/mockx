"""Manual native-core diagnostic; this is not a live FIX throughput claim.

Run from the repository root::

    python -m tests.perf_coinapi_core --input CAPTURE.csv --commands 3000 10000

The complete source snapshot is installed for every trial. Source parsing and
startup are timed separately; the command timer includes native command
construction, scheduler dispatch, matching, reports and immutable book snapshots.
No sockets, persistence or metrics observers are attached. A full independent
book audit and active-order count scan run after, outside the command timer.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import timedelta
from itertools import islice
import json
from pathlib import Path
import time

from examples.coinapi_replay.source import open_source
from src.mockx.configuration import load_config
from src.mockx.engine import MatchingEngine
from src.mockx.identifiers import RunIdentifierScope
from src.mockx.instruments import InstrumentCatalog
from src.mockx.latency import LatencyScheduler
from src.mockx.models import (
    CancelOrderCommand,
    ExecutionParams,
    NewOrderCommand,
    OrderType,
    ReplaceOrderCommand,
    Side,
    TERMINAL_ORDER_STATUSES,
    TimeInForce,
)


def new_command(instrument, scheduler, name, side, quantity, price, *, ioc=False):
    role = f"replay_{side.value.lower()}"
    return NewOrderCommand(
        cl_ord_id=name,
        symbol=instrument.symbol,
        side=side,
        order_type=OrderType.LIMIT,
        order_qty_lots=quantity,
        price_ticks=price,
        tif=TimeInForce.IOC if ioc else TimeInForce.GTC,
        submitted_at=scheduler.current_time,
        participant_id=role,
        session_id=role,
        session_alias=role,
    )


async def trial(instrument, snapshot, events, start_ns, requested):
    scheduler = LatencyScheduler()
    core = MatchingEngine(
        ExecutionParams(0, 0, reject_prob=0, rand_seed=42),
        scheduler,
        InstrumentCatalog({instrument.symbol: instrument}),
    )
    scheduler.register_handler("order_arrived", lambda command: None)
    scheduler.register_handler("order_process", core.process_order)
    seed_start = time.perf_counter()
    identifiers = RunIdentifierScope(1)
    initial = core.prepare_initial_book(
        tuple(
            new_command(
                instrument,
                scheduler,
                f"seed-{index}",
                order.side,
                order.quantity_lots,
                order.price_ticks,
            )
            for index, order in enumerate(snapshot)
        ),
        identifiers,
    )
    core.install_identifier_scope(identifiers)
    core.install_initial_book(initial)
    core.publish_initial_book(initial)
    seed_seconds = time.perf_counter() - seed_start
    source_orders = {
        source.source_id: core.orders[index + 1]
        for index, source in enumerate(snapshot)
    }
    origin = scheduler.current_time
    operations = Counter()
    source_events = commands = 0
    started = time.perf_counter()
    for event in events:
        source_events += 1
        target = origin + timedelta(microseconds=(event.time_ns - start_ns) // 1000)
        await scheduler.run_until(target)
        order = source_orders.get(event.source_id)
        name = f"event-{event.index}"
        if event.operation in {"ADD", "MATCH"}:
            is_match = event.operation == "MATCH"
            side = (
                (Side.SELL if event.side == Side.BUY else Side.BUY)
                if is_match
                else event.side
            )
            internal = core.submit_order(
                new_command(
                    instrument,
                    scheduler,
                    name,
                    side,
                    event.quantity_lots,
                    event.price_ticks,
                    ioc=is_match,
                )
            )
            if not is_match:
                source_orders[event.source_id] = core.orders[internal]
            await scheduler.run_until(target)
            operations["ioc" if is_match else "new"] += 1
        elif order is not None and order.status not in TERMINAL_ORDER_STATUSES:
            total = max(
                order.filled_qty_lots,
                event.source_remaining_lots + event.historical_matched_lots,
            )
            common = dict(
                orig_cl_ord_id=order.cl_ord_id,
                symbol=order.symbol,
                side=order.side,
                requested_at=target,
                session_id=order.session_id,
                participant_id=order.participant_id,
                run_generation=1,
            )
            if event.source_remaining_lots == 0 or total == order.filled_qty_lots:
                core.cancel_order(
                    CancelOrderCommand(
                        cancel_cl_ord_id=name,
                        order_qty_lots=order.order_qty_lots,
                        **common,
                    )
                )
                operations["cancel"] += 1
            elif total != order.order_qty_lots:
                core.replace_order(
                    ReplaceOrderCommand(
                        replace_cl_ord_id=name,
                        order_qty_lots=total,
                        price_ticks=order.price_ticks,
                        order_type=OrderType.LIMIT,
                        tif=TimeInForce.GTC,
                        **common,
                    )
                )
                operations["replace"] += 1
            else:
                continue
        else:
            continue
        commands += 1
        if commands == requested:
            break
    seconds = time.perf_counter() - started
    if commands < requested:
        raise RuntimeError(
            f"Only {commands} commands available; request a smaller trial"
        )
    audit_start = time.perf_counter()
    core.books[instrument.symbol].assert_invariants()
    assert core.active_order_count() == sum(
        order.status not in TERMINAL_ORDER_STATUSES for order in core.orders.values()
    )
    return {
        "commands": commands,
        "commands_per_second": commands / seconds,
        "command_seconds": seconds,
        "source_events": source_events,
        "operations": dict(operations),
        "seed_orders": len(snapshot),
        "seed_seconds": seed_seconds,
        "retained_orders": len(core.orders),
        "resting_orders": core.resting_order_count(),
        "price_levels": core.order_book_level_count(),
        "final_audit_seconds": time.perf_counter() - audit_start,
        "final_audit": "passed",
        "scope": "native_core_without_network_or_metrics",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("examples/coinapi_exchange.yml")
    )
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--source-symbol")
    parser.add_argument("--date")
    parser.add_argument("--commands", type=int, nargs="+", default=[3000, 10000])
    arguments = parser.parse_args(argv)
    if min(arguments.commands) <= 0:
        parser.error("--commands must be positive")
    instrument = InstrumentCatalog.from_config(load_config(arguments.config)).require(
        arguments.symbol
    )
    prepare_start = time.perf_counter()
    with open_source(
        arguments.input,
        instrument,
        date=arguments.date,
        source_symbol=arguments.source_symbol,
    ) as source:
        snapshot, start_ns = source.snapshot, source.start_ns
        # Extra source rows cover no-op updates and already-consumed liquidity.
        events = tuple(islice(source.events(), max(arguments.commands) * 4))
    print(
        json.dumps(
            {
                "source_prepare_seconds": time.perf_counter() - prepare_start,
                "seed_orders": len(snapshot),
                "prepared_events": len(events),
            }
        ),
        flush=True,
    )
    for count in arguments.commands:
        print(
            json.dumps(
                asyncio.run(trial(instrument, snapshot, events, start_ns, count))
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()

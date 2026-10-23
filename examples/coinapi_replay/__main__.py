"""Run with python -m examples.coinapi_replay --help from the repository root."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

import httpx

from src.mockx.configuration import load_config
from src.mockx.instruments import Instrument, parse_decimal

from .controller import ControllerConfig
from .runner import AuditLog, ReplayOptions, ReplayRunner
from .source import prepare_csv
from .transport import NativeFixTransport


def parser():
    result = argparse.ArgumentParser(description="Replay a local CoinAPI L3 CSV through ordinary MockX FIX")
    result.add_argument("--input", required=True, type=Path, help="L3 .csv or .csv.gz")
    result.add_argument("--config", default="examples/coinapi_exchange.yml")
    result.add_argument("--fix-config", default="examples/coinapi_client.cfg")
    result.add_argument("--symbol", required=True, help="Destination MockX instrument")
    result.add_argument("--source-symbol", help="Assert CoinAPI symbol when absent from filename/header")
    result.add_argument("--date", help="UTC YYYY-MM-DD for time-only CSV without a D-YYYYMMDD path")
    result.add_argument("--base-url", help="MockX HTTP URL; defaults to configured admin host/port")
    result.add_argument("--profile", choices=("exact", "interactive"), default="exact")
    result.add_argument("--seed-mode", choices=("bulk", "fix"), default="bulk")
    result.add_argument("--speed", type=float, default=1.0, help="Interactive historical-time/wall-time multiplier")
    result.add_argument("--timeout", type=float, default=15.0)
    result.add_argument("--max-pending", type=int, default=10000)
    result.add_argument("--run-id", default="coinapi-replay")
    result.add_argument("--prepare-only", action="store_true", help="Validate source and report metadata; do not connect")
    result.add_argument("--output", type=Path, default=Path("coinapi-replay-result.json"))
    result.add_argument("--audit", type=Path, help="JSONL evidence; defaults to output path with .events.jsonl suffix")
    result.add_argument("--midpoint-control", action="store_true", help="Enable independent controller module (interactive only)")
    result.add_argument("--controller-max-qty", help="Maximum base quantity per market order; required when enabled")
    result.add_argument("--controller-total-qty", help="Run-wide submitted quantity budget; required when enabled")
    result.add_argument("--controller-depth", type=int, help="Maximum levels consumed; required when enabled")
    result.add_argument("--controller-reaction-ms", type=int, help="Minimum reaction/order interval; required when enabled")
    result.add_argument("--controller-stale-ms", type=int, default=5000)
    result.add_argument("--controller-lag-ms", type=int, default=100)
    return result


def controller_settings(args, instrument):
    if not args.midpoint_control:
        return ControllerConfig()
    if args.profile != "interactive":
        raise ValueError("Midpoint control is available only with --profile interactive")
    required = (args.controller_max_qty, args.controller_total_qty,
                args.controller_depth, args.controller_reaction_ms)
    if any(value is None for value in required):
        raise ValueError("Controller requires explicit max-qty, total-qty, depth and reaction-ms budgets")
    return ControllerConfig(
        enabled=True,
        max_order_qty_lots=instrument.quantity_to_lots(parse_decimal(args.controller_max_qty, field_name="controller max qty")),
        max_total_qty_lots=instrument.quantity_to_lots(parse_decimal(args.controller_total_qty, field_name="controller total qty")),
        max_depth_levels=args.controller_depth,
        reaction_interval_ns=args.controller_reaction_ms * 1_000_000,
        max_book_age_ns=args.controller_stale_ms * 1_000_000,
        max_target_age_ns=args.controller_stale_ms * 1_000_000,
        max_replay_lag_ns=args.controller_lag_ms * 1_000_000,
    )


async def execute(args):
    config = load_config(args.config)
    symbol = args.symbol.upper()
    if symbol not in config.instruments:
        raise ValueError(f"Unknown configured instrument {symbol}")
    instrument = Instrument.from_config(symbol, config.instruments[symbol])
    control = controller_settings(args, instrument)
    options = ReplayOptions(profile=args.profile, seed_mode=args.seed_mode, speed=args.speed,
                            timeout=args.timeout, max_pending=args.max_pending, run_id=args.run_id)
    with prepare_csv(args.input, instrument, date=args.date, symbol=args.source_symbol) as prepared:
        if args.profile == "exact":
            prepared.assert_exact_runtime_precision()
        if args.prepare_only:
            result = dict(status="prepared_not_execution_qualified", source_sha256=prepared.source_sha256,
                          source_symbol=prepared.source_symbol, snapshot_orders=len(prepared.snapshot),
                          events=prepared.event_count, start_ns=prepared.start_ns, end_ns=prepared.end_ns,
                          snapshot_digest=prepared.snapshot_digest)
        else:
            host = "127.0.0.1" if config.admin_api.host == "0.0.0.0" else config.admin_api.host
            url = args.base_url or f"http://{host}:{config.admin_api.port}"
            audit = AuditLog(args.audit or args.output.with_suffix(".events.jsonl"))
            try:
                async with httpx.AsyncClient(base_url=url, timeout=args.timeout) as admin:
                    transport = NativeFixTransport(args.fix_config, instrument)
                    runner = ReplayRunner(prepared, instrument, config, admin, transport,
                                          options=options, controller_config=control, audit=audit)
                    result = await runner.run()
            finally:
                audit.close()
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        asyncio.run(execute(args))
    except KeyboardInterrupt:
        print("Replay interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Replay failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Launch an owned MockX exchange and stream a local CoinAPI capture into FIX."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import httpx
import yaml

from src.mockx.configuration import load_config
from src.mockx.instruments import Instrument

from .runner import ReplayOptions, ReplayRunner
from .source import open_source
from .native_probe import NativeProbe
from .memory import ProcessMemorySampler
from .timing import replay_timer_resolution


ROOT = Path(__file__).resolve().parents[2]
ALL_ROLES = ("replay_buy", "replay_sell", "controller", "strategy_bot")


def fix_settings(*, output, role_names, initiator, port, store_name):
    output = Path(output).resolve()
    dictionary = (ROOT / "configs" / "FIX44.xml").as_posix()
    defaults = {
        "ConnectionType": "initiator" if initiator else "acceptor",
        "ReconnectInterval": "2",
        "StartTime": "00:00:00",
        "EndTime": "00:00:00",
        "UseDataDictionary": "Y",
        "DataDictionary": dictionary,
        "FileStorePath": (output / (store_name + "-store")).as_posix(),
        "FileLogPath": (output / (store_name + "-logs")).as_posix(),
        "HeartBtInt": "30",
        "ResetOnLogon": "Y",
        "ResetOnLogout": "Y",
        "ResetOnDisconnect": "Y",
        "RefreshOnLogon": "Y",
        "ValidateFieldsOutOfOrder": "N",
        "ValidateFieldsHaveValues": "N",
        "ValidateUserDefinedFields": "N",
        "AllowUnknownMsgFields": "Y",
        "SocketNodelay": "Y",
    }
    if initiator:
        defaults.update(
            TargetCompID="MOCKX_COINAPI",
            SocketConnectHost="127.0.0.1",
            SocketConnectPort=str(port),
        )
    else:
        defaults.update(SenderCompID="MOCKX_COINAPI", SocketAcceptPort=str(port))
    lines = ["[DEFAULT]", *(f"{key}={value}" for key, value in defaults.items())]
    for role in role_names:
        lines += [
            "",
            "[SESSION]",
            "BeginString=FIX.4.4",
            f"{'SenderCompID' if initiator else 'TargetCompID'}={role}",
        ]
    return "\n".join(lines) + "\n"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", required=True, type=Path, help="Local Coinbase L3 CSV or CSV.gz"
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "examples" / "coinapi_exchange.yml"
    )
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument(
        "--source-symbol",
        help="Expected CoinAPI symbol (BTC-USDT defaults to COINBASE_SPOT_BTC_USDT)",
    )
    parser.add_argument(
        "--date",
        help="UTC YYYY-MM-DD for time-only input without a D-YYYYMMDD filename",
    )
    parser.add_argument(
        "--duration",
        type=float,
        help="Historical seconds; omit to replay the complete file",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0, help="Simulation/wall-clock rate in (0,1]"
    )
    parser.add_argument("--no-controller", action="store_true")
    parser.add_argument("--controller-order-quantity", default="0.1")
    parser.add_argument("--controller-total-quantity", default="100")
    parser.add_argument(
        "--output", type=Path, help="New run directory; must not already exist"
    )
    parser.add_argument("--api-port", type=int)
    parser.add_argument("--fix-port", type=int, default=4001)
    parser.add_argument("--md-port", type=int)
    parser.add_argument("--glimpse-port", type=int)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-pending", type=int, default=4096)
    parser.add_argument("--max-lag-seconds", type=float, default=1.0)
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Stream every source/request/report to JSONL",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Run the small FIX execution qualification client",
    )
    parser.add_argument(
        "--native-client",
        type=Path,
        help="Existing C++ feed-client executable for ITCH/GLIMPSE qualification",
    )
    return parser.parse_args(argv)


async def wait_ready(base_url, process, timeout, *, expected_exchange):
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(base_url=base_url, timeout=1) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Exchange exited during startup ({process.returncode}); see exchange.log"
                )
            try:
                response = await client.get("/readyz")
                if response.status_code == 200:
                    identity = await client.get("/")
                    if (
                        identity.status_code != 200
                        or identity.json().get("exchange") != expected_exchange
                    ):
                        raise RuntimeError(
                            "Configured HTTP port belongs to a different exchange; no session was started"
                        )
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"Exchange exited during startup ({process.returncode}); see exchange.log"
                        )
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise TimeoutError("Exchange readiness timed out; see exchange.log")


async def launch(args):
    with replay_timer_resolution() as timer_status:
        return await _launch(args, timer_status)


async def _launch(args, timer_status):
    launch_started = time.monotonic()
    source_symbol = args.source_symbol
    if source_symbol is None:
        if args.symbol != "BTC-USDT":
            raise ValueError("Custom --symbol requires an explicit --source-symbol")
        source_symbol = "COINBASE_SPOT_BTC_USDT"
    if not source_symbol.strip():
        raise ValueError("--source-symbol must not be empty")
    options = ReplayOptions(
        rate=args.speed,
        duration=args.duration,
        timeout=args.timeout,
        max_pending=args.max_pending,
        max_lag_seconds=args.max_lag_seconds,
        controller=not args.no_controller,
        probe=args.probe,
        controller_order_quantity=args.controller_order_quantity,
        controller_total_quantity=args.controller_total_quantity,
    )
    for port in (args.fix_port, args.api_port, args.md_port, args.glimpse_port):
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Ports must be between 1 and 65535")
    source_path = args.input.resolve(strict=True)
    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    raw["scheduler"].update(mode="realtime", rate=options.rate)
    if args.api_port:
        raw["admin_api"]["port"] = args.api_port
    if args.md_port:
        raw["moldudp64"]["multicast_port"] = args.md_port
    if args.glimpse_port:
        raw["soupbintcp"]["port"] = args.glimpse_port
    if raw["admin_api"]["host"] != "127.0.0.1":
        raise ValueError("The owned local launcher requires admin_api.host=127.0.0.1")
    run_id = (
        datetime.now(timezone.utc).strftime("coinapi-%Y%m%dT%H%M%S-")
        + uuid.uuid4().hex[:8]
    )
    raw["exchange_name"] = f"MockX-CoinAPI-{run_id}"
    output = (args.output or ROOT / "replay-runs" / run_id).resolve()
    output.mkdir(parents=True, exist_ok=False)
    config_path = output / "exchange.yml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = load_config(config_path, environ={})
    if set(config.instruments) != {args.symbol}:
        raise ValueError(
            "Replay requires exactly the selected instrument in configuration"
        )
    instrument = Instrument.from_config(args.symbol, config.instruments[args.symbol])
    initiator_roles = ["replay_buy", "replay_sell"]
    if options.controller:
        initiator_roles.append("controller")
    if options.probe:
        initiator_roles.append("strategy_bot")
    acceptor_path, client_path = output / "acceptor.cfg", output / "client.cfg"
    acceptor_path.write_text(
        fix_settings(
            output=output,
            role_names=ALL_ROLES,
            initiator=False,
            port=args.fix_port,
            store_name="acceptor",
        ),
        encoding="utf-8",
    )
    client_path.write_text(
        fix_settings(
            output=output,
            role_names=initiator_roles,
            initiator=True,
            port=args.fix_port,
            store_name="client",
        ),
        encoding="utf-8",
    )
    (output / "strategy.cfg").write_text(
        fix_settings(
            output=output,
            role_names=["strategy_bot"],
            initiator=True,
            port=args.fix_port,
            store_name="strategy",
        ),
        encoding="utf-8",
    )
    base_url = f"http://127.0.0.1:{config.admin_api.port}"
    result = {
        "status": "starting",
        "run_id": run_id,
        "input": str(source_path),
        "source_symbol": source_symbol,
        "options": asdict_options(options),
        "runtime_gil_switch_interval_seconds": sys.getswitchinterval(),
        **timer_status,
    }
    hash_started = time.monotonic()
    with source_path.open("rb") as stream:
        result["input_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    result["input_hash_seconds"] = time.monotonic() - hash_started
    result["input_bytes"] = source_path.stat().st_size
    process = runner = None
    runner_start_offset = None
    audit = None
    log = (output / "exchange.log").open("w", encoding="utf-8")
    try:
        source_started = time.monotonic()
        with open_source(
            source_path, instrument, date=args.date, source_symbol=source_symbol
        ) as source:
            result["source_snapshot_seconds"] = time.monotonic() - source_started
            server_command = [
                sys.executable,
                "-m",
                "examples.coinapi_replay.server",
                "--config",
                str(config_path),
                "--fix-config",
                str(acceptor_path),
            ]
            child_started = time.monotonic()
            process = subprocess.Popen(
                server_command,
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            print(f"Run directory: {output}", flush=True)
            await wait_ready(
                base_url,
                process,
                options.timeout,
                expected_exchange=config.exchange_name,
            )
            result["child_ready_seconds"] = time.monotonic() - child_started
            if args.audit:
                audit = (output / "events.jsonl").open(
                    "w", encoding="utf-8", buffering=65536
                )
            native_probe = (
                NativeProbe(
                    config,
                    instrument,
                    executable=args.native_client,
                    timeout=options.timeout,
                )
                if args.native_client
                else None
            )
            memory_samplers = {
                "exchange": ProcessMemorySampler(process.pid),
                "runner": ProcessMemorySampler(os.getpid()),
            }
            runner_constructed = time.monotonic()
            runner = ReplayRunner(
                source,
                instrument,
                base_url,
                client_path,
                run_id,
                options,
                audit=audit,
                native_probe=native_probe,
                memory_samplers=memory_samplers,
            )
            result["runner_construction_seconds"] = (
                time.monotonic() - runner_constructed
            )
            runner_start_offset = time.monotonic() - launch_started
            result.update(await runner.run())
    except BaseException as exc:
        if runner is not None:
            result.update(runner.result)
        if result.get("status") not in {"failed", "interrupted"}:
            result.update(
                status=(
                    "interrupted"
                    if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
                    else "failed"
                ),
                error=f"{type(exc).__name__}: {exc}",
            )
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        cleanup_errors = []
        if audit is not None:
            try:
                audit.close()
            except OSError as exc:
                cleanup_errors.append(f"Audit close: {exc}")
        if process is not None and process.poll() is None:
            try:
                process.stdin.write(b"stop\n")
                process.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                cleanup_errors.append("Exchange did not stop within five seconds")
                process.terminate()
                await asyncio.to_thread(process.wait)
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            result["exchange_exit_code"] = process.returncode
            if process.returncode and result["status"] == "completed":
                result.update(
                    status="failed", cleanup_error="Exchange exited unsuccessfully"
                )
        if cleanup_errors:
            result["status"] = "failed"
            result["cleanup_error"] = "; ".join(
                ([result["cleanup_error"]] if result.get("cleanup_error") else [])
                + cleanup_errors
            )
        log.close()
        if runner_start_offset is not None and "first_book_seconds" in result:
            result["command_to_first_book_seconds"] = (
                runner_start_offset + result["first_book_seconds"]
            )
        result["command_elapsed_seconds"] = time.monotonic() - launch_started
        (output / "result.json").write_text(
            json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
        )
        print(f"Replay {result['status']}: {output / 'result.json'}", flush=True)
    return 0 if result["status"] == "completed" else 1


def asdict_options(options):
    from dataclasses import asdict

    return asdict(options)


def main(argv=None):
    args = parse_args(argv)

    def stop(*_):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        return asyncio.run(launch(args))
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError) as exc:
        print(f"Invalid replay configuration: {exc}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())

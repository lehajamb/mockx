"""Real FIX sockets, owned exchange lifecycle, and public feed verification."""

import asyncio
import json
import socket

import pytest
import yaml

pytest.importorskip("quickfix")

from examples.coinapi_replay.__main__ import ROOT, launch, parse_args


def _ports():
    # Reserve the actual protocols together so selected ports differ and are free.
    sockets = []
    try:
        for kind in (
            socket.SOCK_STREAM,
            socket.SOCK_STREAM,
            socket.SOCK_DGRAM,
            socket.SOCK_STREAM,
        ):
            while True:
                probe = socket.socket(socket.AF_INET, kind)
                probe.bind(("127.0.0.1", 0))
                if probe.getsockname()[1] not in {
                    item.getsockname()[1] for item in sockets
                }:
                    sockets.append(probe)
                    break
                probe.close()
        return [probe.getsockname()[1] for probe in sockets]
    finally:
        for probe in sockets:
            probe.close()


def _capture(path):
    rows = []

    def row(seconds, action, buy, price, quantity, identity):
        stamp = f"2026-07-10T00:00:{seconds}Z"
        rows.append(
            ";".join([stamp, stamp, action, str(buy), price, quantity, identity])
        )

    # The first probe consumes the tiny best ask. This gives the controller a
    # real midpoint error to correct through its independent FIX participant.
    for buy, price, quantity, identity in [
        (1, "99", ".001", "b1"),
        (1, "98", "1", "b2"),
        (1, "97", "1", "b3"),
        (0, "101", ".0001", "a1"),
        (0, "102", "1", "a2"),
        (0, "103", "1", "a3"),
    ]:
        row("00.000000000", "SNAPSHOT", buy, price, quantity, identity)
    for values in [
        ("00.350000000", "0", 0, "100.5", ".01", "c"),
        ("00.500000000", "2", 0, "100.5", ".015", "c"),
        ("00.650000000", "1", 0, "100.5", ".005", "c"),
        ("00.800000000", "4", 0, "100.5", ".004", "c"),
        ("00.950000000", "3", 0, "100.5", "0", "c"),
        ("01.100000000", "0", 0, "100.75", ".01", "c"),
        ("01.250000000", "4", 0, "100.75", ".01", "c"),
        ("01.500000000", "0", 1, "98", ".005", "d"),
        ("01.900000000", "3", 1, "98", "0", "d"),
    ]:
        row(*values)
    path.write_text(
        "time_exchange;time_coinapi;update_type;is_buy;entry_px;entry_sx;order_id\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "controller", [False, True], ids=["historical-and-strategy", "with-controller"]
)
async def test_real_socket_replay_completes_all_actions_and_reconciles_feed(
    tmp_path, controller
):
    source = (
        tmp_path / "capture_D-2026071000_SC-COINBASE_SPOT_BTC_USDT_S-BTC-USDT.itch.csv"
    )
    _capture(source)
    config = yaml.safe_load(
        (ROOT / "examples" / "coinapi_exchange.yml").read_text(encoding="utf-8")
    )
    config["questdb"] = {"enabled": False}
    config["latency"] = {"inbound_ms": 1, "outbound_ms": 2, "engine_ms": 2}
    config_path = tmp_path / "source-config.yml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    api, fix, md, glimpse = _ports()
    output = tmp_path / "run"
    arguments = [
        "--input",
        str(source),
        "--config",
        str(config_path),
        "--output",
        str(output),
        "--api-port",
        str(api),
        "--fix-port",
        str(fix),
        "--md-port",
        str(md),
        "--glimpse-port",
        str(glimpse),
        "--probe",
        "--max-lag-seconds",
        "5",
    ]
    if not controller:
        arguments.append("--no-controller")
    code = await asyncio.wait_for(launch(parse_args(arguments)), timeout=45)
    result = json.loads((output / "result.json").read_text())
    assert code == 0, result
    assert result["status"] == "completed", result
    assert result["seed_orders"] == 6
    assert result["processed_events"] == 9
    assert result["source_operations"] == {
        "ADD": 3,
        "SET": 1,
        "SUB": 1,
        "MATCH": 2,
        "DELETE": 2,
    }
    assert result["reports_by_actor"]["strategy_bot"] >= 6
    assert result["participating_fill_lots_by_actor"]["strategy_bot"] > 0
    assert result["reports_by_actor"]["replay_buy"] > 0
    assert result["reports_by_actor"]["replay_sell"] > 0
    assert result["final_feed_verified"] is True
    assert result["final_book_sequence"] > 0
    assert result["session_end"] == "session_ended"
    assert result["run_generation"] == 1
    assert result["exchange_exit_code"] == 0
    assert result["command_to_first_book_seconds"] >= result["first_book_seconds"]
    assert set(result["memory"]) == {"exchange", "runner"}
    for memory in result["memory"].values():
        assert memory["samples"] + memory["failed_samples"] > 0
        if memory["failed_samples"]:
            assert memory["error"]
    assert "cleanup_error" not in result
    if controller:
        assert result["controller_requested_lots"] > 0
        assert result["reports_by_actor"]["controller"] > 0
    else:
        assert result["controller_requested_lots"] == 0


@pytest.mark.asyncio
async def test_duration_keeps_seed_executable_until_cutoff_before_first_increment(
    tmp_path,
):
    source = (
        tmp_path / "capture_D-2026071000_SC-COINBASE_SPOT_BTC_USDT_S-BTC-USDT.itch.csv"
    )
    _capture(source)
    lines = source.read_text().splitlines()
    future = lines[7].replace("00:00:00.350000000", "00:00:10.000000000")
    source.write_text("\n".join(lines[:7] + [future]) + "\n")
    config = yaml.safe_load((ROOT / "examples" / "coinapi_exchange.yml").read_text())
    config["questdb"] = {"enabled": False}
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(config))
    api, fix, md, glimpse = _ports()
    output = tmp_path / "run"
    args = parse_args(
        [
            "--input",
            str(source),
            "--config",
            str(config_path),
            "--output",
            str(output),
            "--api-port",
            str(api),
            "--fix-port",
            str(fix),
            "--md-port",
            str(md),
            "--glimpse-port",
            str(glimpse),
            "--no-controller",
            "--probe",
            "--duration",
            ".25",
            "--max-lag-seconds",
            "5",
        ]
    )
    assert await asyncio.wait_for(launch(args), timeout=45) == 0
    result = json.loads((output / "result.json").read_text())
    assert result["status"] == "completed"
    assert result["processed_events"] == 0
    assert result["source_operations"] == {}
    assert result["playback_seconds"] >= 0.25
    assert result["reports_by_actor"]["strategy_bot"] >= 3
    assert result["final_feed_verified"]
    assert result["session_end"] == "session_ended"

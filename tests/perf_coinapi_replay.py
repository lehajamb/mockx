"""Manual real-socket replay qualification; intentionally outside pytest discovery.

Run from the repository root. Input captures and generated logs stay local.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def failures(result):
    errors = []
    if result.get("status") != "completed":
        return [result.get("error", "Replay did not complete")]
    for metric, limit in (("first_book_seconds", 5), ("drain_seconds", 2)):
        if result.get(metric, float("inf")) > limit:
            errors.append(f"{metric} exceeds {limit}s: {result.get(metric)}")
    for metric in ("dispatch_lag", "scheduler_lag"):
        samples = result.get(metric, {})
        if not samples.get("count"):
            errors.append(f"{metric} has no samples")
        for key, limit in (("p99_upper_bound_ms", 20), ("max_ms", 250)):
            if samples.get(key, float("inf")) > limit:
                errors.append(f"{metric}.{key} exceeds {limit}ms: {samples.get(key)}")
    if not result.get("final_feed_verified") or not result.get("native_feed_verified"):
        errors.append("Final public/native feed verification is missing")
    native = result.get("native_feed", {})
    if (
        native.get("planned_drops") != 1
        or native.get("gaps") != 1
        or native.get("source_gaps") != 0
    ):
        errors.append("Native recovery did not observe exactly the planned gap")
    if not result.get("reports_by_actor", {}).get("strategy_bot"):
        errors.append("No real FIX strategy reports")
    if not result.get("participating_fill_lots_by_actor", {}).get("strategy_bot", 0):
        errors.append("No real FIX strategy fills")
    for actor in ("runner", "exchange"):
        memory = result.get("memory", {}).get(actor, {})
        if not memory.get("samples") or memory.get("failed_samples"):
            errors.append(f"No {actor} memory measurements")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--native-client", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument(
        "--full-hour",
        action="store_true",
        help="One full capture run with controller; no duration cutoff",
    )
    parser.add_argument("--api-port", type=int, default=8001)
    parser.add_argument("--fix-port", type=int, default=4001)
    parser.add_argument("--md-port", type=int, default=18000)
    parser.add_argument("--glimpse-port", type=int, default=18001)
    args = parser.parse_args()
    if args.repetitions < 1 or args.duration <= 0:
        parser.error("repetitions and duration must be positive")
    output = (
        args.output
        or ROOT
        / "replay-runs"
        / datetime.now(timezone.utc).strftime("qualification-%Y%m%dT%H%M%S")
    ).resolve()
    output.mkdir(parents=True, exist_ok=False)
    runs = (
        [(True, 1)]
        if args.full_hour
        else [
            (controller, index + 1)
            for controller in (False, True)
            for index in range(args.repetitions)
        ]
    )
    results = []
    for controller, index in runs:
        name = f"controller-{'on' if controller else 'off'}-{index}"
        folder = output / name
        command = [
            sys.executable,
            "-m",
            "examples.coinapi_replay",
            "--input",
            str(args.input.resolve()),
            "--native-client",
            str(args.native_client.resolve()),
            "--probe",
            "--output",
            str(folder),
            "--api-port",
            str(args.api_port),
            "--fix-port",
            str(args.fix_port),
            "--md-port",
            str(args.md_port),
            "--glimpse-port",
            str(args.glimpse_port),
        ]
        if not args.full_hour:
            command += ["--duration", str(args.duration)]
        if not controller:
            command.append("--no-controller")
        print(f"Running {name}; {folder}", flush=True)
        with (output / (name + ".log")).open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                **(
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if os.name == "nt"
                    else {}
                ),
            )
        path = folder / "result.json"
        result = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"status": "failed", "error": "No result.json"}
        )
        errors = failures(result)
        if completed.returncode:
            errors.append(f"Replay process exit code {completed.returncode}")
        if args.full_hour and result.get("playback_seconds", 0) < 3590:
            errors.append(
                "Full-hour qualification requires at least 3,590 seconds of playback"
            )
        results.append(
            {
                "run": name,
                "result": str(path),
                "passed": not errors,
                "failures": errors,
                **{
                    key: result.get(key)
                    for key in (
                        "input_sha256",
                        "processed_events",
                        "command_to_first_book_seconds",
                        "drain_seconds",
                        "dispatch_lag",
                        "scheduler_lag",
                        "frame_lag",
                        "memory",
                    )
                },
            }
        )
        (output / "qualification.json").write_text(
            json.dumps(results, indent=2) + "\n", encoding="utf-8"
        )
        print(f"{name}: {'PASS' if not errors else 'FAIL'} {errors}", flush=True)
        if errors:
            return 1
    print(f"Qualification passed: {output / 'qualification.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

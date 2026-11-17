#!/usr/bin/env python3
"""Run fresh hardware checks in scratch space; exit 0 only for a qualified fabrication candidate."""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from kicad_environment import kicad_environment
from verification_core import (apply_waivers, digest, engineering_failures, mechanical_failures,
                               parse_drc, parse_erc, run_tool, scratch_project, source_hashes,
                               source_identity, utc_now)

ROOT = Path(__file__).resolve().parents[1]


def verify(root=ROOT, run_id=None, cadquery_python="/opt/cq/bin/python"):
    root = Path(root).resolve()
    tool_env = kicad_environment()
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    if Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("run-id must be a single directory name")
    run_dir = root / "validation" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    inputs = source_hashes(root)
    result = {"schema_version": 1, "run_id": run_id, "started_utc": utc_now(),
              "source_hashes": inputs, "source_identity": source_identity(inputs),
              "commands": [], "checks": {}, "release_passed": False}
    errors, failures = [], []
    try:
        git = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
        result["git_revision"] = git.stdout.strip() if git.returncode == 0 else None
    except OSError:
        result["git_revision"] = None
    result["environment"] = {"python": sys.version, "container_image": os.environ.get("SORX_IMAGE_ID", "UNRECORDED"),
                             "KICAD7_FOOTPRINT_DIR": tool_env["KICAD7_FOOTPRINT_DIR"]}
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", result["environment"]["container_image"]):
        errors.append("Immutable tool image identity not recorded; set SORX_IMAGE_ID through the dedicated launcher")
    with tempfile.TemporaryDirectory(prefix="sorx-verify-") as temp:
        scratch = scratch_project(root, Path(temp) / "hardware")
        tools, hw = scratch / "tools", scratch / "hw"
        for relative, expected in inputs.items():
            copied = scratch / relative
            capture = copied if copied.is_file() else root / relative
            if digest(capture) != expected:
                raise RuntimeError(f"Input changed while snapshotting: {relative}")
            archived = run_dir / "source" / relative
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(capture, archived)

        def execute(name, command, outputs=(), timeout=600):
            record = run_tool(name, command, tools, run_dir, outputs, timeout, env=tool_env)
            result["commands"].append(record)
            if not record["success"]:
                errors.append(f"{name}: subprocess failed or required output missing")
            return record["success"]

        def inspect(name, function):
            try:
                data, reasons = function()
                result["checks"][name] = {"status": "FAIL" if reasons else "PASS", "details": data}
                failures.extend(f"{name}: {reason}" for reason in reasons)
            except Exception as error:
                errors.append(f"{name}: {type(error).__name__}: {error}")
                result["checks"][name] = {"status": "ERROR", "error": str(error)}

        version = run_tool("kicad-version", ["kicad-cli", "version"], tools, run_dir, env=tool_env)
        result["commands"].append(version)
        if not version["success"]:
            errors.append("KiCad version could not be recorded")
        erc_dir = Path(temp) / "erc"
        erc_dir.mkdir()
        erc_file = erc_dir / "sorx.erc"
        if execute("erc", ["xvfb-run", "-a", "eeschema_do", "run_erc", str(hw / "sorx.kicad_sch"), str(erc_dir)],
                   [erc_file]):
            def erc_result():
                data = parse_erc(erc_file.read_text(encoding="utf-8"))
                return data, [f"{data['errors']} errors, {data['warnings']} warnings"] if data["messages"] else []
            inspect("erc", erc_result)
        netlist = hw / "sorx.net"
        if execute("netlist-export", ["kicad-cli", "sch", "export", "netlist", "--format", "kicadsexpr",
                                     "-o", str(netlist), str(hw / "sorx.kicad_sch")], [netlist]):
            parity = Path(temp) / "netlist-parity.json"
            if execute("netlist-parity", [sys.executable, str(tools / "verification_worker.py"), "netlist",
                                          str(netlist), str(parity)], [parity]):
                def parity_result():
                    data = json.loads(parity.read_text(encoding="utf-8"))
                    return data, ["model and schematic disagree"] if data["mismatches"] or data["extra_multi_node_nets"] else []
                inspect("netlist-parity", parity_result)
        board_file, drc = Path(temp) / "board-check.json", Path(temp) / "drc-stored.txt"
        if execute("board-check", [sys.executable, str(tools / "verification_worker.py"), "board",
                                   str(hw / "sorx.kicad_pcb"), str(board_file), "--drc", str(drc)],
                   [board_file, drc, drc.with_name("drc-refilled.txt")]):
            def board_result():
                data = json.loads(board_file.read_text(encoding="utf-8"))
                reasons = list(data["model_mismatches"])
                if data["duplicate_via_sites"]:
                    reasons.append("Duplicate via/drill sites remain")
                return data, reasons
            inspect("board-model-and-drills", board_result)
            waiver_file = root / "validation/drc-waivers.json"
            waivers = json.loads(waiver_file.read_text(encoding="utf-8")) if waiver_file.exists() else {"schema_version": 1, "waivers": []}
            for name, path in (("drc-stored", drc), ("drc-refilled", drc.with_name("drc-refilled.txt"))):
                def drc_result(path=path):
                    data = parse_drc(path.read_text(encoding="utf-8"))
                    unwaived, waived = apply_waivers(data["findings"], waivers, root, inputs["hw/sorx.kicad_pcb"])
                    data.update(unwaived=len(unwaived), waived=len(waived))
                    return data, [f"{len(unwaived)} unwaived findings"] if unwaived else []
                inspect(name, drc_result)
        parts = hw / "build/mech_parts.json"
        if execute("mechanical-placement", [sys.executable, str(tools / "mech_export.py"),
                                            str(hw / "sorx.kicad_pcb"), str(parts)], [parts]):
            fit = scratch / "mech/fit-check.json"
            fit_ok = execute("mechanical-fit", [cadquery_python, str(tools / "gen_mech.py")], [fit], timeout=900)
            # Retain and parse a failed check's output for diagnostics, but its process failure still blocks release.
            if fit.exists():
                shutil.copytree(scratch / "mech", run_dir / "mechanical")
                def fit_result():
                    data = json.loads(fit.read_text(encoding="utf-8"))
                    reasons = mechanical_failures(data, inputs["hw/sorx.kicad_pcb"], digest(parts))
                    command = result["commands"][-1]
                    valid_failure_report = (data.get("schema_version") == 2 and data.get("success") is False
                                            and data.get("geometry_errors") == []
                                            and data.get("source_pcb_sha256") == inputs["hw/sorx.kicad_pcb"]
                                            and data.get("model_inputs_sha256", {}).get("mech_parts.json") == digest(parts))
                    if command["returncode"] == 1 and valid_failure_report:
                        message = "mechanical-fit: subprocess failed or required output missing"
                        if message in errors:
                            errors.remove(message)
                        command["classification"] = "DESIGN_FAIL: fresh mechanical report contains unmet requirements"
                    return data, reasons
                inspect("mechanical", fit_result)
    if source_hashes(root) != inputs:
        errors.append("Source inputs changed during verification; results cannot qualify this revision")
    gate_file = root / "validation/release-gates.json"
    try:
        gate_data = json.loads(gate_file.read_text(encoding="utf-8")) if gate_file.exists() else {}
        gate_failures = engineering_failures(gate_data, inputs, root)
    except Exception as error:
        gate_failures = [f"Invalid engineering gate review: {error}"]
    result["automatic_checks_passed"] = not errors and not failures
    result["engineering_gate_failures"] = gate_failures
    result["failures"], result["execution_errors"] = failures, errors
    result["release_passed"] = not errors and not failures and not gate_failures
    result["release_status"] = "FABRICATION CANDIDATE — PHYSICAL BRING-UP PENDING" if result["release_passed"] else "NOT READY FOR FABRICATION"
    result["finished_utc"] = utc_now()
    result["evidence_hashes"] = {str(p.relative_to(root)).replace("\\", "/"): digest(p)
                                  for p in sorted(run_dir.rglob("*")) if p.is_file()}
    report = run_dir / "verification.json"
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    (root / "validation/latest.json").write_text(json.dumps({"report": str(report.relative_to(root)).replace("\\", "/"),
                                                            "sha256": digest(report)}, indent=2), encoding="utf-8")
    lines = ["# Independent hardware verification", "", "**" + result["release_status"] + "**", "",
             f"Run: {run_id}. Source identity: `{result['source_identity']}`.", "",
             "Raw commands, exit statuses, input hashes and evidence are in " + str(report.relative_to(root)) + ".", "",
             "## Executed checks", "", "| Check | Status |", "|---|---|"]
    lines.extend(f"| {name} | {check['status']} |" for name, check in result["checks"].items())
    lines.extend(["", "## Remaining failures", ""])
    lines.extend("- " + x for x in errors + failures + gate_failures)
    lines.extend(["", "These software/CAD checks do not establish physical boot, thermal performance, EMC or wire-to-wire latency.", ""])
    (root / "docs/verification-report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"run": run_id, "release_status": result["release_status"],
                      "execution_errors": len(errors), "design_failures": len(failures),
                      "engineering_gate_failures": len(gate_failures)}, indent=2))
    return 2 if errors else 1 if failures or gate_failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run-id")
    parser.add_argument("--cadquery-python", default="/opt/cq/bin/python")
    args = parser.parse_args()
    try:
        return verify(args.root, args.run_id, args.cadquery_python)
    except Exception as error:
        print(f"Verification execution failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

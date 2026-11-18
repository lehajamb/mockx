"""Fail-closed, standard-library support for hardware checks and export."""
from __future__ import annotations
import hashlib
import json
import math
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

GATE_IDS = tuple(f"R{i:02}" for i in range(1, 10))

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def source_hashes(root):
    """Hash source inputs, not reports, generated netlists, historical checkpoints or exports."""
    root = Path(root)
    candidates = set()
    for folder, patterns in (("hw", ("*.kicad_pcb", "*.kicad_pro", "*.kicad_sch", "*.kicad_sym", "*.kicad_dru", "*-lib-table")),
                             ("tools", ("*.py", "*.json")), ("bom", ("*.csv", "*.json"))):
        for pattern in patterns:
            candidates.update((root / folder).glob(pattern))
    candidates.update((root / "hw/sorx.pretty").glob("*.kicad_mod"))
    candidates.update((root / "mech/vendor").rglob("*"))
    candidates.update(p for p in (root / "tools/env").glob("*")
                      if p.suffix in (".py", ".sh", ".ps1", ".json", ".txt", ".lock") or p.name.startswith("Dockerfile"))
    for name in ("validation/drc-waivers.json",):
        if (root / name).is_file():
            candidates.add(root / name)
    return {str(p.relative_to(root)).replace("\\", "/"): digest(p) for p in sorted(candidates) if p.is_file()}

def source_identity(hashes):
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def scratch_project(root, dest):
    """Copy only active inputs. No archived reports/checkpoints can masquerade as fresh outputs."""
    root, dest = Path(root), Path(dest)
    (dest / "hw/build").mkdir(parents=True)
    (dest / "mech").mkdir()
    if (root / "mech/vendor").is_dir():
        shutil.copytree(root / "mech/vendor", dest / "mech/vendor")
    (dest / "docs/reports").mkdir(parents=True)
    shutil.copytree(root / "tools", dest / "tools", ignore=shutil.ignore_patterns("__pycache__", "env", "tests"))
    shutil.copytree(root / "bom", dest / "bom")
    for p in (root / "hw").iterdir():
        if p.is_file() and (p.suffix.startswith(".kicad_") and p.suffix != ".kicad_prl" or p.name.endswith("-lib-table")):
            shutil.copy2(p, dest / "hw" / p.name)
    shutil.copytree(root / "hw/sorx.pretty", dest / "hw/sorx.pretty")
    return dest

def run_tool(name, command, cwd, run_dir, outputs=(), timeout=600, env=None):
    """Delete declared scratch outputs, capture status, and never accept output after a failure."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    outputs = [Path(p) for p in outputs]
    for p in outputs:
        if p.exists():
            if not p.is_file():
                raise ValueError(f"Expected a file output: {p}")
            p.unlink()
        p.parent.mkdir(parents=True, exist_ok=True)
    record = {"name": name, "command": [str(x) for x in command], "started_utc": utc_now()}
    if env is not None:
        record["environment"] = {key: env[key] for key in ("KICAD7_FOOTPRINT_DIR",) if key in env}
    start = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        record.update(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
    except (OSError, subprocess.TimeoutExpired) as error:
        record.update(returncode=None, stdout="", stderr=str(error))
    record["elapsed_seconds"] = round(time.monotonic() - start, 3)
    record["finished_utc"] = utc_now()
    record["missing_outputs"] = [str(p) for p in outputs if not p.is_file() or p.stat().st_size == 0]
    record["success"] = record["returncode"] == 0 and not record["missing_outputs"]
    record["outputs"] = {}
    for p in outputs:
        if p.is_file():
            target = run_dir / p.name
            if target.resolve() != p.resolve():
                shutil.copy2(p, target)
            record["outputs"][target.name] = digest(target)
    (run_dir / f"{name}.log").write_text(record.pop("stdout") + "\n--- stderr ---\n" + record.pop("stderr"), encoding="utf-8")
    return record

def parse_erc(text):
    match = re.search(r"ERC messages:\s*(\d+)\s+Errors\s*(\d+)\s+Warnings\s*(\d+)", text)
    if not match:
        raise ValueError("Missing ERC summary")
    messages, errors, warnings = map(int, match.groups())
    if messages != errors + warnings:
        raise ValueError("Inconsistent ERC summary")
    return {"messages": messages, "errors": errors, "warnings": warnings}

def parse_drc(text):
    if "** End of Report **" not in text:
        raise ValueError("Incomplete DRC report")
    counts = {}
    records = []
    for section in re.split(r"\n(?=\*\* Found )", text):
        head = re.search(r"\*\* Found (\d+) (DRC violations|unconnected pads|Footprint errors) \*\*", section)
        if not head:
            continue
        expected, kind = int(head[1]), head[2]
        found = []
        for match in re.finditer(r"^\[(\w+)\]: ([^\n]*)\n((?:[ \t]+[^\n]*(?:\n|$))+)", section, re.M):
            body = match[3].strip()
            canonical = "\n".join((match[1], match[2], body))
            found.append({"category": match[1], "message": match[2], "objects": body,
                          "fingerprint": hashlib.sha256(canonical.encode()).hexdigest(),
                          "section": kind})
        if len(found) != expected:
            raise ValueError(f"DRC parse mismatch for {kind}: {len(found)} parsed, {expected} reported")
        counts[kind] = expected
        records.extend(found)
    if set(counts) != {"DRC violations", "unconnected pads", "Footprint errors"}:
        raise ValueError("Missing DRC sections")
    return {"counts": counts, "findings": records}

def apply_waivers(findings, document, root, pcb_hash):
    """Waivers are exact finding + PCB matches; required opens and duplicate holes cannot be waived."""
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("Invalid waiver document")
    approved = {}
    for item in document.get("waivers", []):
        required = ("fingerprint", "pcb_sha256", "reason", "reviewer", "evidence_path", "evidence_sha256")
        if not all(isinstance(item.get(k), str) and item[k].strip() for k in required):
            raise ValueError("Waiver lacks exact identity or reviewed evidence")
        evidence = (Path(root) / item["evidence_path"]).resolve()
        if not evidence.is_relative_to(Path(root).resolve()) or not evidence.is_file():
            raise ValueError("Invalid waiver evidence path")
        if digest(evidence) != item["evidence_sha256"]:
            raise ValueError("Waiver evidence changed")
        if item["pcb_sha256"] != pcb_hash:
            raise ValueError("Waiver refers to a different PCB")
        if item["fingerprint"] in approved:
            raise ValueError("Duplicate waiver")
        approved[item["fingerprint"]] = item
    unwaived, waived = [], []
    for finding in findings:
        key = finding["fingerprint"]
        prohibited = finding["section"] == "unconnected pads" or finding["category"] in ("holes_co_located", "shorting_items")
        (waived if key in approved and not prohibited else unwaived).append(finding)
        if key in approved and prohibited:
            raise ValueError("Required connectivity or duplicate-hole findings cannot be waived")
    unused = set(approved) - {f["fingerprint"] for f in findings}
    if unused:
        raise ValueError(f"Stale/unmatched waivers: {sorted(unused)}")
    return unwaived, waived

def mechanical_failures(data, pcb_hash, parts_hash):
    failures = []
    if data.get("schema_version") != 2:
        failures.append("missing mechanical schema version 2")
    if data.get("source_binding_valid") is not True:
        failures.append("mechanical source metadata not validated")
    if data.get("source_pcb_sha256") != pcb_hash:
        failures.append("mechanical PCB hash mismatch")
    if data.get("model_inputs_sha256", {}).get("mech_parts.json") != parts_hash:
        failures.append("mechanical placement input hash mismatch")
    if data.get("success") is not True:
        failures.append("mechanical checker did not pass")
    if not isinstance(data.get("geometry_errors"), list) or data["geometry_errors"]:
        failures.append("geometry errors or missing geometry status")
    volume = data.get("max_interference_mm3")
    if not isinstance(volume, (int, float)) or not math.isfinite(volume) or volume < 0 or volume > 0.001:
        failures.append("invalid or excessive interference")
    if data.get("all_ports_aligned") is not True:
        failures.append("ports not qualified")
    checks = data.get("clearance_checks")
    if not isinstance(checks, dict) or not checks:
        failures.append("clearances not qualified")
    else:
        for name, check in checks.items():
            actual, required = check.get("actual_mm"), check.get("required_mm")
            numbers = all(isinstance(v, (int, float)) and math.isfinite(v) for v in (actual, required))
            if check.get("ok") is not True or not numbers or actual < required:
                failures.append(f"clearance not qualified: {name}")
    if not isinstance(data.get("blockers"), list):
        failures.append("missing mechanical qualification status")
    elif data["blockers"]:
        failures.extend(str(x) for x in data["blockers"])
    return failures

def engineering_failures(document, hashes, root):
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        return ["Missing engineering gate review"]
    failures = []
    if document.get("source_identity") != source_identity(hashes):
        failures.append("Engineering review is not bound to current source inputs")
    gates = document.get("gates", {})
    for name in GATE_IDS:
        gate = gates.get(name, {})
        if gate.get("status") != "PASS":
            failures.append(f"{name}: {gate.get('status', 'MISSING')} — {gate.get('reason', 'No closure evidence')}")
            continue
        if not gate.get("reviewer") or not gate.get("evidence"):
            failures.append(f"{name}: missing reviewer/evidence")
        for item in gate.get("evidence", []):
            p = (Path(root) / item.get("path", "")).resolve()
            if not p.is_relative_to(Path(root).resolve()) or not p.is_file() or digest(p) != item.get("sha256"):
                failures.append(f"{name}: invalid evidence {item.get('path')}")
    return failures

def validate_release_report(report, root):
    current = source_hashes(root)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", report.get("environment", {}).get("container_image", "")):
        raise ValueError("Release verification lacks immutable container identity")
    if report.get("release_passed") is not True or report.get("source_hashes") != current:
        raise ValueError("No passing release verification for the current source inputs")
    manifest = report.get("evidence_hashes", {})
    if not manifest:
        raise ValueError("Release report has no raw evidence manifest")
    for relative, expected in manifest.items():
        path = (Path(root) / relative).resolve()
        if not path.is_relative_to(Path(root).resolve()) or not path.is_file() or digest(path) != expected:
            raise ValueError(f"Release evidence changed or missing: {relative}")
    review = Path(root) / "validation/release-gates.json"
    failures = engineering_failures(json.loads(review.read_text(encoding="utf-8")), current, root)
    if failures:
        raise ValueError("; ".join(failures))
    return current

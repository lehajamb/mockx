"""Print a compact, read-only queue from an input-bound KiCad DRC report."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hardware/tools"))
from verification_core import parse_drc

LEDGER = "docs/routing-ledger.json"
PCB = "hardware/hw/sorx.kicad_pcb"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_hashes(root):
    """Bind stored DRC to native copper, local rules, libraries and the checker image."""
    board_dir = root / "hardware/hw"
    files = [root / PCB, board_dir / "sorx.kicad_pro", board_dir / "fp-lib-table",
             root / "hardware/tools/env/image-id.txt"]
    files += list(board_dir.glob("*.kicad_dru"))
    files += list((board_dir / "sorx.pretty").glob("*.kicad_mod"))
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted(files)}


def project_path(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Evidence outside appliance: {name}")
    return path


def summarize(root, net=None):
    ledger = json.loads((root / LEDGER).read_text(encoding="utf-8"))
    expected = ledger["inputs"]
    actual = input_hashes(root)
    if expected != actual:
        changed = sorted(k for k in expected.keys() | actual.keys() if expected.get(k) != actual.get(k))
        raise ValueError("Stale routing queue; changed inputs: " + ", ".join(changed))
    report = project_path(root, ledger["report"])
    raw = report.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ledger["report_sha256"]:
        raise ValueError("Refilled report differs from the checked-in evidence binding")
    drc = parse_drc(raw.decode("utf-8").replace("\r\n", "\n"))
    grouped = {}
    for f in drc["findings"]:
        if f["section"] == "unconnected pads":
            names = set(re.findall(r"\[([^\[\]\n]+)\]", f["objects"]))
            if len(names) != 1:
                raise ValueError("Open has no unique net: " + f["objects"])
            grouped.setdefault(names.pop(), []).append(f)
    counts = drc["counts"]
    ground = len(grouped.get("GND", []))
    total = counts["unconnected pads"]
    other = counts["DRC violations"] + counts["Footprint errors"]
    lines = ["SORX | NOT READY FOR FABRICATION | saved refilled DRC, inputs match",
             f"PCB SHA-256: {actual[PCB]}",
             f"Required opens: {total} ({ground} GND, {total - ground} other); other findings: {other}."]
    if net is None:
        queue = ", ".join(f"{k} ({len(v)})" for k, v in sorted(grouped.items()))
        lines.extend(textwrap.wrap("Queue: " + (queue or "empty"), 92, subsequent_indent="       "))
        lines.append("Inspect one net: rerun with --net 'NAME'; act only after fresh native checks.")
    else:
        # KiCad can report a zone or track instead of the pad on its island.
        opens = sorted(grouped.get(net, []), key=lambda f: "Pad " not in f["objects"])
        if not opens:
            lines.append(f"{net}: no required opens listed.")
        for f in opens[:3]:
            ends = [s.split("): ", 1)[-1] for s in f["objects"].splitlines() if "@(" in s]
            lines.append(f"  {f['fingerprint'][:12]} | " + " <-> ".join(ends))
        if len(opens) > 3:
            lines.append(f"  ... {len(opens) - 3} more; see raw report.")
    decisions = ledger["decisions"]
    if net is not None:
        decisions = [d for d in decisions if net in d["nets"]]
    for d in decisions[-2:]:
        evidence = d["evidence"]
        if not project_path(root, evidence).is_file():
            raise ValueError("Decision evidence missing: " + evidence)
        lines += [f"{d['result']} {d['id']}: {d['lesson']}", "  " + evidence]
    lines += ["Raw: " + ledger["report"], "Method: docs/routing-runbook.md"]
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--net", help="Show up to three open items and relevant prior decisions")
    group.add_argument("--inputs", action="store_true", help="Print current input hashes only; do not bless a report")
    args = parser.parse_args(argv)
    try:
        if args.inputs:
            print(json.dumps(input_hashes(ROOT), indent=2))
        else:
            print("\n".join(summarize(ROOT, args.net)))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"Routing status unavailable: {exc}. Recheck the native board; see docs/routing-runbook.md.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

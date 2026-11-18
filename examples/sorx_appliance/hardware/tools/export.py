#!/usr/bin/env python3
"""Export into a new isolated directory; default release requires current passing verification."""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from kicad_environment import kicad_environment
from verification_core import (digest, run_tool, source_hashes, source_identity,
                               utc_now, validate_release_report)

ROOT = Path(__file__).resolve().parents[1]


def load_report(root):
    pointer = json.loads((root / "validation/latest.json").read_text(encoding="utf-8"))
    path = (root / pointer["report"]).resolve()
    if not path.is_relative_to(root) or digest(path) != pointer["sha256"]:
        raise ValueError("Latest verification pointer/hash is invalid")
    return path, json.loads(path.read_text(encoding="utf-8"))


def export(root=ROOT, development=False):
    root = Path(root).resolve()
    tool_env = kicad_environment()
    inputs = source_hashes(root)
    report_path = report = None
    if development:
        try:
            report_path, report = load_report(root)
            if report.get("source_hashes") != inputs:
                report_path = report = None
        except (OSError, ValueError, KeyError):
            pass
    else:
        report_path, report = load_report(root)
        validate_release_report(report, root)
        if report["environment"]["container_image"] != os.environ.get("SORX_IMAGE_ID"):
            raise ValueError("Export must use the immutable tool image recorded by verification")
        if report["environment"].get("KICAD7_FOOTPRINT_DIR") != tool_env["KICAD7_FOOTPRINT_DIR"]:
            raise ValueError("Export must use the footprint environment recorded by verification")
    import pcbnew
    board = pcbnew.LoadBoard(str(root / "hw/sorx.kicad_pcb"))
    copper = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, board.GetCopperLayerCount() - 1)] + ["B.Cu"]
    run_id = ("development-" if development else "release-") + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    target = root / "validation/exports" / run_id
    target.mkdir(parents=True, exist_ok=False)
    package = target / "package"
    for name in ("gerbers", "drill", "assembly", "mechanical", "source"):
        (package / name).mkdir(parents=True, exist_ok=True)
    pcb, sch = root / "hw/sorx.kicad_pcb", root / "hw/sorx.kicad_sch"
    result = {"schema_version": 1, "started_utc": utc_now(), "source_hashes": inputs,
              "source_identity": source_identity(inputs), "development": development,
              "fabrication_approval": False, "commands": [], "complete": False,
              "environment": {"python": sys.version, "container_image": os.environ.get("SORX_IMAGE_ID", "UNRECORDED"),
                              "KICAD7_FOOTPRINT_DIR": tool_env["KICAD7_FOOTPRINT_DIR"]}}
    status = "NOT READY FOR FABRICATION — DEVELOPMENT EXPORT" if development else "FABRICATION CANDIDATE — PHYSICAL BRING-UP PENDING"
    (package / "STATUS.txt").write_text(status + "\n", encoding="utf-8")

    def run(name, arguments, expected=()):
        record = run_tool(name, ["kicad-cli"] + [str(x) for x in arguments], root / "hw",
                          target / "logs", expected, env=tool_env)
        result["commands"].append(record)
        if not record["success"]:
            raise RuntimeError(f"{name} failed; incomplete outputs retained, no archive created")

    try:
        run("kicad-version", ["version"])
        run("gerbers", ["pcb", "export", "gerbers", "-o", str(package / "gerbers") + "/",
                        "--layers", ",".join(copper + ["F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]),
                        "--subtract-soldermask", pcb])
        if len(list((package / "gerbers").glob("*"))) < len(copper) + 7:
            raise RuntimeError("Incomplete Gerber layer set")
        run("drill", ["pcb", "export", "drill", "-o", str(package / "drill") + "/",
                      "--format", "excellon", "--excellon-separate-th", "--excellon-units", "mm",
                      "--generate-map", "--map-format", "pdf", pcb])
        if not list((package / "drill").glob("*.drl")):
            raise RuntimeError("No drill files produced")
        output = package / "assembly/sorx-pos.csv"
        run("placement", ["pcb", "export", "pos", "-o", output, "--format", "csv", "--units", "mm",
                          "--side", "both", pcb], [output])
        for side in ("top", "bottom"):
            output = package / f"assembly/sorx-assembly-{side}.pdf"
            layers = "F.Fab,F.SilkS,Edge.Cuts" if side == "top" else "B.Fab,B.SilkS,Edge.Cuts"
            args = ["pcb", "export", "pdf", "-o", output, "--layers", layers, "--black-and-white"]
            if side == "bottom":
                args.append("--mirror")
            run("assembly-" + side, args + [pcb], [output])
        for layer in copper:
            output = package / f"sorx-copper-{layer.replace('.', '_')}.pdf"
            run("copper-" + layer, ["pcb", "export", "pdf", "-o", output, "--layers", layer + ",Edge.Cuts", pcb], [output])
        output = package / "mechanical/board-outline.dxf"
        run("outline", ["pcb", "export", "dxf", "-o", output, "--layers", "Edge.Cuts", "--output-units", "mm", pcb], [output])
        output = package / "mechanical/sorx-board.step"
        run("pcb-step", ["pcb", "export", "step", "-o", output, "--force", "--subst-models", pcb], [output])
        output = package / "sorx-schematic.pdf"
        run("schematic-pdf", ["sch", "export", "pdf", "-o", output, sch], [output])
        output = package / "sorx.net"
        run("schematic-netlist", ["sch", "export", "netlist", "-o", output, sch], [output])
        for relative in inputs:
            dest = package / "source" / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / relative, dest)
        if report_path is not None:
            shutil.copy2(report_path, package / "verification.json")
            shutil.copytree(report_path.parent, package / "verification-evidence")
            result["verification_evidence_mapping"] = {"package_path": "verification-evidence", "hardware_path": str(report_path.parent.relative_to(root)).replace("\\", "/")}
            mechanical = report_path.parent / "mechanical"
            if mechanical.is_dir():
                shutil.copytree(mechanical, package / "mechanical/assembly", dirs_exist_ok=True)
            else:
                result["mechanical_models"] = "Not available from a source-bound verification run"
                if not development:
                    raise RuntimeError("Release lacks source-bound mechanical models")
        else:
            result["mechanical_models"] = "Not included: no verification run bound to current source inputs"
        for review_name in ("release-gates.json", "drc-waivers.json"):
            review_path = root / "validation" / review_name
            if review_path.is_file():
                destination = package / "source/validation" / review_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(review_path, destination)
                review_doc = json.loads(review_path.read_text(encoding="utf-8"))
                refs = [entry for gate in review_doc.get("gates", {}).values() for entry in gate.get("evidence", [])]
                refs.extend({"path": w["evidence_path"], "sha256": w["evidence_sha256"]} for w in review_doc.get("waivers", []))
                for entry in refs:
                    evidence = (root / entry["path"]).resolve()
                    if not evidence.is_relative_to(root) or not evidence.is_file() or digest(evidence) != entry["sha256"]:
                        raise ValueError("Engineering/waiver evidence is stale or missing")
                    destination = package / "source" / evidence.relative_to(root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(evidence, destination)
        if source_hashes(root) != inputs:
            raise RuntimeError("Sources changed during export")
        if not development:
            validate_release_report(report, root)
        result.update(complete=True, fabrication_approval=not development, release_status=status)
        result["artifact_hashes"] = {str(p.relative_to(package)).replace("\\", "/"): digest(p)
                                    for p in sorted(package.rglob("*")) if p.is_file()}
        (package / "manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        archive = shutil.make_archive(str(target / ("sorx-DEVELOPMENT-NOT-FOR-FAB" if development else "sorx-fabrication-candidate")),
                                      "zip", package)
        result["archive"] = {"path": str(Path(archive).relative_to(root)).replace("\\", "/"), "sha256": digest(archive)}
        print(json.dumps({"output": str(target), "status": status, "archive": result["archive"]}, indent=2))
        returncode = 0
    except Exception as error:
        result.update(error=f"{type(error).__name__}: {error}", release_status="INCOMPLETE — NOT READY FOR FABRICATION")
        print(result["error"], file=sys.stderr)
        returncode = 1
    result["finished_utc"] = utc_now()
    (target / "export-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--development", action="store_true", help="Explicitly export non-release artifacts without fabrication approval")
    args = parser.parse_args()
    try:
        return export(args.root, args.development)
    except Exception as error:
        print(f"Export refused: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

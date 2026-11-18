"""Reproduce three original verifier defects without running baseline generators.
Exit 0 means the expected original defects were reproduced; this is not a release check.
"""
import ast
import contextlib
import io
import json
from pathlib import Path
import re
import types
import zipfile

BASELINE = Path(__file__).resolve().parents[2] / "baseline/sorx-hw-2026-11-16-final.zip"


def reproduce():
    with zipfile.ZipFile(BASELINE) as archive:
        verifier = ast.parse(archive.read("sorx-hw/tools/verify.py"))
        exporter = ast.parse(archive.read("sorx-hw/tools/export.py"))
    def assignment(tree, name):
        return next(n for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
    def function(tree, name):
        return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {"re": re, "subprocess": types.SimpleNamespace(
        run=lambda *args, **kwargs: types.SimpleNamespace(returncode=9, stdout="", stderr="injected failure"))}
    chosen = [function(exporter, "run"), assignment(verifier, "WAIVERS"), function(verifier, "waived")]
    exec(compile(ast.Module(body=chosen, type_ignores=[]), "baseline-selected-helpers", "exec"), namespace)
    with contextlib.redirect_stdout(io.StringIO()):
        silent_export = namespace["run"]("pcb", "export", "gerbers") is None
    waived_duplicate = namespace["waived"](("holes_co_located", "duplicate vias",
                                            "Severity: error; two distinct GND via objects"))
    scope = {"unconn": [], "unw": {}, "bad": 0, "m": re.match(r"(x)(0)", "x0"),
             "fc": {"max_interference_mm3": 5.002, "all_ports_aligned": True}}
    exec(compile(ast.Module(body=[assignment(verifier, "passed")], type_ignores=[]),
                 "baseline-release-expression", "exec"), scope)
    return {"failed_export_returned_normally": silent_export,
            "unreviewed_duplicate_vias_waived": waived_duplicate,
            "mechanical_collision_ignored": scope["passed"]}


if __name__ == "__main__":
    result = reproduce()
    print(json.dumps({"baseline_defects_reproduced": result, "fabrication_approval": False}, indent=2))
    raise SystemExit(0 if all(result.values()) else 1)

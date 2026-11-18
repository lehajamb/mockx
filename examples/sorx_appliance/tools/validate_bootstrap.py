#!/usr/bin/env python3
"""Check immutable source provenance without importing or running hardware tools.

Default: verify baseline archives, inventory and reference images; report active
hardware differences. --pristine also requires byte-identical hardware imports.
This is an import check, never an electrical or manufacturing approval.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(name: str) -> PurePosixPath:
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe relative path: {name!r}")
    if any(part in ("", ".", "..") for part in name.rstrip("/").split("/")):
        raise ValueError(f"Unsafe relative path: {name!r}")
    return PurePosixPath(name)


def local_path(name: str) -> Path:
    target = ROOT.joinpath(*safe_path(name).parts)
    if not target.resolve().is_relative_to(ROOT):
        raise ValueError(f"Path escapes appliance workspace: {name!r}")
    return target


def exclusion(name: str) -> str | None:
    if "__pycache__" in PurePosixPath(name).parts or name.endswith((".pyc", ".pyo")):
        return "Python bytecode/cache"
    if name.endswith(".kicad_prl"):
        return "KiCad personal UI state"
    return None


def validate(pristine: bool) -> dict:
    manifest = json.loads((ROOT / "baseline/manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != 1:
        raise ValueError("Unsupported manifest schema")
    sources = manifest["sources"]
    changed: list[str] = []
    imported = excluded = parsed = 0
    destinations: set[str] = set()
    for source in sources:
        archive_path = local_path(source["baseline_path"])
        content = archive_path.read_bytes()
        if len(content) != source["size_bytes"] or digest(content) != source["sha256"]:
            raise ValueError(f"Baseline source changed: {source['baseline_path']}")
        if "entries" not in source:
            continue
        prefix = source["archive_root"]
        safe_path(prefix)
        expected = source["entries"]
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if [m.filename for m in members] != [e["archive_path"] for e in expected]:
                raise ValueError(f"Archive inventory differs: {source['original_name']}")
            seen: set[str] = set()
            for member, entry in zip(members, expected, strict=True):
                name = member.filename
                safe_path(name)
                if not name.startswith(prefix) or name.casefold() in seen:
                    raise ValueError(f"Unexpected root or duplicate archive member: {name}")
                seen.add(name.casefold())
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError(f"Archive symlink: {name}")
                if (member.file_size != entry["size_bytes"]
                        or f"{member.CRC:08x}" != entry["crc32"]
                        or member.is_dir() != entry["directory"]):
                    raise ValueError(f"Archive metadata differs: {name}")
                reason = exclusion(name)
                disposition = "excluded" if reason else "directory" if member.is_dir() else "imported"
                if entry["disposition"] != disposition or entry.get("reason") != reason:
                    raise ValueError(f"Incorrect exclusion/disposition: {name}")
                if member.is_dir():
                    excluded += int(reason is not None)
                    continue
                original = archive.read(member)  # zipfile also checks member CRC.
                if digest(original) != entry["sha256"]:
                    raise ValueError(f"Archive member hash differs: {name}")
                if reason:
                    excluded += 1
                    continue
                relative = source["destination_root"] + "/" + name[len(prefix):]
                if entry["imported_path"] != relative or relative.casefold() in destinations:
                    raise ValueError(f"Incorrect or duplicate destination: {name}")
                destinations.add(relative.casefold())
                active_path = local_path(relative)
                active = active_path.read_bytes() if active_path.is_file() else None
                if active != original:
                    if relative.startswith("reference_images/") or pristine:
                        raise ValueError(f"Preserved/imported file differs: {relative}")
                    changed.append(relative)
                if relative.endswith(".py") and active is not None:
                    ast.parse(active, filename=relative)
                    parsed += 1
                imported += 1

    catalog = json.loads((ROOT / "docs/image-catalog.json").read_text(encoding="utf-8"))
    image_entries = [e for s in sources if s.get("destination_root") == "reference_images"
                     for e in s["entries"] if e["disposition"] == "imported"]
    if {e["imported_path"] for e in image_entries} != {e["path"] for e in catalog["images"]}:
        raise ValueError("Image catalog does not cover the supplied image bundle")
    baseline_hashes = {e["imported_path"]: e["sha256"] for s in sources
                       for e in s.get("entries", []) if e["disposition"] == "imported"}
    for item in catalog["images"]:
        if digest(local_path(item["path"]).read_bytes()) != item["sha256"]:
            raise ValueError(f"Image hash differs: {item['path']}")
        for duplicate in item["duplicates_in_hardware"]:
            if baseline_hashes.get(duplicate) != item["sha256"]:
                raise ValueError(f"Incorrect baseline image duplicate: {duplicate}")

    return {"check": "bootstrap provenance and Python syntax only",
            "fabrication_approval": False, "pristine": pristine,
            "sources_verified": len(sources), "imported_files_verified": imported,
            "excluded_entries": excluded, "python_files_parsed": parsed,
            "reference_image_files": len(image_entries), "changed_hardware_files": changed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pristine", action="store_true", help="Also require unchanged active hardware imports")
    args = parser.parse_args()
    try:
        result = validate(args.pristine)
    except (OSError, ValueError, KeyError, SyntaxError, zipfile.BadZipFile) as error:
        print(f"Bootstrap validation FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

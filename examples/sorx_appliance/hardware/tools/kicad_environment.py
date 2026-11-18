"""Explicit, validated footprint-library environment for headless KiCad 7 tools."""
from __future__ import annotations
import os
from pathlib import Path

FOOTPRINT_VARIABLE = "KICAD7_FOOTPRINT_DIR"
INSTALLED_FOOTPRINTS = Path("/usr/share/kicad/footprints")


def kicad_environment(base=None, *, installed_footprints=INSTALLED_FOOTPRINTS):
    """Copy the environment; validate configured paths instead of masking errors.

    pcbnew's Python entry point does not initialize the GUI's built-in library
    variables. Its global fp-lib-table still references this variable, so it
    must be supplied explicitly before starting a KiCad subprocess.
    """
    env = dict(os.environ if base is None else base)
    configured = env.get(FOOTPRINT_VARIABLE)
    root = Path(configured) if configured else Path(installed_footprints)
    root = root.expanduser().resolve()
    required = root / "Capacitor_SMD.pretty" / "C_0402_1005Metric.kicad_mod"
    if not root.is_dir() or not required.is_file():
        raise FileNotFoundError(f"{FOOTPRINT_VARIABLE} does not resolve the required installed library: {root}")
    env[FOOTPRINT_VARIABLE] = str(root)
    return env

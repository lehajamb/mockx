"""Regression coverage for headless KiCad footprint-library resolution."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from verification_core import run_tool

class KiCadEnvironmentTests(unittest.TestCase):
    def library(self, folder):
        root=Path(folder)/"footprints"
        required=root/"Capacitor_SMD.pretty"/"C_0402_1005Metric.kicad_mod"
        required.parent.mkdir(parents=True);required.write_text("(footprint fixture)")
        return root
    def test_missing_variable_uses_verified_installed_library(self):
        module=importlib.import_module("kicad_environment")
        with tempfile.TemporaryDirectory() as folder:
            root=self.library(folder);base={"PATH":"kept"}
            env=module.kicad_environment(base,installed_footprints=root)
            self.assertEqual(env["KICAD7_FOOTPRINT_DIR"],str(root.resolve()))
            self.assertEqual(base,{"PATH":"kept"})
            self.assertEqual(env["PATH"],"kept")
    def test_invalid_explicit_library_is_not_silently_replaced(self):
        module=importlib.import_module("kicad_environment")
        with tempfile.TemporaryDirectory() as folder:
            root=self.library(folder)
            with self.assertRaises(FileNotFoundError):
                module.kicad_environment({"KICAD7_FOOTPRINT_DIR":str(Path(folder)/"absent")},installed_footprints=root)
    def test_partial_library_is_rejected(self):
        module=importlib.import_module("kicad_environment")
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                module.kicad_environment({},installed_footprints=folder)
    def test_subprocess_receives_and_records_explicit_library_variable(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.library(folder);out=Path(folder)/"actual.txt"
            env={**os.environ,"KICAD7_FOOTPRINT_DIR":str(root)}
            command=[sys.executable,"-c","import os,pathlib,sys;pathlib.Path(sys.argv[1]).write_text(os.environ['KICAD7_FOOTPRINT_DIR'])",str(out)]
            record=run_tool("environment",command,folder,Path(folder)/"logs",[out],env=env)
            self.assertTrue(record["success"])
            self.assertEqual(out.read_text(),str(root))
            self.assertEqual(record["environment"]["KICAD7_FOOTPRINT_DIR"],str(root))
            self.assertEqual(set(record["environment"]),{"KICAD7_FOOTPRINT_DIR"})

if __name__=="__main__":unittest.main()

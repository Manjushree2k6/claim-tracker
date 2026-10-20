"""Tiny test runner for environments without pytest:  python run_tests.py
(With pytest installed, simply run `pytest -q`; the tests are plain functions.)"""
import glob
import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))

passed = failed = 0
for path in sorted(glob.glob(os.path.join(HERE, "tests", "test_*.py"))):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        try:
            getattr(mod, name)()
            passed += 1
            print(f"PASS  {os.path.basename(path)}::{name}")
        except Exception:
            failed += 1
            print(f"FAIL  {os.path.basename(path)}::{name}")
            traceback.print_exc()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

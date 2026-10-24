"""Run original bridge suites against this release without editing their sources.

Usage: python tests/run_legacy_bridge_tests.py --chess-tests PATH --eat-tests PATH
Only temporary SQLite fixtures are used by the supplied legacy suites.
"""
import argparse
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_chess(path):
    original_spec = importlib.util.spec_from_file_location

    def release_spec(name, location, *args, **kwargs):
        # The original suite derives its bridge path from its own location.
        # Redirect that import so copied suites also work without an old checkout.
        if name == "bridge" and Path(location).name == "chess_bridge.py":
            location = ROOT / "deploy" / "adapters" / "chess_bridge.py"
        return original_spec(name, location, *args, **kwargs)

    with patch.object(importlib.util, "spec_from_file_location", release_spec):
        module = load_module("legacy_chess_quota_tests", path)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if suite.countTestCases() == 0:
        raise RuntimeError("No legacy chess tests were collected")
    print(f"Running {suite.countTestCases()} original chess tests against deploy/adapters/chess_bridge.py", flush=True)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def run_eat(path, pytest):
    module_name = "deploy.lazylady_sso_bridge.app"
    module = load_module("release_eat_bridge", ROOT / "deploy" / "adapters" / "eat_bridge.py")
    print("Running original eat tests against deploy/adapters/eat_bridge.py", flush=True)
    # Isolate collection from the neighboring application's unrelated plugins,
    # configuration and ancestor conftest files. Its fixtures remain unchanged.
    with patch.dict(sys.modules, {module_name: module}), patch.dict(os.environ, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}):
        return pytest.main([
            "-q", "-p", "no:cacheprovider", "-c", os.devnull,
            f"--confcutdir={path.parent}", f"--rootdir={path.parent}", str(path),
        ]) == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chess-tests", type=Path, default=ROOT.parent / "world/chess-atelier/tests/central_quota_test.py")
    parser.add_argument("--eat-tests", type=Path, default=ROOT.parent / "mealie/tests/unit_tests/test_eat_sso_bridge.py")
    args = parser.parse_args()
    for path in (args.chess_tests, args.eat_tests):
        if not path.is_file():
            parser.error(f"Legacy test file does not exist: {path}")
    try:
        import pytest
    except ImportError:
        parser.error("Use a test Python environment containing pytest, fastapi, httpx and sqlalchemy")
    chess_ok = run_chess(args.chess_tests.resolve())
    eat_ok = run_eat(args.eat_tests.resolve(), pytest)
    return 0 if chess_ok and eat_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

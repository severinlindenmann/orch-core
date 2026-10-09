import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGES = [
    "schema",
    "canon",
    "crypto",
    "custody",
    "identity",
    "store",
    "model",
    "ops",
    "cli",
    "instructions",
    "addons",
    "importer",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name):
    assert importlib.import_module(f"orch.{name}").__doc__


def test_version():
    import orch

    assert orch.__version__ == "2.0.0.dev0"


def test_module_version():
    r = subprocess.run([sys.executable, "-m", "orch", "--version"], capture_output=True, text=True)
    assert (r.returncode, r.stdout.strip()) == (0, "orch v2 (in development)")


def test_entry_point_version():
    exe = shutil.which("orch", path=str(Path(sys.executable).parent))
    assert exe, "orch entry point not installed"
    r = subprocess.run([exe, "--version"], capture_output=True, text=True)
    assert (r.returncode, r.stdout.strip()) == (0, "orch v2 (in development)")


def test_unimplemented_exits_2():
    r = subprocess.run([sys.executable, "-m", "orch", "list"], capture_output=True, text=True)
    assert r.returncode == 2 and "not implemented" in r.stderr


def test_shared_vectors_parse():
    data = json.loads((Path(__file__).parent / "vectors" / "vectors_v2.json").read_text())
    assert "2" in data["suites"]

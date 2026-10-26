"""dev-pair test fixtures: the script loaded as a module (it has no ``.py``), and short real-path scratch roots (the
control socket limit is 100 bytes, so pytest's long ``tmp_path`` on macOS cannot hold a sandbox root)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev-pair"


def load() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("dev_pair", str(SCRIPT))
    spec = importlib.util.spec_from_loader("dev_pair", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dev_pair"] = mod  # dataclasses look their module up here
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def dp() -> ModuleType:
    return load()


@pytest.fixture
def scratch():
    """A short, private directory below the real ``/tmp`` (no symlink in the path), removed by its exact path."""
    d = Path(tempfile.mkdtemp(prefix="dp", dir=os.path.realpath("/tmp")))
    os.chmod(d, 0o700)
    yield d
    shutil.rmtree(d, ignore_errors=True)

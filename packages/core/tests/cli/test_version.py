"""U1: the build stamp, `orch --version` and `orch --version --json`."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from orch import _build

STAMP_PY = Path(__file__).resolve().parents[4] / "scripts" / "release" / "stamp.py"


def _stamp_module():
    spec = importlib.util.spec_from_file_location("stamp", STAMP_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stamp"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def stamped(tmp_path, monkeypatch):
    path = tmp_path / "_build.json"
    path.write_text(
        json.dumps(
            {
                "version": "2.0.0.dev42",
                "channel": "develop",
                "commit": "1a2b3c4d5e6f",
                "built_at": "2026-10-22T05:00:00Z",
            }
        )
    )
    monkeypatch.setattr(_build, "_STAMP", path)
    return path


def test_source_checkout_is_a_development_build(tmp_path, monkeypatch):
    monkeypatch.setattr(_build, "_STAMP", tmp_path / "missing.json")
    assert _build.version_line() == "orch 2.0.0.dev0 (development build)"
    assert _build.build_info()["development"] is True


def test_stamped_line_and_info(stamped):
    assert _build.version_line() == "orch 2.0.0.dev42 (channel develop, commit 1a2b3c4)"
    info = _build.build_info()
    assert (info["version"], info["channel"], info["commit"], info["development"]) == (
        "2.0.0.dev42",
        "develop",
        "1a2b3c4d5e6f",
        False,
    )


@pytest.mark.parametrize(
    "body", ["not json", "[]", '{"version": "2.0.0.dev1"}', '{"version":1,"channel":"a","commit":"b","built_at":"c"}']
)
def test_malformed_stamp_falls_back(tmp_path, monkeypatch, body):
    p = tmp_path / "_build.json"
    p.write_text(body)
    monkeypatch.setattr(_build, "_STAMP", p)
    assert _build.build_info()["development"] is True


def test_cli_version_plain_and_json(cli):
    r = cli("--version")
    assert (r.code, r.out) == (0, "orch 2.0.0.dev0 (development build)\n")
    r = cli("--version", "--json")
    assert r.code == 0
    data = json.loads(r.out)
    assert data["version"] == "2.0.0.dev0" and data["development"] is True and data["commit"] is None


DASH = 'dependencies = ["fastapi==1", "orch-core>=2.0.0.dev0"]'


def _tree(tmp_path, dash_body=None):
    core, dash = tmp_path / "core", tmp_path / "dash"
    (core / "src" / "orch").mkdir(parents=True)
    dash.mkdir()
    (core / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "2.0.0.dev0"\n')
    (core / "src" / "orch" / "__init__.py").write_text('"""x."""\n\n__version__ = "2.0.0.dev0"\n')
    body = dash_body if dash_body is not None else f'[project]\nversion = "0.1.0"\n{DASH}\n'
    (dash / "pyproject.toml").write_text(body)
    return core, dash


def test_stamp_writes_all_files(tmp_path):
    core, dash = _tree(tmp_path)
    _stamp_module().stamp("2.0.0.dev7", "main", "abcdef1234", core=core, dashboard=dash)
    assert 'version = "2.0.0.dev7"' in (core / "pyproject.toml").read_text()
    assert '__version__ = "2.0.0.dev7"' in (core / "src" / "orch" / "__init__.py").read_text()
    d = (dash / "pyproject.toml").read_text()
    assert 'version = "2.0.0.dev7"' in d and '"orch-core==2.0.0.dev7"' in d and "fastapi==1" in d
    stamp = json.loads((core / "src" / "orch" / "_build.json").read_text())
    assert stamp["version"] == "2.0.0.dev7" and stamp["channel"] == "main" and stamp["commit"] == "abcdef1234"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp["built_at"])


@pytest.mark.parametrize(
    "body, what",
    [
        (f"[project]\n{DASH}\n", "dashboard pyproject.toml version"),  # zero version lines
        (f'[project]\nversion = "0.1.0"\nversion = "0.2.0"\n{DASH}\n', "dashboard pyproject.toml version"),  # two
        ('[project]\nversion = "0.1.0"\ndependencies = ["fastapi==1"]\n', "orch-core dependency"),  # zero pins
        (
            f'[project]\nversion = "0.1.0"\n{DASH}\nextra = ["orch-core>=1"]\n',
            "orch-core dependency",
        ),  # two pins
    ],
)
def test_stamp_refuses_zero_or_two_matches_and_writes_nothing(tmp_path, body, what):
    core, dash = _tree(tmp_path, body)
    with pytest.raises(ValueError, match=what):
        _stamp_module().stamp("2.0.0.dev7", "main", "abcdef1234", core=core, dashboard=dash)
    assert 'version = "2.0.0.dev0"' in (core / "pyproject.toml").read_text()
    assert not (core / "src" / "orch" / "_build.json").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("bad", "main", "abcdef1"),
        ("2.0.0.dev1", "Main", "abcdef1"),
        ("2.0.0.dev1", "main", "xyz"),
        ("2.0.0.dev1", "../x", "abcdef1"),
    ],
)
def test_stamp_rejects_bad_input(tmp_path, args):
    with pytest.raises(ValueError):
        _stamp_module().stamp(*args, core=tmp_path, dashboard=tmp_path)


@pytest.mark.slow
def test_wheel_contains_build_json(tmp_path):
    """The gitignored stamp must land in the wheel (hatch `artifacts`)."""
    import shutil
    import subprocess
    import zipfile

    core = Path(__file__).resolve().parents[2]
    src = tmp_path / "core"
    shutil.copytree(core, src, ignore=shutil.ignore_patterns(".venv", "__pycache__", "tests", "dist", ".pytest_cache"))
    _stamp_module().stamp("2.0.0.dev9", "develop", "abcdef1", core=src, dashboard=_tree(tmp_path / "t")[1])
    out = tmp_path / "dist"
    r = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out), str(src)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    with zipfile.ZipFile(next(out.glob("*.whl"))) as z:
        assert "orch/_build.json" in z.namelist()


def test_stamp_sets_every_part_to_the_one_version(tmp_path):
    core, dash = _tree(tmp_path)
    hub, relay = tmp_path / "hub", tmp_path / "relay"
    hub.mkdir()
    relay.mkdir()
    (hub / "pyproject.toml").write_text('[project]\nversion = "2.0.0.dev0"\n')
    (hub / "__init__.py").write_text('__version__ = "2.0.0.dev0"\n')
    (relay / "pyproject.toml").write_text('[project]\nversion = "2.0.0.dev0"\n')
    parts = ((hub / "pyproject.toml", hub / "__init__.py"), (relay / "pyproject.toml", None))
    _stamp_module().stamp("2.0.0", "main", "abcdef1234", core=core, dashboard=dash, parts=parts)
    assert 'version = "2.0.0"' in (hub / "pyproject.toml").read_text()
    assert '__version__ = "2.0.0"' in (hub / "__init__.py").read_text()
    assert 'version = "2.0.0"' in (relay / "pyproject.toml").read_text()


def test_every_part_of_the_tree_carries_the_one_dev_version():
    """One version for all parts: a part left at its own number trips the hub and updater version checks."""
    import tomllib

    mod = _stamp_module()
    for pyproject in (mod.CORE / "pyproject.toml", mod.DASHBOARD / "pyproject.toml", *(p for p, _ in mod.PARTS)):
        assert tomllib.loads(pyproject.read_text())["project"]["version"] == "2.0.0.dev0", pyproject
    for init in (mod.CORE / "src" / "orch" / "__init__.py", *(i for _, i in mod.PARTS if i is not None)):
        assert '__version__ = "2.0.0.dev0"' in init.read_text(), init

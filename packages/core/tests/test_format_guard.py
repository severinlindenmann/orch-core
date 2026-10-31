"""Persisted-format guard for the host and workspace kinds (``scripts/formats.py``, ``contracts/formats/``).

A change to what core writes to disk (SQLite DDL, the version constants of persisted JSON, the store layout names)
fails here until a migration step is registered, ``CURRENT`` is bumped and the contract is updated. How:
``docs/architecture/persisted-formats.md``.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("orch_formats_guard", ROOT / "scripts" / "formats.py")
formats = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = formats
_spec.loader.exec_module(formats)

STEPS = "packages/core/src/orch/migrate/steps.py"
#: kind -> (a source file that holds DDL of the kind, a DDL text in it, the same text changed)
DDL = {
    "host": (
        "packages/core/src/orch/store/pins.py",
        "CREATE TABLE IF NOT EXISTS heads (head TEXT PRIMARY KEY, seq INTEGER, prev TEXT)",
        "CREATE TABLE IF NOT EXISTS heads (head TEXT PRIMARY KEY, seq INTEGER, prev TEXT, extra TEXT)",
    ),
    "workspace": (
        "packages/core/src/orch/store/index.py",
        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);",
        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL, extra TEXT);",
    ),
}


def edit(monkeypatch, path, old, new):
    """Make ``formats.read_source`` return ``path`` with ``old`` replaced by ``new`` (``old`` must be in it)."""
    real = formats.read_source

    def read(rel):
        text = real(rel)
        if rel == path:
            assert old in text, f"{old!r} is no longer in {path}: update this test"
            text = text.replace(old, new)
        return text

    monkeypatch.setattr(formats, "read_source", read)


def expected(kind):
    return (
        f"persisted format changed for {kind}: add a migration step (packages/core/src/orch/migrate/steps.py), "
        f'bump CURRENT["{kind}"] and run scripts/formats.py --update {kind}'
    )


@pytest.mark.parametrize("kind", ["host", "workspace"])
def test_the_contract_matches_the_code(kind):
    assert formats.check(kind) == []


@pytest.mark.parametrize("kind", ["host", "workspace"])
def test_a_changed_ddl_string_fails_the_check(kind, monkeypatch, capsys):
    edit(monkeypatch, *DDL[kind])
    assert formats.check(kind) == [expected(kind)]
    assert formats.main(["--check", kind]) == 1
    assert expected(kind) in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["host", "workspace"])
def test_update_refuses_a_changed_format_that_was_not_bumped(kind, monkeypatch, tmp_path):
    shutil.copy(formats.contract_path(kind), tmp_path / f"{kind}.json")
    monkeypatch.setattr(formats, "CONTRACT_DIR", str(tmp_path))  # an absolute path replaces the root
    before = (tmp_path / f"{kind}.json").read_text()
    edit(monkeypatch, *DDL[kind])
    (problem,) = formats.update(kind)
    assert problem.startswith(f"refusing to update {kind}")
    assert (tmp_path / f"{kind}.json").read_text() == before


@pytest.mark.parametrize("kind", ["host", "workspace"])
def test_a_bumped_format_without_a_step_fails(kind, monkeypatch, capsys):
    edit(monkeypatch, STEPS, f'"{kind}": 1', f'"{kind}": 2')
    (problem,) = formats.check(kind)
    assert f"no migration step {kind} 1→2 is registered" in problem
    assert formats.main(["--check", kind]) == 1
    assert "no migration step" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["host", "workspace"])
def test_a_bumped_format_with_its_step_asks_for_the_contract_update(kind, monkeypatch):
    real = formats.read_source

    def read(rel):
        text = real(rel)
        if rel == STEPS:
            text = text.replace(f'"{kind}": 1', f'"{kind}": 2') + f'\nregister(Step("{kind}", 1, 2, "x", f))\n'
        return text

    monkeypatch.setattr(formats, "read_source", read)
    (problem,) = formats.check(kind)
    assert "is stale" in problem and "no migration step" not in problem

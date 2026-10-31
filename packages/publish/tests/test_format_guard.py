"""Persisted-format guard for publish (``scripts/formats.py``, ``contracts/formats/publish.json``).

A change to ``pubsrv/db.py`` (``SCHEMA``, ``SCHEMA_VERSION``) or to the layout of the data dir (``shares/``, ``apps/``,
``incoming/``, ``publish.db``) fails here until a migration step is registered in core, ``CURRENT["publish"]`` is bumped
and the contract is updated: ``docs/architecture/persisted-formats.md``.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "orch_formats_guard", ROOT / "scripts" / "formats.py"
)
formats = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = formats
_spec.loader.exec_module(formats)

KIND = "publish"
STEPS = "packages/core/src/orch/migrate/steps.py"
DDL = (
    "packages/publish/pubsrv/db.py",
    "CREATE TABLE IF NOT EXISTS owners(",
    "CREATE TABLE IF NOT EXISTS owners(extra TEXT,",
)
CHANGED = (
    "persisted format changed for publish: add a migration step (packages/core/src/orch/migrate/steps.py), "
    'bump CURRENT["publish"] and run scripts/formats.py --update publish'
)


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


def test_the_contract_matches_the_code():
    assert formats.check(KIND) == []


def test_a_changed_ddl_string_fails_the_check(monkeypatch, capsys):
    edit(monkeypatch, *DDL)
    assert formats.check(KIND) == [CHANGED]
    assert formats.main(["--check", KIND]) == 1
    assert CHANGED in capsys.readouterr().err


def test_a_changed_data_dir_layout_fails_the_check(monkeypatch):
    old = 'for d in ("incoming", "shares", "apps"):'
    edit(
        monkeypatch,
        "packages/publish/pubsrv/shares.py",
        old,
        'for d in ("incoming", "shares", "apps", "extra"):',
    )
    assert formats.check(KIND) == [CHANGED]


def test_update_refuses_a_changed_format_that_was_not_bumped(monkeypatch, tmp_path):
    shutil.copy(formats.contract_path(KIND), tmp_path / f"{KIND}.json")
    monkeypatch.setattr(
        formats, "CONTRACT_DIR", str(tmp_path)
    )  # an absolute path replaces the root
    before = (tmp_path / f"{KIND}.json").read_text()
    edit(monkeypatch, *DDL)
    (problem,) = formats.update(KIND)
    assert problem.startswith("refusing to update publish")
    assert (tmp_path / f"{KIND}.json").read_text() == before


def test_a_bumped_format_without_a_step_fails(monkeypatch, capsys):
    edit(monkeypatch, STEPS, '"publish": 1', '"publish": 2')
    (problem,) = formats.check(KIND)
    assert "no migration step publish 1→2 is registered" in problem
    assert formats.main(["--check", KIND]) == 1
    assert "no migration step" in capsys.readouterr().err


def test_a_bumped_format_with_its_step_asks_for_the_contract_update(monkeypatch):
    real = formats.read_source

    def read(rel):
        text = real(rel)
        if rel == STEPS:
            text = (
                text.replace('"publish": 1', '"publish": 2')
                + '\nregister(Step("publish", 1, 2, "x", f))\n'
            )
        return text

    monkeypatch.setattr(formats, "read_source", read)
    (problem,) = formats.check(KIND)
    assert "is stale" in problem and "no migration step" not in problem

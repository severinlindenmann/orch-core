"""The A/B layout contract (orch.update.layout): flip, shim, retention, the strict install record."""

from __future__ import annotations

import json
import os

import pytest

from orch.ops.errors import OrchError
from orch.update.layout import (
    InstallLayout,
    flip_current,
    prune_cache,
    prune_tools,
    read_record,
    requirements_text,
    version_key,
    write_record,
)


@pytest.fixture
def lay(tmp_path):
    lay = InstallLayout.of({"HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path / "state")})
    lay.ensure()
    return lay


def add(lay, ver, complete=True):
    (lay.tool_dir(ver) / "venv" / "bin").mkdir(parents=True)
    (lay.tool_dir(ver) / "venv" / "bin" / "orch").write_text("x")
    if complete:
        (lay.tool_dir(ver) / ".complete").write_text("")


def test_paths_follow_the_contract(lay, tmp_path):
    state, data = tmp_path / "state", tmp_path / ".local/share/orch"
    assert lay.record == data / "install.json" and lay.cache == data / "cache" and lay.lock == data / "update.lock"
    assert lay.tool_bin("2.0.0") == state / "install/tools/orch-2.0.0/venv/bin/orch"
    assert lay.current == state / "install/current"
    assert (lay.root.stat().st_mode & 0o777) == 0o700
    assert (
        InstallLayout.of({"XDG_DATA_HOME": str(tmp_path / "x"), "ORCH_STATE_DIR": str(state)}).data
        == tmp_path / "x/orch"
    )
    assert InstallLayout.of({"XDG_DATA_HOME": "rel", "HOME": str(tmp_path), "ORCH_STATE_DIR": str(state)}).data == data


@pytest.mark.parametrize("bad", ["../x", "2.0", "2.0.0/..", "", "2.0.0.dev", "a.b.c", "2.0.0 ", "2.0.0\n"])
def test_a_version_is_never_a_path(lay, bad):
    with pytest.raises(OrchError):
        lay.tool_dir(bad)


def test_flip_is_a_relative_symlink(lay):
    add(lay, "2.0.0.dev1")
    add(lay, "2.0.0.dev2")
    add(lay, "2.0.0.dev3", complete=False)
    flip_current(lay, "2.0.0.dev1")
    assert os.readlink(lay.current) == "tools/orch-2.0.0.dev1" and lay.current_version() == "2.0.0.dev1"
    flip_current(lay, "2.0.0.dev2")
    assert lay.current_version() == "2.0.0.dev2"
    assert not list(lay.root.glob("current.new-*"))
    for v in ("2.0.0.dev9", "2.0.0.dev3"):  # not installed / not complete
        with pytest.raises(OrchError):
            flip_current(lay, v)
    assert lay.current_version() == "2.0.0.dev2"
    assert lay.installed_versions() == ["2.0.0.dev2", "2.0.0.dev1"]


def test_requirements_text_is_what_install_sh_writes(tmp_path):
    a = tmp_path / "orch_core-1-py3-none-any.whl"
    text = requirements_text("x==1\ny==2\n", [("core", a, "ab" * 32), ("constraints", tmp_path / "c.txt", "00")])
    assert text == f"x==1\ny==2\n{a.as_uri()} --hash=sha256:{'ab' * 32}\n"


def test_a_current_link_that_points_elsewhere_names_no_version(lay):
    os.symlink("/etc", lay.current)
    assert lay.current_version() is None


def test_versions_sort_dev_before_release():
    assert version_key("2.0.0.dev9") < version_key("2.0.0") < version_key("2.0.1.dev1") < version_key("2.1.0")
    assert version_key("2.0.0.dev9") < version_key("2.0.0.dev10")


def test_retention_keeps_current_previous_and_one_more(lay):
    for v in ("2.0.0.dev1", "2.0.0.dev2", "2.0.0.dev3", "2.0.0.dev4", "2.0.0.dev5"):
        add(lay, v)
    gone = prune_tools(lay, "2.0.0.dev2")  # current is an old one: it is kept besides the two newest others
    assert sorted(gone) == ["2.0.0.dev1", "2.0.0.dev3"]
    assert lay.installed_versions() == ["2.0.0.dev5", "2.0.0.dev4", "2.0.0.dev2"]
    for v in ("2.0.0.dev1", "2.0.0.dev2", "2.0.0.dev3"):
        lay.cache_dir(v).mkdir(parents=True)
    prune_cache(lay, {"2.0.0.dev1"})
    assert sorted(p.name for p in lay.cache.iterdir()) == ["2.0.0.dev1", "2.0.0.dev2", "2.0.0.dev3"][0:3]
    lay.cache_dir("2.0.0.dev4").mkdir()
    prune_cache(lay, set())
    assert sorted(p.name for p in lay.cache.iterdir()) == ["2.0.0.dev3", "2.0.0.dev4"]


def test_the_record_round_trips_at_0600_and_is_strict(lay):
    from .world import record

    rec = record("2.0.0", {"main": 3})
    write_record(lay, rec)
    assert (lay.record.stat().st_mode & 0o777) == 0o600
    assert read_record(lay) == rec
    doc = json.loads(lay.record.read_text())
    assert doc["format"] == "orch.install/1"
    for bad in (
        {**doc, "extra": 1},
        {**doc, "channel": "../x"},
        {**doc, "format": "orch.install/2"},
        {**doc, "seq_seen": {"main": 0}},
        {**doc, "version": "x"},
        {**doc, "uv": "x"},
        {k: v for k, v in doc.items() if k != "files"},
    ):
        lay.record.write_text(json.dumps(bad))
        with pytest.raises(OrchError) as e:
            read_record(lay)
        assert e.value.code == "update.no_record"
    lay.record.write_text("not json")
    with pytest.raises(OrchError):
        read_record(lay)
    lay.record.unlink()
    with pytest.raises(OrchError):
        read_record(lay)

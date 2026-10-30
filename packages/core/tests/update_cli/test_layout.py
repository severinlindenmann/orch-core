"""The A/B layout contract (orch.update.layout): flip, shim, retention, the strict install record."""

from __future__ import annotations

import json
import os

import pytest

from orch.ops.errors import OrchError
from orch.update.layout import (
    InstallLayout,
    Record,
    ensure_shim,
    flip_current,
    prune_cache,
    prune_tools,
    read_record,
    version_key,
    write_record,
)


@pytest.fixture
def lay(tmp_path):
    lay = InstallLayout.of({"HOME": str(tmp_path)})
    lay.ensure()
    return lay


def add(lay, ver):
    (lay.tool_dir(ver) / "bin").mkdir(parents=True)
    (lay.tool_dir(ver) / "bin" / "orch").write_text("x")


def test_paths_follow_the_contract(lay, tmp_path):
    base = tmp_path / ".local/share/orch"
    assert lay.record == base / "install.json"
    assert lay.tool_bin("2.0.0") == base / "install/tools/orch-2.0.0/bin/orch"
    assert lay.current == base / "install/current" and lay.shim == base / "install/bin/orch"
    assert (lay.root.stat().st_mode & 0o777) == 0o700
    assert InstallLayout.of({"XDG_DATA_HOME": str(tmp_path / "x")}).data == tmp_path / "x/orch"
    assert InstallLayout.of({"XDG_DATA_HOME": "rel", "HOME": str(tmp_path)}).data == base


@pytest.mark.parametrize("bad", ["../x", "2.0", "2.0.0/..", "", "2.0.0.dev", "a.b.c", "2.0.0 ", "2.0.0\n"])
def test_a_version_is_never_a_path(lay, bad):
    with pytest.raises(OrchError):
        lay.tool_dir(bad)


def test_flip_is_a_relative_symlink_and_the_shim_follows(lay):
    add(lay, "2.0.0.dev1")
    add(lay, "2.0.0.dev2")
    flip_current(lay, "2.0.0.dev1")
    assert os.readlink(lay.current) == "tools/orch-2.0.0.dev1" and lay.current_version() == "2.0.0.dev1"
    ensure_shim(lay)
    assert os.path.realpath(lay.shim) == str(lay.tool_bin("2.0.0.dev1").resolve())
    flip_current(lay, "2.0.0.dev2")
    assert os.path.realpath(lay.shim) == str(lay.tool_bin("2.0.0.dev2").resolve())
    assert not list(lay.root.glob("*.tmp"))
    with pytest.raises(OrchError):
        flip_current(lay, "2.0.0.dev9")  # not installed
    assert lay.current_version() == "2.0.0.dev2"


def test_a_current_link_that_points_elsewhere_names_no_version(lay):
    os.symlink("/etc", lay.current)
    assert lay.current_version() is None


def test_versions_sort_dev_before_release():
    assert version_key("2.0.0.dev9") < version_key("2.0.0") < version_key("2.0.1.dev1") < version_key("2.1.0")
    assert version_key("2.0.0.dev9") < version_key("2.0.0.dev10")


def test_retention_keeps_current_previous_and_one_more(lay):
    for v in ("2.0.0.dev1", "2.0.0.dev2", "2.0.0.dev3", "2.0.0.dev4", "2.0.0.dev5"):
        add(lay, v)
    gone = prune_tools(lay, "2.0.0.dev5", "2.0.0.dev4")
    assert sorted(gone) == ["2.0.0.dev1", "2.0.0.dev2"]
    assert lay.installed_versions() == ["2.0.0.dev5", "2.0.0.dev4", "2.0.0.dev3"]
    for v in ("2.0.0.dev1", "2.0.0.dev2", "2.0.0.dev3"):
        lay.cache_dir(v).mkdir(parents=True)
    prune_cache(lay, {"2.0.0.dev1"})
    assert sorted(p.name for p in lay.cache.iterdir()) == ["2.0.0.dev1", "2.0.0.dev2", "2.0.0.dev3"][0:3]
    lay.cache_dir("2.0.0.dev4").mkdir()
    prune_cache(lay, set())
    assert sorted(p.name for p in lay.cache.iterdir()) == ["2.0.0.dev3", "2.0.0.dev4"]


def test_the_record_round_trips_at_0600_and_is_strict(lay):
    rec = Record("main", "2.0.0", None, {"main": 3}, "2026-10-20T00:00:00Z", "root-1", {"a.whl": "ab"}, 5)
    write_record(lay, rec)
    assert (lay.record.stat().st_mode & 0o777) == 0o600
    assert read_record(lay) == rec
    doc = json.loads(lay.record.read_text())
    for bad in (
        {**doc, "extra": 1},
        {**doc, "channel": "../x"},
        {**doc, "seq_seen": {"main": 0}},
        {**doc, "version": "x"},
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

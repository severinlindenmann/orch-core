"""Manifest CLI uses no host, listener, or signing state."""

import sys
from pathlib import Path

import pytest

from orch_dashboard import __main__, manifest

REPO_MANIFEST = Path(__file__).resolve().parents[2] / "orch-addon.json"


@pytest.fixture
def data(monkeypatch):
    data = REPO_MANIFEST.read_bytes()
    monkeypatch.setattr(manifest, "manifest_bytes", lambda: data)
    return data


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["orch-dashboard", "addon-manifest", *map(str, args)])
    __main__.main()


def test_print(data, monkeypatch, capsys):
    invoke(monkeypatch)
    assert capsys.readouterr().out.encode() == data


def test_write_and_identical(data, tmp_path, monkeypatch):
    (tmp_path / ".orch").mkdir()
    invoke(monkeypatch, "--write", tmp_path)
    target = tmp_path / ".orch/addons/dashboard/orch-addon.json"
    assert target.read_bytes() == data
    invoke(monkeypatch, "--write", tmp_path)
    assert target.read_bytes() == data


def test_without_a_store_nothing_is_created(data, tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit, match="2"):
        invoke(monkeypatch, "--write", tmp_path)
    assert "no orch store" in capsys.readouterr().err and not list(tmp_path.iterdir())


def test_refuse_and_force(data, tmp_path, monkeypatch, capsys):
    (tmp_path / ".orch").mkdir()
    invoke(monkeypatch, "--write", tmp_path)
    target = tmp_path / ".orch/addons/dashboard/orch-addon.json"
    target.write_bytes(b"different")
    with pytest.raises(SystemExit, match="2"):
        invoke(monkeypatch, "--write", tmp_path)
    assert "--force" in capsys.readouterr().err
    assert target.read_bytes() == b"different"
    invoke(monkeypatch, "--write", tmp_path, "--force")
    assert target.read_bytes() == data


@pytest.mark.parametrize("part", ["workspace", "store", "addons", "dashboard", "manifest"])
@pytest.mark.parametrize("force", [False, True])
def test_symlink_refusal(data, tmp_path, monkeypatch, part, force):
    workspace = tmp_path / "workspace"
    (workspace / ".orch/addons/dashboard").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    target = {
        "workspace": workspace,
        "store": workspace / ".orch",
        "addons": workspace / ".orch/addons",
        "dashboard": workspace / ".orch/addons/dashboard",
        "manifest": workspace / ".orch/addons/dashboard/orch-addon.json",
    }[part]
    if target.is_dir():
        # Rename the original so the link target also looks like a valid workspace subtree.
        other.rmdir()
        target.rename(other)
    target.symlink_to(other)
    with pytest.raises(SystemExit, match="2"):
        invoke(monkeypatch, "--write", workspace, *(["--force"] if force else []))
    assert not list(other.rglob("orch-addon.json"))

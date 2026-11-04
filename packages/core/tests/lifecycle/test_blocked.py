"""run-12 O12: macOS turned orch off in Login Items & Extensions: `orch services` says so; `orch stop` is not that."""

from __future__ import annotations

import io

from orch.lifecycle import units
from orch.lifecycle.units import Env, blocked_by_macos, blocked_orch_labels

DISABLED = (
    'disabled services = {\n\t"io.orch.host" => disabled\n\t"io.orch.runner" => enabled\n\t"com.x.y" => disabled\n}\n'
)


def env(tmp_path, out=DISABLED, platform="darwin"):
    return Env(env={"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "st")}, out=io.StringIO(),
               err=io.StringIO(), platform=platform, uid=501, run=lambda argv: (0, out))  # fmt: skip


def test_disabled_label_is_blocked_enabled_is_not(tmp_path):
    e = env(tmp_path)
    assert blocked_by_macos(e, "io.orch.host") is True
    assert blocked_by_macos(e, "io.orch.runner") is False
    assert blocked_orch_labels(e) == ["io.orch.host"]


def test_not_macos_is_never_blocked(tmp_path):
    assert blocked_by_macos(env(tmp_path, platform="linux"), "io.orch.host") is False


def test_a_unit_orch_stop_disabled_is_not_blocked(tmp_path, monkeypatch):
    e = env(tmp_path)
    monkeypatch.setattr(units, "stopped_by_orch", lambda state: {"io.orch.host"})
    assert blocked_by_macos(e, "io.orch.host") is False and blocked_orch_labels(e) == []


def test_a_part_orch_stop_stopped_reads_stopped_not_blocked(tmp_path, monkeypatch):
    """Run 14: `orch stop` records a part by its NAME; macOS lists its launchd LABEL as disabled; it is stopped."""

    class Runner:
        def info(self, key):
            return {"running": False}

        def label(self, key):
            return "io.orch.host"

    monkeypatch.setattr(units, "_runner", lambda e, kind: Runner())
    e = env(tmp_path)
    monkeypatch.setattr(units, "stopped_by_orch", lambda state: {"dashboard"})
    assert units._part(e, "dashboard", "Dashboard", "g", {"runner": "launchd"}).blocked is False
    monkeypatch.setattr(units, "stopped_by_orch", lambda state: set())
    assert units._part(e, "dashboard", "Dashboard", "g", {"runner": "launchd"}).blocked is True  # really blocked

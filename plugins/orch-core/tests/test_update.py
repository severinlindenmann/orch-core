import json
import os
import subprocess

import pytest

from addon_fixtures import GOOD, make_addon
from orch import update
from orch.addons import manage, userfiles
from orch.addons.discovery import find


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from orch import actor
    from orch.addons import discovery
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path / "no-defaults")
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr(update, "core_source", lambda: None)  # tests opt in to a core clone explicitly
    monkeypatch.delenv(update.CONTINUE_ENV, raising=False)
    monkeypatch.setattr(update, "refresh_plugin", lambda: "plugin stub")  # never run the real claude CLI


class Talk:
    def __init__(self, answer="", confirm=False):
        self.answer, self.confirmed, self.said, self.asked, self.reviews = answer, confirm, [], 0, 0

    def run(self, **kw):
        def ask(_):
            self.asked += 1
            return self.answer

        def review(r):
            self.reviews += 1
            return "REVIEW"
        update.run(ask=ask, review_text=review, confirm=lambda n: self.confirmed, out=self.said.append, **kw)


def _installed_enabled(tmp_path, root):
    src = make_addon(tmp_path / "src")
    manage.install(str(src))
    manage.trust_addon("hello-status")
    userfiles.set_enabled(root, "hello-status", True)
    return src


def _bump(src, **over):
    data = json.loads((src / "orch-addon.json").read_text())
    (src / "orch-addon.json").write_text(json.dumps({**data, "version": "0.2.0", **over}))


def test_addon_update_with_nothing_new_is_trusted_again_and_stays_enabled(tmp_path):
    root = tmp_path / "ws"
    src = _installed_enabled(tmp_path, root)
    _bump(src)
    t = Talk("y")
    t.run(check_only=False, force=True)
    assert t.asked == 1 and t.reviews == 0
    assert userfiles.trust_state(find("hello-status")) == "trusted"
    assert userfiles.workspace_addons(root)["hello-status"]["enabled"] is True
    assert t.said == ["updated hello-status 0.1.0 → 0.2.0"]


def test_addon_update_with_a_new_permission_asks_before_trusting(tmp_path):
    src = _installed_enabled(tmp_path, tmp_path / "ws")
    _bump(src, binaries=["git", "ls"])
    t = Talk("y", confirm=False)
    t.run(check_only=False, force=True)
    assert t.reviews == 1
    assert userfiles.trust_state(find("hello-status")) == "changed"  # installed, left off
    assert "stays off" in t.said[-1]
    t2 = Talk("y", confirm=True)
    _bump(src, version="0.3.0", binaries=["git", "ls"])
    t2.run(check_only=False, force=True)
    assert userfiles.trust_state(find("hello-status")) == "trusted"


def test_saying_no_snoozes_for_a_day_but_orch_update_ignores_it(tmp_path):
    src = _installed_enabled(tmp_path, tmp_path / "ws")
    _bump(src)
    t = Talk("n")
    t.run(check_only=False, force=False)
    assert t.asked == 1 and userfiles.registry_entries()["hello-status"]["version"] == "0.1.0"  # nothing applied
    t.run(check_only=False, force=False)
    assert t.asked == 1  # snoozed: serve stays quiet
    t.answer = "y"
    t.run(check_only=False, force=True)
    assert t.asked == 2 and userfiles.trust_state(find("hello-status")) == "trusted"


def test_check_only_changes_nothing(tmp_path):
    src = _installed_enabled(tmp_path, tmp_path / "ws")
    _bump(src)
    t = Talk("y")
    t.run(check_only=True, force=True)
    assert t.asked == 0 and t.said[0].startswith("updates available: hello-status")
    assert userfiles.registry_entries()["hello-status"]["version"] == "0.1.0"


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-q", "-b", "main", cwd=upstream)
    (upstream / "f").write_text("1")
    _git("add", ".", cwd=upstream)
    _git("commit", "-qm", "one", cwd=upstream)
    repo = tmp_path / "clone"
    _git("clone", "-q", str(upstream), str(repo), cwd=tmp_path)
    monkeypatch.setattr(update, "core_source", lambda: (repo, repo))
    return upstream, repo


def test_core_update_pulls_reinstalls_and_restarts(tmp_path, monkeypatch, clone):
    upstream, repo = clone
    assert update.core_check() is None
    (upstream / "f").write_text("2")
    _git("commit", "-qam", "two", cwd=upstream)
    assert update.core_check().behind == 1

    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f"#!/bin/sh\necho \"$@\" > {log}\n")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{uv.parent}{os.pathsep}{os.environ['PATH']}")
    execs = []
    monkeypatch.setattr(os, "execv", lambda path, argv: execs.append(argv))
    t = Talk("")
    t.run(check_only=False, force=True)
    assert (repo / "f").read_text() == "2"
    assert log.read_text().startswith("tool install --force --reinstall orch-core[dashboard] @")
    assert execs and os.environ.get(update.CONTINUE_ENV) == "1" and "plugin stub" in t.said
    assert update.core_check() is None  # the recorded build is now the clone's HEAD


def test_after_a_core_update_the_restart_does_not_ask_again(tmp_path, monkeypatch):
    src = _installed_enabled(tmp_path, tmp_path / "ws")
    _bump(src)
    monkeypatch.setenv(update.CONTINUE_ENV, "1")
    t = Talk("n")
    t.run(check_only=False, force=False)
    assert t.asked == 0 and userfiles.trust_state(find("hello-status")) == "trusted"


def test_offline_core_check_is_silent(tmp_path, monkeypatch, clone):
    upstream, repo = clone
    _git("remote", "set-url", "origin", str(tmp_path / "gone"), cwd=repo)
    assert update.core_check() is None

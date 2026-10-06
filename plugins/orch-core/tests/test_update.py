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
    monkeypatch.setattr(update, "core_source", lambda: "no clone in tests")  # tests opt in to a core clone explicitly
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
    assert t.said[-1] == "updated hello-status 0.1.0 → 0.2.0"
    assert "orch-core: not checked: no clone in tests" in t.said and "addons: 1 checked, 1 with updates" in t.said


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
    assert t.asked == 1 and t.said[-1].startswith("update check: next one ")  # snoozed: serve only says when
    t.answer = "y"
    t.run(check_only=False, force=True)
    assert t.asked == 2 and userfiles.trust_state(find("hello-status")) == "trusted"


def test_check_only_changes_nothing(tmp_path):
    src = _installed_enabled(tmp_path, tmp_path / "ws")
    _bump(src)
    t = Talk("y")
    t.run(check_only=True, force=True)
    assert t.asked == 0 and t.said[-1].startswith("updates available: hello-status")
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


def test_offline_core_check_says_why(tmp_path, monkeypatch, clone):
    upstream, repo = clone
    _git("remote", "set-url", "origin", str(tmp_path / "gone"), cwd=repo)
    assert update.core_check() is None
    s = update.core_status()
    assert not s.checked and s.line.startswith("not checked: could not fetch")


def test_core_without_upstream_says_why(clone):
    upstream, repo = clone
    _git("branch", "--unset-upstream", cwd=repo)
    s = update.core_status()
    assert not s.checked and "no upstream branch" in s.line


def test_orch_update_always_says_what_it_checked_and_found(clone):
    upstream, repo = clone
    t = Talk("")
    t.run(check_only=False, force=True)
    assert t.said[0] == f"checking for updates: orch-core at {upstream} (main) and the custom addons …"
    assert t.said[1].startswith("orch-core: up to date (commit ")
    assert t.said[2:] == ["addons: no custom addons installed", "nothing to update"]
    t.run(check_only=True, force=True)
    assert t.said[-1] == "nothing to update"
    (upstream / "f").write_text("2")
    _git("commit", "-qam", "two", cwd=upstream)
    t.run(check_only=True, force=True)
    assert "orch-core: 1 new commit (" in t.said[-3] and t.said[-1] == "updates available: orch-core (1 new commit)"


def test_a_core_that_cannot_be_checked_is_named_not_called_up_to_date(monkeypatch):
    monkeypatch.setattr(update, "core_source", lambda: "orch-core was installed from /x, which is not a git clone")
    t = Talk("")
    t.run(check_only=True, force=True)
    assert "orch-core: not checked: orch-core was installed from /x, which is not a git clone" in t.said
    assert "up to date" not in " ".join(t.said)


def test_serve_says_the_check_is_clean_and_when_the_next_one_is(tmp_path):
    t = Talk("")
    t.run(check_only=False, force=False)
    assert t.said[-1] == "nothing to update; next check in a day (orch update checks now)"
    t.run(check_only=False, force=False)
    assert t.said[-1].startswith("update check: next one ") and len(t.said) == 5


def test_a_plugin_cache_install_points_at_the_marketplace_clone(tmp_path, monkeypatch):
    from importlib import metadata
    plugins = tmp_path / ".claude" / "plugins"
    cache = plugins / "cache" / "orch-core" / "orch-core" / "0.4.1"
    cache.mkdir(parents=True)
    clone = plugins / "marketplaces" / "orch-core"
    (clone / "plugins" / "orch-core").mkdir(parents=True)
    (clone / "plugins" / "orch-core" / "pyproject.toml").write_text("")
    _git("init", "-q", cwd=clone)

    class Dist:
        def read_text(self, name):
            return json.dumps({"url": cache.as_uri(), "dir_info": {}})
    monkeypatch.undo()  # the real core_source, with the fake distribution below
    monkeypatch.setattr(metadata, "distribution", lambda name: Dist())
    said = update.core_source()
    assert f"installed from {cache}, which is not a git clone" in said
    assert f'uv tool install --force "{clone / "plugins" / "orch-core"}[dashboard]"' in said
    assert update.marketplace_clone(clone / "plugins" / "orch-core") is None  # only cache folders map to a clone


def test_remote_urls_lose_their_credentials():
    assert update._public_url("https://user:tok@github.com/o/r.git") == "https://github.com/o/r.git"
    assert update._public_url("git@github.com:o/r.git") == "git@github.com:o/r.git"


@pytest.fixture
def tagged_remote(tmp_path, monkeypatch):
    """A git remote with release tags, and an install of it pinned at v0.4.1 (no clone anywhere)."""
    up = tmp_path / "remote"
    up.mkdir()
    _git("init", "-q", "-b", "main", cwd=up)
    (up / "f").write_text("1")
    _git("add", ".", cwd=up)
    _git("commit", "-qm", "one", cwd=up)
    for t in ("v0.4.1", "v0.4.10", "v0.4.2", "nightly"):
        _git("tag", t, cwd=up)
    monkeypatch.setattr(update, "core_source", lambda: update.GitRemote(up.as_uri(), "plugins/orch-core", "v0.4.1"))
    return up


def test_remote_install_is_behind_the_newest_tag_by_version_not_by_name(tagged_remote):
    s = update.core_status()
    assert s.checked and s.update.tag == "v0.4.10" and s.line == "v0.4.1 → v0.4.10"


def test_remote_install_on_the_newest_tag_is_up_to_date(tagged_remote, monkeypatch):
    monkeypatch.setattr(update, "core_source", lambda: update.GitRemote(tagged_remote.as_uri(), None, "v0.4.10"))
    s = update.core_status()
    assert s.update is None and s.checked and s.line == "up to date (v0.4.10)"


def test_unreachable_remote_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "core_source", lambda: update.GitRemote((tmp_path / "nope").as_uri(), None, "v0.4.1"))
    s = update.core_status()
    assert s.update is None and not s.checked and "could not list the tags" in s.line


def test_remote_update_reinstalls_from_the_tag(tmp_path, monkeypatch, tagged_remote):
    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f"#!/bin/sh\necho \"$@\" > {log}\n")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{uv.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(os, "execv", lambda path, argv: None)
    t = Talk("")
    t.run(check_only=False, force=True)
    assert log.read_text().strip() == (
        f"tool install --force --reinstall orch-core[dashboard] @ git+{tagged_remote.as_uri()}@v0.4.10#subdirectory=plugins/orch-core")
    assert "orch-core (v0.4.1 → v0.4.10)" in " ".join(t.said) or any("v0.4.1 → v0.4.10" in s for s in t.said)


def test_direct_url_with_vcs_info_is_a_remote_install(monkeypatch):
    from importlib import metadata

    class Dist:
        def read_text(self, name):
            return json.dumps({"url": "https://github.com/o/r", "subdirectory": "plugins/orch-core",
                               "vcs_info": {"vcs": "git", "requested_revision": "v0.4.1"}})
    monkeypatch.undo()
    monkeypatch.setattr(metadata, "distribution", lambda name: Dist())
    assert update.core_source() == update.GitRemote("https://github.com/o/r", "plugins/orch-core", "v0.4.1")

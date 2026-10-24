"""V3 G5 (#923): ``orch addon new | pack | dev`` on the command line. ``new`` and ``pack`` are local and offline (an
agent may run them: no host, no key); ``dev`` is a person's command and an agent is refused before the host is asked."""

from __future__ import annotations

import io
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from orch.addons import package
from orch.cli import addon_market as am
from orch.cli import publish
from orch.cli.main import main as orch_main
from orch.market import author

GIT_ENV = {
    "PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}  # fmt: skip


def run(argv, env=None, *, as_json=True):
    out = io.StringIO()
    code = am.main([*argv, *(["--json"] if as_json else [])], env=env or {}, out=out, err=io.StringIO())
    return code, out.getvalue()


def sh(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=GIT_ENV)


@pytest.fixture
def host(monkeypatch):
    log = SimpleNamespace(called=[], connected=[])
    monkeypatch.setattr(publish, "_target", lambda env: object())
    monkeypatch.setattr(publish, "_call", lambda t, op, args, env: log.called.append((op, args)) or {})
    monkeypatch.setattr(am, "_connect", lambda *a: log.connected.append(a) or (_ for _ in ()).throw(OSError("no host")))
    return log


@pytest.fixture
def here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_new_writes_a_scaffold_that_validates(here, host):
    code, out = run(["new", "changelog"])
    assert code == 0, out
    data = json.loads(out)["data"]
    assert data["name"] == "changelog" and data["files"] == ["README.md", "main.py", "orch-addon.json"]
    files = {p: (here / "changelog" / p).read_bytes() for p in data["files"]}
    author.check_files(files)
    assert host.called == [] and host.connected == []


def test_new_text_says_what_to_do_next(here):
    code, out = run(["new", "changelog"], as_json=False)
    assert code == 0 and "orch addon pack" in out and "git commit" in out


def test_new_never_overwrites(here):
    (here / "changelog").mkdir()
    (here / "changelog" / "orch-addon.json").write_text("mine")
    code, out = run(["new", "changelog"])
    assert code == 2 and json.loads(out)["code"] == "invalid.input"
    assert (here / "changelog" / "orch-addon.json").read_text() == "mine"


@pytest.mark.parametrize("name", ["Changelog", "a/b", "..", "", "x" * 41])
def test_new_refuses_a_bad_name(here, name):
    code, out = run(["new", name])
    assert code == 2 and json.loads(out)["code"] == "invalid.input"
    assert not any(here.iterdir())


def test_pack_prints_the_digest_and_an_entry_ready_to_paste(here, host):
    run(["new", "changelog"])
    repo = here / "changelog"
    sh(repo, "init", "-q", "-b", "main")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "first")
    code, out = run(["pack", str(repo), "--git", "https://github.com/acme/orch-addon-changelog"])
    assert code == 0, out
    d = json.loads(out)["data"]
    files = author.scaffold("changelog")
    assert d["package_sha256"] == package.package_digest(files) and d["entry"]["package_sha256"] == d["package_sha256"]
    assert d["entry"]["commit"] == d["commit"] and d["placeholder_git"] is False
    code, text = run(["pack", str(repo)], as_json=False)
    assert code == 0 and d["package_sha256"] in text and '"commit"' in text and "placeholder" in text
    assert host.called == [] and host.connected == []


def test_pack_defaults_to_the_current_directory(here, monkeypatch):
    run(["new", "changelog"])
    repo = here / "changelog"
    sh(repo, "init", "-q", "-b", "main")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "first")
    monkeypatch.chdir(repo)
    code, out = run(["pack"])
    assert code == 0 and json.loads(out)["data"]["name"] == "changelog"


def test_pack_refuses_a_folder_that_is_not_a_repository_root(here):
    run(["new", "changelog"])
    code, out = run(["pack", str(here / "changelog")])
    assert code == 2 and "git repository" in json.loads(out)["message"]


def test_pack_refuses_a_broken_commit_with_its_code(here):
    run(["new", "changelog"])
    repo = here / "changelog"
    (repo / "lib.so").write_bytes(b"\x7fELF")
    sh(repo, "init", "-q", "-b", "main")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "x")
    code, out = run(["pack", str(repo)])
    assert code == 3 and json.loads(out)["code"] == "market.package_invalid"


@pytest.mark.parametrize("env", [{"ORCH_GRANT": "g"}, {"ORCH_SESSION": "s"}])
def test_an_agent_may_scaffold_and_pack_but_not_dev(here, host, env):
    assert run(["new", "x-addon"], env)[0] == 0
    code, out = run(["dev", str(here / "x-addon")], env)
    assert code == 3 and json.loads(out)["code"] == "market.actor_refused"
    assert host.called == [] and host.connected == []


def test_dev_asks_the_host_only_for_a_person(here, host):
    run(["new", "x-addon"])
    code, out = run(["dev", str(here / "x-addon")])
    assert code == 2 and "git repository" in json.loads(out)["message"] and host.connected == []  # before any host call
    sh(here / "x-addon", "init", "-q", "-b", "main")
    code, out = run(["dev", str(here / "x-addon")])
    assert code == 9 and json.loads(out)["code"] == "host.unavailable" and len(host.connected) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["new"], ["new", "a", "b"], ["pack", "a", "b"], ["dev"], ["new", "a", "--commit", "x"],
        ["install", "a", "--git", "u"], ["dev", "a", "--git", "https://github.com/a/b"], ["pack", "--check"],
    ],
)  # fmt: skip
def test_usage_errors(here, argv):
    code, out = run(argv)
    assert code == 2 and json.loads(out)["code"] == "invalid.input"


def test_the_top_level_command_reaches_it(here):
    out, err = io.StringIO(), io.StringIO()
    assert orch_main(["addon", "new", "viathecli", "--json"], env={}, stdout=out, stderr=err) == 0
    assert (here / "viathecli" / "orch-addon.json").is_file()


def test_new_writes_through_one_directory_descriptor_and_cleans_up_after_a_failure(here, monkeypatch):
    real = os.open
    seen: list[bool] = []

    def flaky(path, flags, mode=0o777, *, dir_fd=None):
        if str(path) == "main.py":
            seen.append(dir_fd is not None)
            raise OSError(28, "No space left on device")
        return real(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", flaky)
    code, out = run(["new", "changelog"])
    assert code == 2 and "nothing was kept" in json.loads(out)["message"] and seen == [True]
    assert not (here / "changelog").exists()  # its own new directory, and what it created in it, are gone


def test_new_files_are_created_below_a_directory_descriptor(here, monkeypatch):
    real = os.open
    rels: list[tuple[str, bool]] = []

    def spy(path, flags, mode=0o777, *, dir_fd=None):
        if str(path) in ("orch-addon.json", "main.py", "README.md"):
            rels.append((str(path), dir_fd is not None))
        return real(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", spy)
    assert run(["new", "changelog"])[0] == 0
    assert sorted(rels) == [("README.md", True), ("main.py", True), ("orch-addon.json", True)]

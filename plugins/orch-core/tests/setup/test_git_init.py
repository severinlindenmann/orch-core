"""Design r3 part C (4): a folder without git and Worktrees on: setup offers ``git init``, declares the repo inside the
setup signature, writes ``.gitignore`` lines for orch's folders and never commits the person's files."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from orch.setup import steps
from orch.setup.protocol import SetupError

from .conftest import get, post
from .fakes import FakePorts, names
from .test_create import (
    AGENT,
    fake_signer,  # noqa: F401 - the signing stand-ins
    run_ok,
    setup_flow,
)
from .test_secrets import reveal, words_for

WITH_GIT = {**AGENT, "git_init": True}


def test_the_state_says_whether_the_folder_is_a_git_repository(flow, tmp):
    assert get(flow, "state")[1]["git"] == {"repo": False}
    (tmp.work / ".git").mkdir()
    assert get(flow, "state")[1]["git"] == {"repo": True}


def test_a_folder_below_a_repository_counts_as_one(tmp):
    (tmp.root / ".git").mkdir()
    f = setup_flow(tmp, FakePorts())
    assert get(f, "state")[1]["git"] == {"repo": True}


def test_git_init_is_refused_where_git_already_is(tmp):
    (tmp.work / ".git").mkdir()
    f = setup_flow(tmp, FakePorts())
    with pytest.raises(SetupError) as e:
        post(f, "profile", WITH_GIT)
    assert e.value.code == "invalid.input"


def test_git_init_must_be_a_boolean(flow):
    with pytest.raises(SetupError):
        post(flow, "profile", {**AGENT, "git_init": "yes"})


def test_the_review_names_git_init_and_the_repo_it_declares(tmp):
    f = setup_flow(tmp, FakePorts())
    post(f, "profile", WITH_GIT)
    r = reveal(f)
    post(f, "secrets/confirm", {"words": words_for(r)})
    rv = post(f, "review")[1]
    assert any(x["id"] == "git" and "git init" in x["text"] and "None of your files" in x["text"] for x in rv["flags"])
    settings = next(i for i in rv["items"] if i["id"] == "settings")
    assert settings["type"] == "settings.changed" and settings["fields"]["set"]["repos"] == {
        "work": {"path": str(tmp.work)}
    }


def test_the_run_makes_the_repository_before_it_prepares_the_settings(tmp):
    f, ports, p, _ = run_ok(tmp, WITH_GIT)
    assert p["error"] is None and p["done"] is True
    n = names(ports)
    assert n.index("git_init") < n.index("human.prepare")
    assert ("git_init", str(tmp.work)) in ports.log
    assert [s["id"] for s in p["steps"]].index("git") < [s["id"] for s in p["steps"]].index("settings")


def test_without_the_choice_nothing_is_initialised(tmp):
    f, ports, p, rv = run_ok(tmp, AGENT)
    assert "git_init" not in names(ports) and all(x["id"] != "git" for x in rv["flags"])


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@needs_git
def test_init_repository_makes_an_empty_repository_with_the_ignore_lines_and_commits_nothing(tmp):
    (tmp.work / "mine.txt").write_text("keep me")
    steps.init_repository(tmp.work)
    assert (tmp.work / ".git").is_dir()
    assert (tmp.work / ".gitignore").read_text().splitlines() == [".orch/", ".orch-work/"]
    assert (tmp.work / "mine.txt").read_text() == "keep me"
    log = subprocess.run(["git", "-C", str(tmp.work), "log"], capture_output=True, text=True)
    assert log.returncode != 0  # no commit exists
    staged = subprocess.run(["git", "-C", str(tmp.work), "diff", "--cached", "--name-only"], capture_output=True)
    assert staged.stdout == b""  # nothing was added either


@needs_git
def test_init_repository_appends_to_an_existing_gitignore_and_never_replaces_it(tmp):
    (tmp.work / ".gitignore").write_text("node_modules\n.orch-work/")
    steps.init_repository(tmp.work)
    assert (tmp.work / ".gitignore").read_text().splitlines() == ["node_modules", ".orch-work/", ".orch/"]


@needs_git
def test_init_repository_leaves_an_existing_repository_alone(tmp):
    (tmp.work / ".git").mkdir()
    steps.init_repository(tmp.work)
    assert not (tmp.work / ".gitignore").exists() and list((tmp.work / ".git").iterdir()) == []


@needs_git
def test_init_repository_refuses_a_gitignore_that_is_a_link(tmp):
    target = tmp.root / "elsewhere"
    target.write_text("x")
    (tmp.work / ".gitignore").symlink_to(target)
    with pytest.raises(OSError):
        steps.init_repository(tmp.work)
    assert target.read_text() == "x"

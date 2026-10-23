"""Regression (Opus review #439 F1, the reviewer's PoC inverted): a repo's OWN config (.git/config) defines a filter
driver and an attribute selects it. Before the fix the runner's worktree.add checkout and the direct-mode read_git
status ran it; now the orch.gitsafe gate refuses to run git there at all, and since #565 the direct-mode status is
orch's own reader (no git process).

The invariant: orch never runs git against a repository whose local/worktree config or attributes could make git
execute a program or read config from elsewhere."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from test_wtgit_confine import NAME, _git, world  # noqa: F401

from orch import gitsafe
from orch.runner import wtgit
from orch.runner.supervise import SpawnError


def _plant(repo: Path, marker: Path, *, committed: bool = True) -> None:
    if committed:
        (repo / ".gitattributes").write_text("* filter=evil\n")
        _git(repo, "add", ".gitattributes")
        _git(repo, "commit", "-qm", "attrs")
    else:  # nothing committed: .git/info/attributes selects the driver
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    _git(repo, "config", "filter.evil.smudge", f"sh -c 'touch {marker}; cat'")
    _git(repo, "config", "filter.evil.clean", f"sh -c 'touch {marker}; cat'")


@pytest.mark.parametrize("committed", [True, False])
def test_worktree_add_refuses_a_repo_local_smudge_filter(world, committed):  # noqa: F811
    root, repo, own, args = world
    marker = root / "smudged"
    _plant(repo, marker, committed=committed)
    a = args("worktree.add")
    a["base"] = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(SpawnError) as e:
        wtgit.run_git(a, own)
    assert e.value.code == "observe.unavailable"
    assert "filter.evil" in e.value.message
    assert not marker.exists(), "repo-local smudge filter ran"
    assert not (Path(f"{repo}.worktrees") / NAME).exists()


def test_the_direct_status_runs_no_repo_local_clean_filter(world):  # noqa: F811
    """#565: the direct-mode status is orch's reader in-process; a repo-local clean filter never runs, and the
    answer is the real one (the bytes changed: dirty)."""
    from orch.gitread.local import READER

    root, repo, own, args = world
    marker = root / "cleaned"
    _plant(repo, marker)
    marker.unlink(missing_ok=True)
    time.sleep(1.1)
    (repo / "f").write_text("y")  # changed stat: status would hash it through the clean filter
    assert "modified: f" in READER.dirty(repo)
    assert not marker.exists(), "repo-local clean filter ran"
    import subprocess

    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, check=False)
    assert marker.exists(), "the control (plain git status) must run the filter"


def test_remove_checks_the_worktrees_own_config_too(world):  # noqa: F811
    """``git worktree remove`` reads the worktree's config too: its config.worktree is gated as well (defence in depth:
    since #565 remove always runs ``--force``, so git's own status never runs there)."""
    root, repo, own, args = world
    assert wtgit.run_git(args("worktree.add"), own) == {"ok": True}
    wt = Path(f"{repo}.worktrees") / NAME
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(wt, "config", "--worktree", "filter.evil.clean", "sh -c 'touch /nonexistent'")
    with pytest.raises(SpawnError) as e:
        wtgit.run_git(args("worktree.remove"), own)
    assert e.value.code == "observe.unavailable" and "the worktree" in e.value.message
    assert wt.exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("include.path", "/elsewhere/config"),
        ("includeIf.gitdir:/.path", "/elsewhere/config"),
        ("core.attributesFile", "/elsewhere/attributes"),
        ("filter.x.process", "evil"),
        ("merge.x.driver", "evil"),
        ("core.sshCommand", "evil"),
        ("core.gitProxy", "evil"),
        ("core.askPass", "evil"),
        ("credential.helper", "evil"),
        ("credential.https://example.com.helper", "evil"),
        ("core.fsmonitor", "/tmp/evil"),
        ("core.pager", "evil"),
        ("core.editor", "evil"),
        ("sequence.editor", "evil"),
        ("gpg.program", "evil"),
        ("gpg.ssh.program", "evil"),
        ("remote.origin.uploadpack", "evil"),
        ("remote.origin.receivepack", "evil"),
        ("uploadpack.packObjectsHook", "evil"),
        ("lfs.customtransfer.x.path", "evil"),
        ("lfs.extension.x.smudge", "evil"),
        ("lfs.standalonetransferagent", "x"),
        ("filter.lfs.required", "true"),  # the filter section is not on the allow-list at all (install LFS globally)
        ("alias.st", "!sh -c evil"),  # refused since the allow-list (round 2); orch never invokes an alias anyway
    ],
)
def test_the_gate_refuses_every_program_key(world, key, value):  # noqa: F811
    """Every program key outside ``diff.*``: the gate tolerates ``diff.*`` (orch-52 on #561), see below."""
    root, repo, own, args = world
    _git(repo, "config", key, value)
    with pytest.raises(gitsafe.Unsafe) as e:
        gitsafe.gate(str(repo), own)
    assert key.lower().split(".")[0] in e.value.reason.lower()
    with pytest.raises(SpawnError) as e2:
        wtgit.run_git(args("worktree.add"), own)
    assert e2.value.code == "observe.unavailable"
    assert value not in e2.value.message  # the key is named, never its value


@pytest.mark.parametrize(
    "key,value",
    [
        ("core.hooksPath", ".husky"),  # overridden by -c core.hooksPath=/dev/null on every command
        ("core.fsmonitor", "true"),  # git's built-in daemon, no program; overridden to false anyway
        ("remote.origin.url", "https://example.com/x.git"),
        ("user.name", "x"),
    ],
)
def test_the_gate_lets_harmless_keys_through(world, key, value):  # noqa: F811
    root, repo, own, args = world
    _git(repo, "config", key, value)
    gitsafe.gate(str(repo), own)


def test_an_overridden_hooks_path_never_runs_a_hook(world):  # noqa: F811
    root, repo, own, args = world
    hooks = repo / ".husky"
    hooks.mkdir()
    marker = root / "hooked"
    (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch {marker}\n")
    os.chmod(hooks / "post-checkout", 0o755)
    _git(repo, "config", "core.hooksPath", ".husky")
    assert wtgit.run_git(args("worktree.add"), own) == {"ok": True}
    assert not marker.exists()


def test_an_unlistable_config_fails_closed(world):  # noqa: F811
    root, repo, own, args = world
    (repo / ".git" / "config").write_text("[core\nbroken")
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.gate(str(repo), own)


def test_exec_risk_ignores_other_scopes():
    assert gitsafe.exec_risk([("global", "filter.lfs.smudge", "git-lfs smudge %f")]) is None
    assert gitsafe.exec_risk([("command", "core.fsmonitor", "/x")]) is None
    assert gitsafe.exec_risk([("worktree", "filter.x.clean", "c")]) == "filter.x.clean"


def test_a_global_driver_still_works_in_run_git(world):  # noqa: F811
    """The person's own global filter (LFS-like): worktree add refuses the repo instead of running it."""
    root, repo, own, args = world
    marker = root / "global-smudge"
    script = root / "smudge.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    os.chmod(script, 0o755)
    # #584: the checkout is orch's own; only the allow-listed filter name (lfs) from the PERSON's config is applied
    (root / ".gitconfig").write_text(f'[filter "lfs"]\n\tsmudge = {script}\n\tclean = cat\n')
    (repo / ".gitattributes").write_text("* filter=lfs\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "attrs")
    a = args("worktree.add")
    a["base"] = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(SpawnError) as e:  # #584 (structural): no filter process runs during populate, lfs included
        wtgit.run_git(a, own)
    assert e.value.code == "worktree.checkout_unsupported"
    assert not marker.exists()


@pytest.mark.parametrize("key", ["diff.x.textconv", "diff.x.command", "diff.external"])
def test_the_gate_tolerates_a_diff_program_and_the_worktree_commands_never_run_it(world, key):  # noqa: F811
    """orch-52 on #561: ``diff.*`` is tolerated (git-lfs and editors set it) and neutralised: orch's diff operations
    pin --no-ext-diff/--no-textconv, and ``worktree add``/``remove`` run no diff at all."""
    root, repo, own, args = world
    marker = root / "diff-program-ran"
    (repo / ".gitattributes").write_text("* diff=x\n")
    _git(repo, "config", key, f'echo ran >> {marker}; cat "$1" 2>/dev/null; true')
    gitsafe.gate(str(repo), own)
    assert wtgit.run_git(args("worktree.add"), own) == {"ok": True}
    assert wtgit.run_git(args("worktree.remove"), own) == {"ok": True}
    assert not marker.exists()
    # positive control: plain git's diff runs that program, so the assertion above can fail
    (repo / "f").write_text("y")  # a tracked change the attribute maps to driver x
    _git(repo, "diff")  # check=True: rc 0
    assert marker.exists()

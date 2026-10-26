"""#439 fix round 2: the gate of :mod:`orch.gitsafe` is an allow-list (Opus D1, structural), refuses config files
another account could write (Opus D3), refuses populated submodules (Codex High/Medium), and the confinement walk
checks every directory from ``/`` (Opus D2)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_wtgit_confine import NAME, _git, world  # noqa: F401

from orch import gitsafe
from orch.gitread.errors import Unavailable
from orch.gitread.local import READER
from orch.runner import wtgit
from orch.runner.supervise import SpawnError

L = "local"

#: every key known to make git execute a program or read config elsewhere (gitsafe.KNOWN_EXEC), as git spells them
EXEC_KEYS = [
    "hook.evil.command", "hook.evil.event", "filter.x.smudge", "filter.x.clean", "filter.x.process",
    "filter.lfs.required", "diff.x.textconv", "diff.x.command", "diff.x.binary", "diff.external", "merge.x.driver",
    "merge.x.name", "include.path", "includeIf.gitdir:/x/.path", "core.attributesFile", "core.pager", "pager.log",
    "pager.status", "alias.st", "core.editor", "sequence.editor", "gpg.program", "gpg.ssh.program",
    "gpg.openpgp.program", "gpg.x509.program", "gpg.ssh.defaultKeyCommand", "credential.helper",
    "credential.https://h.example.helper", "credential.username", "core.sshCommand", "core.gitProxy", "core.askPass",
    "uploadpack.packObjectsHook", "gc.recentObjectsHook", "remote.origin.vcs", "remote.origin.uploadpack",
    "remote.origin.receivepack", "remote.origin.proxy", "difftool.x.cmd", "difftool.x.path", "mergetool.x.cmd",
    "mergetool.x.path", "man.x.cmd", "man.x.path", "browser.x.cmd", "browser.x.path", "guitool.x.cmd",
    "trailer.x.cmd", "trailer.x.command", "sendemail.smtpServer", "sendemail.x.toCmd", "sendemail.ccCmd",
    "web.browser", "help.browser", "core.alternateRefsCommand", "lfs.customtransfer.x.path", "lfs.extension.x.clean",
    "lfs.standalonetransferagent", "lfs.url", "submodule.s.update", "init.templateDir", "pull.twohead",
    "pull.octopus", "branch.main.mergeOptions", "core.worktree", "core.excludesFile", "http.proxy",
    "http.https://h.example.extraHeader", "url.https://evil.example/.insteadOf", "protocol.allow", "safe.directory",
    "receive.procReceiveRefs", "fsck.skipList", "notes.rewriteRef", "unknownsection.key", "orch.anything",
]  # fmt: skip


@pytest.mark.parametrize("key", EXEC_KEYS)
def test_every_known_exec_key_and_any_unknown_key_is_refused(key):
    assert gitsafe.exec_risk([(L, key, "x")]) == key
    assert gitsafe.exec_risk([("worktree", key, "x")]) == key


@pytest.mark.parametrize("value", ["/tmp/evil", "evil", "!sh -c x", "true; x"])
def test_a_non_boolean_fsmonitor_is_refused(value):
    assert gitsafe.exec_risk([(L, "core.fsmonitor", value)]) == "core.fsmonitor"


#: a typical .git/config: ``git init`` on macOS, ``git clone``, branch tracking, per-repo identity and signing, LFS's
#: own local key (``lfs.<url>.access``, ``lfs.repositoryformatversion``; its filters are global), husky
REAL_WORLD = [
    ("core.repositoryformatversion", "0"), ("core.filemode", "true"), ("core.bare", "false"),
    ("core.logallrefupdates", "true"), ("core.ignorecase", "true"), ("core.precomposeunicode", "true"),
    ("core.hooksPath", ".husky"), ("core.fsmonitor", "false"), ("core.autocrlf", "input"),
    ("remote.origin.url", "git@github.com:o/r.git"), ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
    ("remote.origin.gh-resolved", "base"), ("remote.pushDefault", "origin"),
    ("branch.main.remote", "origin"), ("branch.main.merge", "refs/heads/main"), ("branch.feat/x.remote", "origin"),
    ("branch.feat/x.merge", "refs/heads/feat/x"), ("branch.feat/x.rebase", "true"), ("user.name", "P"),
    ("user.email", "p@example.com"), ("user.signingkey", "ABCD"), ("commit.gpgsign", "true"), ("gpg.format", "ssh"),
    ("pull.rebase", "true"), ("push.autoSetupRemote", "true"), ("init.defaultBranch", "main"),
    ("extensions.worktreeConfig", "true"), ("extensions.objectFormat", "sha256"), ("gc.auto", "0"),
    ("maintenance.auto", "false"), ("maintenance.gc.enabled", "true"), ("lfs.repositoryformatversion", "0"),
    ("lfs.https://github.com/o/r.git/info/lfs.access", "basic"), ("color.ui", "auto"), ("color.diff.meta", "blue"),
    ("advice.detachedHead", "false"), ("status.showUntrackedFiles", "all"), ("diff.renames", "copies"),
    ("merge.conflictStyle", "zdiff3"), ("rebase.autoStash", "true"), ("fetch.prune", "true"),
    ("submodule.recurse", "false"), ("rerere.enabled", "true"),
]  # fmt: skip


def test_a_typical_real_world_config_passes():
    assert gitsafe.exec_risk([(L, k, v) for k, v in REAL_WORLD]) is None


def test_a_real_clone_with_tracking_passes_the_gate(world):  # noqa: F811
    root, repo, own, args = world
    clone = root / "clone"
    _git(root, "clone", "-q", str(repo), str(clone))
    _git(clone, "checkout", "-q", "-b", "feat", "--track", "origin/main")
    _git(clone, "config", "user.email", "p@example.com")
    _git(clone, "config", "core.hooksPath", ".husky")
    gitsafe.gate(str(clone), own)
    got = READER.dirty(clone)
    assert got == "", got  # and orch's reader reads it, clean (#565)


def test_an_unknown_section_is_refused_by_the_gate_named_never_valued(world):  # noqa: F811
    root, repo, own, args = world
    _git(repo, "config", "newgitfeature.runthis", "s3cr3t-value")
    with pytest.raises(gitsafe.Unsafe) as e:
        gitsafe.gate(str(repo), own)
    assert "newgitfeature.runthis" in e.value.reason and "s3cr3t-value" not in e.value.reason
    with pytest.raises(SpawnError) as e2:
        wtgit.run_git(args("worktree.add"), own)
    assert e2.value.code == "observe.unavailable" and "s3cr3t-value" not in e2.value.message


def test_records_check_local_uses_the_same_allow_list():
    from orch.records import RecordsError
    from orch.records.git import check_local

    with pytest.raises(RecordsError) as e:
        check_local([(L, "hook.x.command", "touch m"), (L, "hook.x.event", "reference-transaction")], "origin")
    assert e.value.code == "records.unsafe_config"
    with pytest.raises(RecordsError):
        check_local([(L, "unknownsection.key", "x")], "origin")
    check_local([(L, "remote.origin.url", "https://example.com/r.git"), (L, "user.name", "x")], "origin")


# -- Opus D3: config files another account could write ---------------------------------------------------------------


def test_a_group_or_other_writable_config_file_is_refused(world):  # noqa: F811
    root, repo, own, args = world
    cfg = repo / ".git" / "config"
    for mode in (0o664, 0o646):
        os.chmod(cfg, mode)
        with pytest.raises(gitsafe.Unsafe) as e:
            gitsafe.gate(str(repo), own)
        assert "config file" in e.value.reason
        with pytest.raises(SpawnError) as e2:
            wtgit.run_git(args("worktree.add"), own)
        assert e2.value.code == "observe.unavailable"
    os.chmod(cfg, 0o644)
    gitsafe.gate(str(repo), own)


def test_a_config_file_of_another_owner_is_refused(world, monkeypatch):  # noqa: F811
    root, repo, own, args = world
    real = os.geteuid()
    monkeypatch.setattr(gitsafe.os, "geteuid", lambda: real + 1)
    with pytest.raises(gitsafe.Unsafe) as e:
        gitsafe.gate(str(repo), own)
    assert "not owned by this user" in e.value.reason


def test_a_symlinked_or_writable_worktree_config_is_refused(world):  # noqa: F811
    root, repo, own, args = world
    _git(repo, "config", "extensions.worktreeConfig", "true")
    wtc = repo / ".git" / "config.worktree"
    wtc.write_text("[user]\n\tname = x\n")
    gitsafe.gate(str(repo), own)
    os.chmod(wtc, 0o666)
    with pytest.raises(gitsafe.Unsafe) as e:
        gitsafe.gate(str(repo), own)
    assert "config.worktree" in e.value.reason
    wtc.unlink()
    elsewhere = root / "elsewhere.config"
    elsewhere.write_text("[user]\n\tname = x\n")
    wtc.symlink_to(elsewhere)
    with pytest.raises(gitsafe.Unsafe) as e:
        gitsafe.gate(str(repo), own)
    assert "symlink" in e.value.reason


def test_a_group_writable_git_dir_is_refused(world):  # noqa: F811
    root, repo, own, args = world
    os.chmod(repo / ".git", 0o775)
    try:
        with pytest.raises(gitsafe.Unsafe) as e:
            gitsafe.gate(str(repo), own)
        assert "git dir" in e.value.reason
    finally:
        os.chmod(repo / ".git", 0o755)


# -- Codex High/Medium: submodules are refused, never inspected and never "clean" -----------------------------------


def _populated_submodule(repo: Path, marker: Path) -> Path:
    """A checked-out gitlink ``sub`` whose OWN config defines a clean filter that creates ``marker``, with a change."""
    sub = repo / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    (sub / "a").write_text("a\n")
    (sub / ".gitattributes").write_text("* filter=m\n")
    _git(sub, "add", ".")
    _git(sub, "commit", "-qm", "s")
    _git(repo, "add", "sub")
    _git(repo, "commit", "-qm", "gitlink")
    _git(sub, "config", "filter.m.clean", f"touch {marker}; cat")
    # Same size as the committed "a\n": git then has to read (and so filter) the file to see the change. A different
    # size is reported as modified from the stat data alone, and whether the filter also ran depended on whether the
    # index entry was still "racy" (written in the same clock tick), which made the control below flaky.
    (sub / "a").write_text("b\n")
    return sub


def test_a_submodule_clean_filter_never_runs_and_its_state_is_never_clean(world):  # noqa: F811
    import subprocess

    root, repo, own, args = world
    marker = root / "submodule-filter-ran"
    _populated_submodule(repo, marker)
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1"}
    # control: plain git status runs the submodule's own filter, so the assertions below can fail
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], env=env, capture_output=True, check=True)
    assert marker.exists(), "the control must run the submodule's filter"
    marker.unlink()
    gitsafe.gate(str(repo), own)  # the parent's own config is fine
    with pytest.raises(Unavailable) as e:  # orch's reader (#565): never "" (clean), never opens the submodule
        READER.dirty(repo)
    assert "submodule" in e.value.reason
    assert not marker.exists()


def test_an_empty_gitlink_directory_is_not_a_populated_submodule(world):  # noqa: F811
    root, repo, own, args = world
    _populated_submodule(repo, root / "m")
    import shutil

    shutil.rmtree(repo / "sub")
    (repo / "sub").mkdir()  # as after a clone without --recurse-submodules
    assert READER.dirty(repo) == ""


# -- Opus D2: every directory from / to <repo>.worktrees ------------------------------------------------------------


def test_a_group_writable_intermediate_directory_is_refused(world):  # noqa: F811
    root, repo, own, args = world
    above = root.parent  # an ancestor above the repo's parent (the parent itself has the stricter check)
    old = above.stat().st_mode & 0o7777
    try:
        os.chmod(above, 0o775)
        with pytest.raises(SpawnError) as e:
            wtgit.run_git(args("worktree.add"), own)
        assert e.value.code == "launch.cwd_changed" and "writable by group or others" in e.value.message
        assert not Path(f"{repo}.worktrees", NAME).exists()
        os.chmod(above, 0o1777)  # sticky: others cannot rename what they do not own (like /tmp)
        assert wtgit.run_git(args("worktree.add"), own) == {"ok": True}
    finally:
        os.chmod(above, old)


def test_an_intermediate_directory_of_another_owner_is_refused(world, monkeypatch):  # noqa: F811
    root, repo, own, args = world
    real_fstat = os.fstat
    above = root.parent.stat()

    def fstat(fd):
        st = real_fstat(fd)
        if (st.st_dev, st.st_ino) == (above.st_dev, above.st_ino):
            fields = list(st)
            fields[4] = os.geteuid() + 1  # st_uid: another, non-root account
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(wtgit.os, "fstat", fstat)
    with pytest.raises(SpawnError) as e:
        wtgit.run_git(args("worktree.add"), own)
    assert e.value.code == "launch.cwd_changed" and "owned by another user" in e.value.message


def test_a_staged_gitlink_change_with_an_empty_submodule_directory_is_dirty(world):  # noqa: F811
    import shutil

    root, repo, own, args = world
    sub = _populated_submodule(repo, root / "m")
    _git(sub, "add", ".")
    _git(sub, "commit", "-qm", "two")
    new = _git(sub, "rev-parse", "HEAD").strip()
    shutil.rmtree(repo / "sub")
    (repo / "sub").mkdir()
    _git(repo, "update-index", "--cacheinfo", f"160000,{new},sub")  # staged: index differs from HEAD
    assert "modified: sub" in READER.dirty(repo)


@pytest.mark.parametrize("key", ["rebase.instructionFormat", "rebase.instructionformat", "sequence.editor"])
def test_rebase_keys_that_format_or_run_the_todo_list_are_refused(key):
    assert gitsafe.exec_risk([(L, key, "%s%nexec touch /tmp/x")]) == key


@pytest.mark.parametrize("key", ["rebase.autosquash", "rebase.reschedulefailedexec", "rebase.updateRefs"])
def test_plain_rebase_switches_pass(key):
    assert gitsafe.exec_risk([(L, key, "true")]) is None


def test_no_lazy_fetch_in_every_scrubbed_environment():
    assert gitsafe.gate_env({})["GIT_NO_LAZY_FETCH"] == "1"
    assert wtgit.git_env({})["GIT_NO_LAZY_FETCH"] == "1"
    # ops/health._git (the only isolated operations left since #565) and the gate share orch.gitsafe's isolation
    assert gitsafe.isolated_env({})["GIT_NO_LAZY_FETCH"] == "1"
    assert "protocol.allow=never" in gitsafe._argv("/", ["status"])


def test_a_status_submodule_summary_is_refused():
    """A summary opens submodules: no allowed key may change what an operation opens (#561)."""
    assert gitsafe.exec_risk([(L, "status.submoduleSummary", "true")]) == "status.submoduleSummary"


@pytest.mark.parametrize(
    "key", ["diff.external", "diff.x.textconv", "diff.x.command", "diff.submodule", "diff.relative"]
)
def test_the_gate_tolerates_every_diff_key_which_the_operations_neutralise(world, key):  # noqa: F811
    """orch-52 on #561: git-lfs and editors set ``diff.*``; the gate tolerates it (every diff operation pins it), while
    the shared allow-list (``exec_risk``, the records tool) still refuses the program-running keys."""
    root, repo, own, args = world
    _git(repo, "config", key, "/nonexistent/prog")
    gitsafe.gate(str(repo), own)
    runs_a_program = key in ("diff.external", "diff.x.textconv", "diff.x.command")
    assert gitsafe.exec_risk([(L, key, "x")]) == (key if runs_a_program else None)

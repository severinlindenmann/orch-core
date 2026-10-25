"""H1 L5 (design-h1-share-mirror): the agent user reads bare mirrors in ``/var/lib/orch/share/<person>/``, never the
person's home. Real git in a throwaway repository, an in-memory kernel for the ACL xattrs (``FakeKernel``); the
real-kernel version of the central claims is ``tests/setup/test_osapply_linux_root.py`` (Linux CI)."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from orch.host import sharemirror
from orch.host.columns import refusal, resolve
from orch.host.sharemirror import ShareError, SyncGate
from orch.setup.osapply import share_acl
from tests.host import sharemirror_helpers
from tests.host.sharemirror_helpers import (
    ACCESS,
    AGENT_UID,
    DEFAULT,
    TOKEN,
    git,
    mode,
    tree_bytes,
)

world = sharemirror_helpers.world  # the fixture


def test_the_agent_can_read_every_new_mirror_file_and_folder(world):
    """Linux cuts an inherited default ACL by the create mode: a folder made 0700 or a config made 0600 gives the agent
    nothing and its clone fails. Every folder must give r-x and every file r (the tmp folder before the rename too)."""
    os.umask(0)  # the kernel ignores the umask under a default ACL; what counts is the mode that was asked for
    final = Path(world.share.mirror_path(world.ws8, "proj"))
    seen: list[str] = []
    real = sharemirror._fetch
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sharemirror, "_fetch", lambda m, g: (seen.append(m), real(m, g))[1])
    try:
        sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    finally:
        monkey.undo()
    assert Path(seen[0]).name.startswith(".proj.tmp-")
    for path in [final, *final.rglob("*")]:
        want = 0o5 if path.is_dir() else 0o4
        got = world.k.agent_view(path, AGENT_UID)
        assert got & want == want, f"{path.relative_to(final)}: the agent would get {got:03b}"
    assert (final / "config").stat().st_mode & 0o777 == 0o640
    assert world.k.of(final, ACCESS) == sharemirror.rx_acl(AGENT_UID)  # set exactly, before the rename


# --- 2: the config bytes, empty hooks, no remote, no FETCH_HEAD, no token -----------------------------------


def test_the_mirror_has_exactly_our_config_empty_hooks_and_no_fetch_head(world):
    got = sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    assert got.created and set(got.heads) == {"refs/heads/main", "refs/tags/v1"}
    final = Path(world.share.mirror_path(world.ws8, "proj"))
    config = (final / "config").read_bytes()
    assert config == sharemirror.config_bytes(str(final))
    assert config == (
        b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n\tlogAllRefUpdates = false\n"
        + f"\thooksPath = {final}/hooks\n".encode()
        + b"[fetch]\n\tfsckObjects = true\n[gc]\n\tautoDetach = false\n"
    )
    assert b"remote" not in config and b"url" not in config and b"credential" not in config
    assert (final / "hooks").is_dir() and list((final / "hooks").iterdir()) == []
    assert not (final / "FETCH_HEAD").exists()
    assert not list(final.parent.glob(".*tmp*")), "the temporary folder is gone after the rename"
    # the repository's own config held a token URL: it is nowhere in the mirror, in no file at all
    assert TOKEN.encode() not in tree_bytes(final) and b"example.invalid" not in tree_bytes(final)
    assert git(final, "rev-parse", "HEAD") == git(
        world.repo, "rev-parse", "main"
    )  # HEAD follows the checked-out branch


def test_a_second_create_refreshes_the_same_mirror_and_hooks_stay_empty(world):
    sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    (world.repo / "b.txt").write_text("b\n")
    git(world.repo, "add", "b.txt")
    git(world.repo, "commit", "-q", "-m", "third")
    again = sharemirror.share_registered_repo(world.share, world.ws8, "proj", str(world.repo))
    assert again is not None and not again.created
    assert again.heads["refs/heads/main"] == git(world.repo, "rev-parse", "main")
    final = Path(world.share.mirror_path(world.ws8, "proj"))
    assert list((final / "hooks").iterdir()) == [] and not (final / "FETCH_HEAD").exists()


# --- 3: refs/stash and the working tree never get in --------------------------------------------------------


def test_the_blob_of_a_stash_holding_an_untracked_secret_is_not_in_the_mirror(world):
    stash_blob = git(world.repo, "rev-parse", "refs/stash^3:stashed.env")
    assert git(world.repo, "cat-file", "-e", stash_blob, check=False) == ""  # the source has it
    sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    final = world.share.mirror_path(world.ws8, "proj")
    probe = lambda sha: git(final, "cat-file", "-e", sha, check=False)  # noqa: E731
    import subprocess

    missing = subprocess.run(["git", "-C", final, "cat-file", "-e", stash_blob], capture_output=True).returncode
    assert missing != 0, "the stash's untracked blob reached the mirror"
    assert b"stash-only-secret" not in tree_bytes(Path(final)) and b"plain-env-secret" not in tree_bytes(Path(final))
    refs = git(final, "for-each-ref", "--format=%(refname)").splitlines()
    assert refs and all(r.startswith(("refs/heads/", "refs/tags/")) for r in refs), refs
    assert probe  # (the helper above is only for readability)


# --- 4: an integrity failure keeps the old mirror; creation is atomic ---------------------------------------


def _bad_commit(repo: Path, branch: str) -> None:
    """A branch that points at a commit git's fsck refuses (no author)."""
    import subprocess

    tree = git(repo, "rev-parse", "main^{tree}")
    out = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--literally", "-t", "commit", "--stdin"],
        input=f"tree {tree}\n\nno author line\n", capture_output=True, text=True, check=True,
        env={"PATH": os.environ["PATH"], "HOME": str(repo.parent)},
    ).stdout.strip()  # fmt: skip
    git(repo, "update-ref", f"refs/heads/{branch}", out)


def test_an_fsck_failure_keeps_the_old_mirror_and_says_so_plainly(world):
    sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    final = world.share.mirror_path(world.ws8, "proj")
    before = git(final, "for-each-ref", "--format=%(refname) %(objectname)")
    (world.repo / "c.txt").write_text("c\n")
    git(world.repo, "add", "c.txt")
    git(world.repo, "commit", "-q", "-m", "good new commit")
    _bad_commit(world.repo, "broken")
    with pytest.raises(ShareError, match="integrity check"):
        sharemirror.sync_mirror(world.share, world.ws8, "proj")
    assert git(final, "for-each-ref", "--format=%(refname) %(objectname)") == before  # not even the good commit
    assert not os.path.lexists(f"{final}/FETCH_HEAD")


def test_a_failed_creation_leaves_no_mirror_and_no_temporary_folder(world):
    _bad_commit(world.repo, "broken")
    with pytest.raises(ShareError, match="integrity check"):
        sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    parent = Path(world.share.mirrors) / world.ws8
    assert list(parent.iterdir()) == []  # neither proj.git nor a .proj.tmp-* folder
    assert sharemirror._registry(world.share) == {}


def test_the_mirror_appears_complete_or_not_at_all(world, monkeypatch):
    """The final name is taken by one rename, after the fetch: while git runs, only the temporary name exists."""
    final = world.share.mirror_path(world.ws8, "proj")
    seen: list[bool] = []
    real = sharemirror._fetch

    def spy(mirror, gitdir):
        seen.append(os.path.lexists(final))
        assert Path(mirror).name.startswith(".proj.tmp-")
        real(mirror, gitdir)

    monkeypatch.setattr(sharemirror, "_fetch", spy)
    sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    assert seen == [False] and os.path.isdir(final)


def test_a_repository_whose_own_config_runs_programs_is_refused_before_git_runs(world):
    git(world.repo, "config", "core.alternateRefsCommand", "touch /tmp/pwned")
    with pytest.raises(ShareError) as refused:
        sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    assert str(refused.value) == "This repository's settings can make git run programs, so orch will not copy it."
    assert "core.alternaterefscommand" in refused.value.details and "allow-list" in refused.value.details
    assert not os.path.lexists(world.share.mirror_path(world.ws8, "proj"))


# --- 5: mirror.sync: one per 30 s for G, coalesced, the agent allowed ---------------------------------------


def test_column_g_and_p_may_sync_and_n_may_not():
    p = resolve("person", "absent", True)
    g = resolve("agent", "verified", True)
    n = resolve("agent", "absent", True)
    assert [refusal(c, "pg_write", "mirror.sync") for c in (p, g, n)] == [None, None, "op.not_allowed"]


def test_the_gate_limits_g_to_one_run_per_interval_and_never_limits_p():
    now = [100.0]
    gate = SyncGate(30.0, clock=lambda: now[0])
    runs: list[int] = []

    def work():
        runs.append(1)
        return {"n": len(runs)}

    assert gate.run("k", work, limited=True) == ({"n": 1}, True)
    now[0] += 29.9
    assert gate.run("k", work, limited=True) == ({"n": 1}, False)  # the last answer, no new run
    assert gate.run("k", work, limited=False) == ({"n": 2}, True)  # the person is not limited
    now[0] += 29.9
    assert gate.run("k", work, limited=True) == ({"n": 2}, False)  # the person's run counts as the last one
    now[0] += 0.2
    assert gate.run("k", work, limited=True) == ({"n": 3}, True)
    assert gate.run("other", work, limited=True)[1] is True  # another repository has its own window
    assert len(runs) == 4


def test_calls_that_arrive_while_a_sync_runs_are_coalesced_into_it():
    gate = SyncGate(30.0)
    started, release = threading.Event(), threading.Event()
    runs: list[int] = []

    def slow():
        runs.append(1)
        started.set()
        release.wait(5)
        return "heads"

    answers: list[tuple] = []
    first = threading.Thread(target=lambda: answers.append(gate.run("k", slow, limited=False)))
    first.start()
    assert started.wait(5)
    waiters = [
        threading.Thread(target=lambda lim=lim: answers.append(gate.run("k", slow, limited=lim)))
        for lim in (True, False)
    ]
    for t in waiters:
        t.start()
    release.set()
    for t in [first, *waiters]:
        t.join(5)
    assert len(runs) == 1, "one git run for three callers"
    assert sorted(a[1] for a in answers) == [False, False, True] and {a[0] for a in answers} == {"heads"}


def test_a_failed_sync_counts_toward_the_limit_for_g_but_not_for_the_person():
    now = [0.0]
    gate = SyncGate(30.0, clock=lambda: now[0])
    runs: list[int] = []

    def boom():
        runs.append(1)
        raise ShareError("no")

    with pytest.raises(ShareError):
        gate.run("k", boom, limited=True)
    with pytest.raises(ShareError):  # the agent asks again inside the window: the same error, git does not run again
        gate.run("k", boom, limited=True)
    assert len(runs) == 1
    assert gate.run("k", lambda: "ok", limited=False) == ("ok", True)  # the person may always try
    now[0] += 31
    with pytest.raises(ShareError):
        gate.run("k2", boom, limited=True)
    assert len(runs) == 2


# --- 6: unregister deletes the mirror -----------------------------------------------------------------------


def test_unregister_removes_the_mirror_its_record_and_leaves_no_trash(world):
    sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    other = world.repo.parent / "other"
    (other).mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "x").write_text("x")
    git(other, "add", "x")
    git(other, "commit", "-q", "-m", "x")
    sharemirror.create_mirror(world.share, world.ws8, "other", str(other))
    assert sharemirror.delete_mirror(world.share, world.ws8, "proj") is True
    parent = Path(world.share.mirrors) / world.ws8
    assert sorted(p.name for p in parent.iterdir()) == ["other.git"]
    assert set(sharemirror._registry(world.share)) == {f"{world.ws8}/other"}
    assert sharemirror.delete_mirror(world.share, world.ws8, "proj") is False  # nothing left to delete
    assert world.repo.is_dir() and (world.repo / ".env").is_file()  # the person's own repository is untouched


# --- 7: the kernel: ACLs only inside the share, exact, read back --------------------------------------------


def test_acls_are_set_only_inside_the_share_and_are_exactly_the_agents_read_access(world):
    sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    interp = world.root / "python" / "cpython-3.12-test" / "bin"
    interp.mkdir(parents=True)
    (interp / "python3").write_text("#!/bin/sh\n")
    src = world.base / "orch-src" / "orch"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    sharemirror.install_release_copy(world.share, source=src)
    inside = {(s.st_dev, s.st_ino) for p in [world.root, *world.root.rglob("*")] for s in [os.lstat(p)]}
    assert world.k.sets, "nothing was set"
    assert {(dev, ino) for dev, ino, _ in world.k.sets} <= inside  # never the home, the repo, the state folder
    want = sharemirror.rx_acl(AGENT_UID)
    for rel in ("mirrors", f"mirrors/{world.ws8}", "releases", "python"):
        assert world.k.of(world.root / rel, ACCESS) == want and world.k.of(world.root / rel, DEFAULT) == want, rel
    assert world.k.of(world.root, ACCESS) is None  # the share folder itself is root's step, not ours
    assert world.k.of(world.home, ACCESS) is None and world.k.of(world.repo, ACCESS) is None


def test_the_acl_encoding_is_the_exact_setup_and_agent_entries():
    import struct

    entries = [struct.unpack_from("<HHI", share_acl(AGENT_UID), 4 + 8 * i) for i in range(5)]
    assert entries == [
        (1, 7, 0xFFFFFFFF),
        (2, 1, AGENT_UID),
        (4, 0, 0xFFFFFFFF),
        (0x10, 1, 0xFFFFFFFF),
        (0x20, 0, 0xFFFFFFFF),
    ]
    rx = [struct.unpack_from("<HHI", sharemirror.rx_acl(AGENT_UID), 4 + 8 * i) for i in range(5)]
    assert rx == [
        (1, 7, 0xFFFFFFFF),
        (2, 5, AGENT_UID),
        (4, 0, 0xFFFFFFFF),
        (0x10, 5, 0xFFFFFFFF),
        (0x20, 0, 0xFFFFFFFF),
    ]


def test_an_acl_that_reads_back_differently_is_a_refusal(tmp_path, monkeypatch):
    from tests.host.sharemirror_helpers import make_share, restore

    ns = make_share(tmp_path, monkeypatch, drop_sets=True)  # the file system silently ignores the set
    try:
        with pytest.raises(ShareError, match="read back"):
            sharemirror.create_mirror(ns.share, ns.ws8, "proj", str(ns.repo))
    finally:
        restore(ns)


def test_a_symlinked_mirrors_folder_or_share_is_refused_not_followed(world):
    elsewhere = world.base / "elsewhere"
    elsewhere.mkdir()
    (world.root / "mirrors").symlink_to(elsewhere)
    with pytest.raises(ShareError, match="link"):
        sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    assert list(elsewhere.iterdir()) == []
    link = world.base / "share" / "bob"
    link.symlink_to(world.root)
    other = sharemirror.Share("bob", str(link), world.share.state, AGENT_UID)
    with pytest.raises(ShareError, match="safely"):
        sharemirror.create_mirror(other, world.ws8, "proj", str(world.repo))


def test_a_share_folder_of_someone_else_is_refused(world, monkeypatch):
    me = os.geteuid()
    monkeypatch.setattr(sharemirror.os, "geteuid", lambda: me + 1)
    with pytest.raises(ShareError, match="someone else"):
        sharemirror._open_share(world.share)


# --- 8: the doctor: one drift case per check ----------------------------------------------------------------


def _doctor(world) -> dict[str, str]:
    return {r.code: r.kind for r in sharemirror.check(world.share)}


def _final(world) -> Path:
    return Path(world.share.mirror_path(world.ws8, "proj"))


@pytest.fixture
def shared(world):
    sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    return world


def test_a_fresh_mirror_has_nothing_to_report(shared):
    assert _doctor(shared) == {"release_missing": "availability"}  # only the release is not copied in yet


def test_drift_the_config_is_not_ours(shared):
    cfg = _final(shared) / "config"
    cfg.write_bytes(cfg.read_bytes() + b'[remote "origin"]\n\turl = https://x:y@h/p\n')
    assert _doctor(shared)["mirror_config"] == "exposure"


def test_drift_a_hook_file(shared):
    (_final(shared) / "hooks" / "post-update").write_text("#!/bin/sh\n")
    assert _doctor(shared)["mirror_hooks"] == "exposure"


def test_drift_a_ref_besides_heads_and_tags(shared):
    (_final(shared) / "refs" / "stash").write_text("0" * 40 + "\n")
    assert _doctor(shared)["mirror_refs"] == "exposure"


def test_drift_a_packed_ref_besides_heads_and_tags(shared):
    (_final(shared) / "packed-refs").write_text("# pack-refs\n" + "a" * 40 + " refs/stash\n")
    assert _doctor(shared)["mirror_refs"] == "exposure"


def test_drift_a_fetch_head(shared):
    (_final(shared) / "FETCH_HEAD").write_text(f"{'a' * 40}\t\t{shared.repo}\n")
    assert _doctor(shared)["mirror_fetch_head"] == "exposure"


def test_drift_a_symlink_in_the_mirror(shared):
    (_final(shared) / "objects" / "leak").symlink_to(shared.home)
    assert _doctor(shared)["mirror_exposed"] == "exposure"


def test_drift_a_file_the_group_or_others_can_read(shared):
    path = _final(shared) / "HEAD"
    path.chmod(0o644)  # no ACL on it (the fake kernel): the person's group could read it
    assert _doctor(shared)["mirror_exposed"] == "exposure"


def test_drift_an_acl_entry_for_someone_else_or_more_than_read(shared):
    import struct

    from orch.setup.osapply import GROUP_OBJ, MASK, OTHER, USER, USER_OBJ, acl

    wider = acl(((USER_OBJ, 6), (USER, 6, AGENT_UID), (GROUP_OBJ, 0), (MASK, 6), (OTHER, 0)))
    other = acl(((USER_OBJ, 6), (USER, 4, AGENT_UID + 1), (GROUP_OBJ, 0), (MASK, 4), (OTHER, 0)))
    head = _final(shared) / "HEAD"
    fd = os.open(head, os.O_RDONLY)
    try:
        for blob in (wider, other):
            shared.k.store[(*(lambda s: (s.st_dev, s.st_ino))(os.fstat(fd)), ACCESS)] = blob
            assert _doctor(shared)["mirror_exposed"] == "exposure"
    finally:
        os.close(fd)
    assert struct  # (kept for the readers of the encoding above)


def test_drift_a_mirror_owned_by_someone_else(shared, monkeypatch):
    known = sharemirror._registry(shared.share)
    monkeypatch.setattr(sharemirror, "_registry", lambda share: known)
    monkeypatch.setattr(sharemirror, "_mine", lambda fd, where: None)  # the share folder itself passes ...
    me = os.geteuid()
    monkeypatch.setattr(sharemirror.os, "geteuid", lambda: me + 1)  # ... but its content is not ours
    assert _doctor(shared)["mirror_exposed"] == "exposure"


def test_a_stale_mirror_is_availability_only_and_a_missing_one_too(shared):
    (shared.repo / "n.txt").write_text("n\n")
    git(shared.repo, "add", "n.txt")
    git(shared.repo, "commit", "-q", "-m", "ahead")
    assert _doctor(shared)["mirror_stale"] == "availability"
    sharemirror.sync_mirror(shared.share, shared.ws8, "proj")
    assert "mirror_stale" not in _doctor(shared)
    import shutil

    shutil.rmtree(_final(shared))
    assert _doctor(shared)["mirror_missing"] == "availability"


def _install_fake_release(world):
    interp = world.root / "python" / "cpython-3.12-test" / "bin"
    interp.mkdir(parents=True)
    (interp / "python3").write_text("#!/bin/sh\n")
    (interp / "python3").chmod(0o755)
    src = world.base / "orch-src" / "orch"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    return sharemirror.install_release_copy(world.share, source=src), str(interp / "python3")


def test_the_release_names_the_share_interpreter_and_is_not_writable(shared):
    final, python = _install_fake_release(shared)
    assert (shared.root / "releases" / "current").resolve() == final.resolve()
    assert (final / "bin" / "orch-runner").read_text().startswith(f"#!{python} -I\n")
    assert _doctor(shared) == {}
    os.chmod(final / "orch" / "__init__.py", 0o664)  # the group could write the program the agent runs
    assert _doctor(shared)["release_writable"] == "exposure"


def test_the_release_needs_an_interpreter_and_installing_one_uses_the_share_python_dir(shared):
    with pytest.raises(ShareError, match="not installed"):
        sharemirror.install_release_copy(shared.share)
    calls: list[tuple] = []

    class Done:
        returncode = 0

    def fake_uv(argv, **kw):
        calls.append((argv, kw["env"]))
        exe = shared.root / "python" / "cpython-3.12.1-linux" / "bin"
        exe.mkdir(parents=True)
        (exe / "python3").write_text("#!/bin/sh\n")
        return Done()

    python = sharemirror.install_interpreter(shared.share, "3.12", run=fake_uv, uv="/usr/bin/uv")
    assert python == str(shared.root / "python" / "cpython-3.12.1-linux" / "bin" / "python3")
    argv, env = calls[0]
    assert argv == ["/usr/bin/uv", "python", "install", "3.12"] and env["UV_PYTHON_INSTALL_DIR"] == shared.share.python
    with pytest.raises(ShareError, match="cannot be installed"):
        sharemirror.install_interpreter(shared.share, "3.12; rm -rf /", run=fake_uv)


# --- 10: tier A and macOS do nothing ------------------------------------------------------------------------


def test_tier_a_and_macos_have_no_share_and_do_nothing(tmp_path, monkeypatch):
    assert sharemirror.share_for("ann", tmp_path, platform="darwin") is None
    assert sharemirror.share_for("ann", tmp_path, platform="linux") is None  # no orch-agent-ann user here
    assert sharemirror.share_for("not a name!", tmp_path, platform="linux") is None
    assert sharemirror.share_registered_repo(None, "0123abcd", "proj", "/x") is None
    assert sharemirror.status_text(None).text() == "Agents run as you; nothing is shared."
    assert sharemirror.repair(None).text() == "Agents run as you; nothing is shared."
    called: list[tuple] = []
    monkeypatch.setattr(sharemirror, "_setx", lambda *a: called.append(a))
    assert not called


def test_the_registration_review_line_is_the_one_in_the_design():
    assert sharemirror.REVIEW_LINE == (
        "Your agents get a read-only copy of this repository's branches. "
        "Your working folder and uncommitted files stay private."
    )


def test_orch_share_cli_refuses_an_agent_and_root_and_reports_tier_a(monkeypatch):
    import io

    from orch.cli import share as cli

    err = io.StringIO()
    assert cli.main(["status"], env={"ORCH_GRANT": "gr_x"}, out=io.StringIO(), err=err) == 3
    assert "never changes it" in err.getvalue()
    out = io.StringIO()
    monkeypatch.setattr(cli, "_share", lambda env: None)
    assert cli.main(["status"], env={}, out=out, err=io.StringIO()) == 0
    assert out.getvalue() == "Agents run as you; nothing is shared.\n"
    out = io.StringIO()
    assert cli.main(["repair"], env={}, out=out, err=io.StringIO()) == 0
    assert "nothing is shared" in out.getvalue()
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    assert cli.main(["status"], env={}, out=io.StringIO(), err=err) == 3


def test_orch_share_status_and_repair_on_a_share(world, monkeypatch):
    import io
    import shutil

    from orch.cli import share as cli

    def no_python(share, *a, **k):
        raise ShareError("no uv here")

    monkeypatch.setattr(sharemirror, "install_interpreter", no_python)  # never the real uv, never the network
    monkeypatch.setattr(cli, "_share", lambda env: world.share)
    sharemirror.create_mirror(world.share, world.ws8, "proj", str(world.repo))
    shutil.rmtree(_final(world))
    out = io.StringIO()
    assert cli.main(["repair"], env={}, out=out, err=io.StringIO()) == 3  # exit 3: the release part could not be done
    assert f"made  {world.ws8}/proj" in out.getvalue() and _final(world).is_dir()
    assert "program for your agents: no uv here" in out.getvalue()
    out = io.StringIO()
    assert cli.main(["status"], env={}, out=out, err=io.StringIO()) == 0  # only an availability note: not copied yet
    assert "1 repositories" in out.getvalue() and "not copied in yet" in out.getvalue()
    assert mode(_final(world)) & 0o007 == 0 and stat.S_ISDIR(os.lstat(_final(world)).st_mode)


# --- the venv of the release: built by the person from a lock, before the freeze ------------------------------


def test_the_release_venv_is_built_as_the_person_from_the_lock_with_hashes_and_wheels_only(shared):
    interp = shared.root / "python" / "cpython-3.12-test" / "bin"
    interp.mkdir(parents=True)
    (interp / "python3").write_text("#!/bin/sh\n")
    src = shared.base / "orch-src" / "orch"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    lock = shared.base / "requirements.lock"
    lock.write_text("jsonschema==4.0.0 \\\n    --hash=sha256:" + "a" * 64 + "\n")
    calls: list[tuple[list[str], dict]] = []

    class Done:
        returncode = 0
        stderr = ""

    def fake_uv(argv, **kw):
        calls.append((argv, kw["env"]))
        if argv[1] == "venv":  # what uv would make; the release is still writable here (before the freeze)
            bin_dir = Path(argv[-1]) / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n")
        return Done()

    final = sharemirror.install_release_copy(shared.share, source=src, lock=lock, run=fake_uv)
    (venv, install) = [c[0] for c in calls]
    assert venv[:5] == ["uv", "--no-config", "venv", "--no-python-downloads", "--python"]
    assert venv[5] == str(interp / "python3")
    assert install[:4] == ["uv", "--no-config", "pip", "install"]
    assert all(env["UV_LINK_MODE"] == "copy" for _argv, env in calls)  # the person's cache is never hardlinked in
    for flag in ("--require-hashes", "--only-binary", ":all:", "-r"):
        assert flag in install
    assert install[install.index("-r") + 1].endswith("/requirements.lock")
    assert all(env["UV_PYTHON_INSTALL_DIR"] == shared.share.python for _argv, env in calls)
    import hashlib
    import json

    assert (
        json.loads((final / "RELEASE.json").read_text())["lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    )
    assert (final / "bin" / "orch-runner").read_text().startswith(f"#!{final}/venv/bin/python -I\n")
    assert (final / "requirements.lock").read_bytes() == lock.read_bytes()
    assert _doctor(shared) == {}
    # another lock is another release (the digest is part of the version); the same lock is the same release
    lock.write_text(lock.read_text() + "# changed\n")
    other = sharemirror.install_release_copy(shared.share, source=src, lock=lock, run=fake_uv)
    assert other != final


def test_a_failing_uv_gives_a_plain_sentence_with_the_output_under_details(shared):
    interp = shared.root / "python" / "cpython-3.12-test" / "bin"
    interp.mkdir(parents=True)
    (interp / "python3").write_text("#!/bin/sh\n")
    src = shared.base / "orch-src" / "orch"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    lock = shared.base / "requirements.lock"
    lock.write_text("x\n")

    class Bad:
        returncode = 2
        stderr = "hash mismatch for jsonschema"

    with pytest.raises(ShareError) as bad:
        sharemirror.install_release_copy(shared.share, source=src, lock=lock, run=lambda *a, **k: Bad())
    assert str(bad.value) == "The program for your agents could not be set up with its libraries."
    assert "hash mismatch" in bad.value.details
    assert not list((shared.root / "releases").glob(".staging-*")), "a failed build leaves no half release"

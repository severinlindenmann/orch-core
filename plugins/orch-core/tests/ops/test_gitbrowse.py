"""The File Explorer's git (``orch.gitbrowse``, issue #491): a second closed set of read-only operations on top of
``orch.gitsafe``. Real git, real repositories; a hostile ``HOME`` must start no program, a hostile repository config is
refused by the gate, and no operand can become an option."""

from __future__ import annotations

import os
import subprocess

import pytest

from orch import gitbrowse, gitsafe

DATE = "2026-01-0{n}T10:00:00+00:00"
IDENT = ("-c", "user.name=Ann Author", "-c", "user.email=ann@example.invalid")


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for k in list(os.environ):
        if k.startswith("GIT_") or k == "XDG_CONFIG_HOME":
            monkeypatch.delenv(k)
    monkeypatch.setenv("HOME", str(home))
    w = type("W", (), {})()
    w.tmp, w.home, w.mark = tmp_path, home, tmp_path / "mark"
    w.mark.mkdir()

    def git(repo, *args, n=1):
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_DATE": DATE.format(n=n), "GIT_COMMITTER_DATE": DATE.format(n=n)}  # fmt: skip
        return subprocess.run(["git", *IDENT, "-C", str(repo), *args], env=env, check=True, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL).stdout.strip()  # fmt: skip

    w.git = git
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    (repo / "dir").mkdir()
    (repo / "dir" / "ünï code.txt").write_text("alpha\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first: add files\n\nbody line", n=1)
    w.c1 = git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("one\ntwo\n")
    git(repo, "commit", "-q", "-am", "second: change a", n=2)
    w.c2 = git(repo, "rev-parse", "HEAD")
    (repo / "dir" / "ünï code.txt").write_text("alpha\nbeta needle\n")
    git(repo, "commit", "-q", "-am", "third: change unicode file", n=3)
    w.c3 = git(repo, "rev-parse", "HEAD")
    w.repo = repo
    w.run = lambda op, *operand, **kw: gitbrowse.run(str(repo), op, os.environ, operand=operand, **kw)

    def fired():
        return {p.name for p in w.mark.iterdir()}

    w.fired = fired
    return w


def test_file_log_lists_only_the_commits_of_that_file_newest_first(world):
    res = world.run("file_log", "50", "0", "HEAD", "a.txt")
    assert res.code == 0 and not res.cut
    rows = [r.split("\x1f") for r in res.out.decode().split("\x1e") if r.strip()]
    assert [r[0] for r in rows] == [world.c2, world.c1]
    assert (
        rows[0][1] == "Ann Author" and rows[0][2].startswith("2026-01-02T10:00:00") and rows[0][3].startswith("second")
    )


def test_file_log_paging_and_unicode_paths(world):
    res = world.run("file_log", "1", "1", "HEAD", "dir/ünï code.txt")
    rows = [r for r in res.out.decode().split("\x1e") if r.strip()]
    assert len(rows) == 1 and rows[0].startswith(world.c1)


def test_the_path_is_literal_never_a_pathspec(world):
    (world.repo / "[ab].txt").write_text("x\n")
    world.git(world.repo, "add", "-A")
    world.git(world.repo, "commit", "-q", "-m", "glob", n=4)
    got = world.run("file_log", "50", "0", "HEAD", "[ab].txt").out.decode()
    assert got.count("\x1e") == 1
    assert world.run("file_log", "50", "0", "HEAD", "*.txt").out == b""
    assert world.run("file_log", "50", "0", "HEAD", ":!a.txt").out == b""


def test_commit_file_patch_has_the_record_then_only_that_files_diff(world):
    res = world.run("commit_file_patch", "3", world.c3, "dir/ünï code.txt")
    text = res.out.decode()
    head, _, rest = text.lstrip("\x1e").partition("\x1e")
    assert head.split("\x1f")[0] == world.c3 and "+beta needle" in rest and "a.txt" not in rest
    # the root commit has a diff too (everything added)
    first = world.run("commit_file_patch", "3", world.c1, "a.txt").out.decode()
    assert "+one" in first and "body line" not in first.split("\x1e")[2]
    assert "body line" in first.split("\x1e")[1]


def test_file_diff_between_two_versions(world):
    res = world.run("file_diff", "3", world.c1, world.c3, "dir/ünï code.txt")
    assert "+beta needle" in res.out.decode()


def test_blob_at_a_revision_and_its_size(world):
    assert world.run("blob_size", world.c1, "a.txt").out.strip() == b"4"
    assert world.run("blob", world.c1, "a.txt").out == b"one\n"
    assert world.run("blob", world.c1, "nope.txt").code != 0


def test_output_is_cut_at_max_bytes_and_the_group_is_killed(world):
    big = world.repo / "big.txt"
    big.write_text("x" * 300000 + "\n")
    world.git(world.repo, "add", "-A")
    world.git(world.repo, "commit", "-q", "-m", "big", n=5)
    res = world.run("blob", "HEAD", "big.txt", max_bytes=1000)
    assert res.cut and len(res.out) == 1000


def test_grep_is_literal_and_reports_path_line_text(world):
    res = world.run("grep", "needle")
    assert res.code == 0
    fields = res.out.split(b"\0")
    assert fields[0] == "dir/ünï code.txt".encode() and fields[1] == b"2" and fields[2].startswith(b"beta needle")
    assert world.run("grep", "n.*dle").code == 1  # a regex is just text here
    assert world.run("grep", "-e").code == 1  # an option-looking search is just text


def test_commit_names_lists_the_files_of_a_commit(world):
    out = world.run("commit_names", world.c1).out.split(b"\0")
    assert out[:2] == [b"A", b"a.txt"] and b"A" in out


@pytest.mark.parametrize(
    "op,operand",
    [
        ("file_log", ("50", "0", "-p", "a.txt")),
        ("file_log", ("50", "0", "--output=/tmp/x", "a.txt")),
        ("file_log", ("50", "0", "HEAD", "../a.txt")),
        ("file_log", ("50", "0", "HEAD", "/etc/passwd")),
        ("file_log", ("50", "0", "HEAD", "a/./b")),
        ("file_log", ("50", "0", "HEAD", "a//b")),
        ("file_log", ("50", "0", "HEAD", "a\nb")),
        ("file_log", ("-1", "0", "HEAD", "a")),
        ("file_log", ("50", "x", "HEAD", "a")),
        ("file_log", ("50", "0", "HEAD~1", "a")),
        ("file_log", ("50", "0", "main", "a")),
        ("file_log", ("50", "0", "HEAD")),
        ("file_diff", ("3", "HEAD..HEAD", "HEAD", "a")),
        ("blob", ("HEAD", "--x/../y")),
        ("blob", ("-p", "a")),
        ("grep", ("a\nb",)),
        ("grep", ("",)),
        ("grep", ("x" * 201,)),
        ("nope", ()),
    ],
)
def test_no_operand_can_become_an_option_or_leave_the_repo(world, op, operand):
    with pytest.raises(gitsafe.Unsafe):
        gitbrowse.command(str(world.repo), op, os.environ, operand=operand)


def test_every_operation_has_fixed_arguments_with_end_of_options_and_separators():
    for name, spec in gitbrowse.OPS.items():
        args = spec.args
        if "<rev>" in "".join(args) and ":" not in "".join(a for a in args if "<rev>" in a):
            assert "--end-of-options" in args, name
        if "<file>" in "".join(args) and args[0] in ("log", "show", "diff"):
            assert args[args.index("--") + 1].startswith(":(literal)"), name
        assert args[0] in {"log", "show", "diff", "cat-file", "grep"}


def test_a_hostile_global_config_starts_no_program(world):
    mark = world.mark

    def prog(name):
        p = world.tmp / name
        p.write_text(f'#!/bin/sh\necho ran >> "{mark / name}"\nexit 0\n')
        p.chmod(0o755)
        return str(p)

    (world.home / ".gitconfig").write_text(
        f"[core]\n\tpager = {prog('pager')}\n\tfsmonitor = {prog('fsm')}\n\tattributesFile = {world.home / 'attrs'}\n"
        f"[pager]\n\tlog = {prog('pager-log')}\n\tshow = {prog('pager-show')}\n"
        f'[diff]\n\texternal = {prog("ext")}\n[diff "evil"]\n\ttextconv = {prog("textconv")}\n'
        f'[filter "evil"]\n\tclean = {prog("clean")}\n\tsmudge = {prog("smudge")}\n'
        f"[alias]\n\tshow = !{prog('alias')}\n"
    )
    (world.home / "attrs").write_text("*.txt diff=evil filter=evil\n")
    (world.repo / ".gitattributes").write_text("*.txt diff=evil filter=evil\n")
    w = world
    ran = [
        w.run("file_log", "50", "0", "HEAD", "a.txt"),
        w.run("commit_file_patch", "3", w.c2, "a.txt"),
        w.run("file_diff", "3", w.c1, w.c2, "a.txt"),
        w.run("blob", w.c2, "a.txt"),
        w.run("grep", "two"),
        w.run("commit_names", w.c2),
    ]
    assert all(r.code == 0 for r in ran), [r.code for r in ran]
    assert "+two" in ran[1].out.decode() and "+two" in ran[2].out.decode()
    assert w.fired() == set()
    # positive control: plain git with the same HOME does run them
    plain = subprocess.run(["git", "-C", str(w.repo), "diff", w.c1, w.c2, "--", "a.txt"],
                           env={"HOME": str(w.home), "PATH": os.environ["PATH"]}, capture_output=True)  # fmt: skip
    assert plain.returncode == 0 and {"ext"} <= w.fired()


def test_a_repository_config_that_names_a_program_is_refused_by_the_gate(world):
    world.git(world.repo, "config", "core.sshCommand", "/bin/evil")
    for op, operand in (("file_log", ("50", "0", "HEAD", "a.txt")), ("grep", ("one",)), ("blob", ("HEAD", "a.txt"))):
        with pytest.raises(gitsafe.Unsafe):
            gitbrowse.command(str(world.repo), op, os.environ, operand=operand)


def test_a_repository_hook_and_filter_never_run(world):
    hook = world.repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f'#!/bin/sh\necho ran >> "{world.mark / "hook"}"\n')
    hook.chmod(0o755)
    world.git(world.repo, "config", "core.hooksPath", str(world.repo / ".git" / "hooks"))
    world.run("file_log", "50", "0", "HEAD", "a.txt")
    world.run("grep", "one")
    assert world.fired() == set()

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
    rows = gitbrowse.parse_log(res.out)
    assert [r[0] for r in rows] == [world.c2, world.c1]
    assert (
        rows[0][1] == "Ann Author" and rows[0][2].startswith("2026-01-02T10:00:00") and rows[0][3].startswith("second")
    )


def test_file_log_paging_and_unicode_paths(world):
    res = world.run("file_log", "1", "1", "HEAD", "dir/ünï code.txt")
    rows = gitbrowse.parse_log(res.out)
    assert len(rows) == 1 and rows[0][0] == world.c1


def test_the_path_is_literal_never_a_pathspec(world):
    (world.repo / "[ab].txt").write_text("x\n")
    world.git(world.repo, "add", "-A")
    world.git(world.repo, "commit", "-q", "-m", "glob", n=4)
    got = world.run("file_log", "50", "0", "HEAD", "[ab].txt").out.decode()
    assert len(gitbrowse.parse_log(got.encode())) == 1
    assert world.run("file_log", "50", "0", "HEAD", "*.txt").out == b""
    assert world.run("file_log", "50", "0", "HEAD", ":!a.txt").out == b""


def test_commit_file_patch_has_the_record_then_only_that_files_diff(world):
    res = world.run("commit_file_patch", "3", world.c3, "dir/ünï code.txt")
    text = res.out.decode()
    (sha, who, when, subject, body), rest = gitbrowse.parse_show(res.out)
    assert sha == world.c3 and "+beta needle" in rest and "a.txt" not in rest and text
    # the root commit has a diff too (everything added)
    record, patch = gitbrowse.parse_show(world.run("commit_file_patch", "3", world.c1, "a.txt").out)
    assert "+one" in patch and "body line" not in patch and record[4] == "body line"


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
    assert gitbrowse.parse_grep(res.out) == [("dir/ünï code.txt", 2, "beta needle")]
    assert world.run("grep", "n.*dle").code == 1  # a regex is just text here
    assert world.run("grep", "-e").code == 1  # an option-looking search is just text


def test_commit_names_lists_the_files_of_a_commit(world):
    assert gitbrowse.parse_names(world.run("commit_names", world.c1).out) == [
        ("A", "a.txt"),
        ("A", "dir/ünï code.txt"),
    ]
    assert gitbrowse.parse_names(world.run("range_names", world.c1, world.c3).out) == [
        ("M", "a.txt"),
        ("M", "dir/ünï code.txt"),
    ]


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
        assert "<rev>" not in "".join(args), name  # only <commit>: resolved as a commit, never a tree or a blob
        if "<commit>" in "".join(args) and ":" not in "".join(a for a in args if "<commit>" in a):
            assert "--end-of-options" in args, name
        if "<file>" in "".join(args) and args[0] in ("log", "show", "diff"):
            assert args[args.index("--") + 1].startswith(":(literal)"), name
        assert args[0] in {"log", "show", "diff", "cat-file", "grep", "rev-parse"}


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


def test_the_deny_list_is_excluded_by_git_itself_on_every_pathspec_operation(world):
    """On top of the dashboard's own check: a caller that forgot to check a path still gets nothing denied."""
    r = world.repo
    for name, text in {
        ".env": "SECRET=needle",
        ".env.local": "needle",
        "deploy/key.pem": "needle",
        "id_ed25519": "needle",
        ".orch/config.json": "needle",
        "events/e1.json": "needle",
        "sub/.git-notes/x.txt": "needle ok",
        "keys.jsonl": "needle",
        "ssh/id_rsa.work": "needle",
    }.items():
        (r / name).parent.mkdir(parents=True, exist_ok=True)
        (r / name).write_text(text + "\n")
    world.git(r, "add", "-f", "-A")
    world.git(r, "commit", "-q", "-m", "secrets", n=6)
    head = world.git(r, "rev-parse", "HEAD")

    def paths(out):
        return sorted(p for p, _l, _t in gitbrowse.parse_grep(out))

    assert paths(world.run("grep", "needle").out) == ["dir/ünï code.txt", "events/e1.json", "sub/.git-notes/x.txt"]
    hits = gitbrowse.run(str(r), "grep", os.environ, operand=("needle",), exclude_top=("events",))
    assert paths(hits.out) == ["dir/ünï code.txt", "sub/.git-notes/x.txt"]
    names = [p for _s, p in gitbrowse.parse_names(world.run("commit_names", head).out)]
    assert names == ["events/e1.json", "sub/.git-notes/x.txt"]
    names = [p for _s, p in gitbrowse.parse_names(world.run("range_names", world.c3, head).out)]
    assert names == ["events/e1.json", "sub/.git-notes/x.txt"]
    # a denied file asked for by name has no history, no patch, no diff
    for denied in (".env", "deploy/key.pem", ".orch/config.json"):
        assert world.run("file_log", "50", "0", "HEAD", denied).out == b""
        assert world.run("commit_file_patch", "3", head, denied).out.split(b"\0")[-1].strip() == b""
        assert world.run("file_diff", "3", world.c3, head, denied).out == b""


def test_a_bad_top_exclusion_is_refused(world):
    for bad in ("a/b", ":(x)", "", "a b", "x" * 101):
        with pytest.raises(gitsafe.Unsafe):
            gitbrowse.command(str(world.repo), "grep", os.environ, operand=("x",), exclude_top=(bad,))


def test_grep_reads_the_index_not_the_working_tree_and_never_through_a_symlink(world):
    secret = world.tmp / "outside-secret.txt"
    secret.write_text("TOPSECRET needle\n")
    (world.repo / "link.txt").symlink_to(secret)
    (world.repo / "dirlink").symlink_to(world.tmp)
    world.git(world.repo, "add", "link.txt", "dirlink")
    (world.repo / "dir" / "ünï code.txt").write_text("changed in the work tree only: needle3\n")
    got = world.run("grep", "needle")
    text = got.out.decode()
    assert "TOPSECRET" not in text and "needle3" not in text  # not through the link, not the unsaved work tree
    # git does not even search the link's own text (a path)
    assert world.run("grep", "outside-secret").out == b""


def test_only_commits_are_read_never_a_tree_or_a_blob_or_a_path_in_the_revision(world):
    """A tree id or a blob id (of any file, a denied one included) is not a revision here; neither is `rev:path`."""
    r = world.repo
    (r / "secret.txt").write_text("TOPSECRET needle\n")
    world.git(r, "add", "secret.txt")
    world.git(r, "commit", "-q", "-m", "secret", n=7)
    head = world.git(r, "rev-parse", "HEAD")
    tree = world.git(r, "rev-parse", "HEAD^{tree}")
    blob = world.git(r, "rev-parse", "HEAD:secret.txt")
    old_blob = world.git(r, "rev-parse", f"{world.c1}:a.txt")
    for bad in (tree, blob, old_blob, world.git(r, "rev-parse", "HEAD:dir")):
        assert world.run("blob", bad, "secret.txt").code != 0
        assert world.run("blob_size", bad, "secret.txt").code != 0
        assert world.run("file_diff", "3", bad, head, "secret.txt").out == b""
        assert world.run("file_diff", "3", head, bad, "secret.txt").out == b""
        assert world.run("file_log", "5", "0", bad, "secret.txt").out == b""
        assert world.run("commit_file_patch", "3", bad, "secret.txt").out == b""
        assert world.run("range_names", bad, head).out == b""
        assert world.run("commit_names", bad).out == b""
    # a diff between two blobs is the leak this closes: plain git does it, gitbrowse does not
    plain = subprocess.run(["git", "-C", str(r), "diff", old_blob, blob], capture_output=True, text=True)
    assert plain.returncode == 0 and "TOPSECRET" in plain.stdout
    for bad in ("HEAD:secret.txt", f"{head}:secret.txt", f"{head}^{{tree}}", "HEAD^{tree}", f"{world.c1}..{head}",
                f"{head}~1", f"{head}^", "main", "refs/tags/x", "refs/heads/../x", f"{head}\n--all"):  # fmt: skip
        with pytest.raises(gitsafe.Unsafe):
            gitbrowse.command(str(r), "blob", os.environ, operand=(bad, "secret.txt"))
        with pytest.raises(gitsafe.Unsafe):
            gitbrowse.command(str(r), "file_diff", os.environ, operand=("3", bad, head, "secret.txt"))


def test_a_commit_resolves_in_every_form_the_dashboard_uses(world):
    assert world.run("blob", "HEAD", "a.txt").out == b"one\ntwo\n"
    assert world.run("blob", world.c1, "a.txt").out == b"one\n"
    assert world.run("blob", "refs/heads/main", "a.txt").out == b"one\ntwo\n"


def test_git_is_pinned_to_the_confirmed_folders_and_no_variable_of_the_caller_reaches_it(world, monkeypatch):
    other = world.tmp / "other"
    other.mkdir()
    world.git(other, "init", "-q", "-b", "main")
    (other / "x.txt").write_text("other\n")
    world.git(other, "add", "-A")
    world.git(other, "commit", "-q", "-m", "other repo", n=9)
    for name, value in {
        "GIT_DIR": str(other / ".git"), "GIT_COMMON_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
        "GIT_OBJECT_DIRECTORY": str(other / ".git" / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git" / "objects"), "GIT_INDEX_FILE": str(other / "idx"),
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.pager", "GIT_CONFIG_VALUE_0": "x",
        "GIT_NAMESPACE": "x", "GIT_CEILING_DIRECTORIES": "/",
    }.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    real = os.path.realpath(world.repo)
    for op, operand in (("file_log", ("5", "0", "HEAD", "a.txt")), ("grep", ("needle",)), ("blob", ("HEAD", "a.txt"))):
        argv, env = gitbrowse.command(str(world.repo), op, os.environ, operand=operand)
        want = {
            "GIT_DIR": f"{real}/.git", "GIT_COMMON_DIR": f"{real}/.git", "GIT_WORK_TREE": real,
            "GIT_OBJECT_DIRECTORY": f"{real}/.git/objects",
        }  # fmt: skip
        assert all(env[k] == v for k, v in want.items())
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env and "GIT_INDEX_FILE" not in env
        assert "GIT_NAMESPACE" not in env and "GIT_CEILING_DIRECTORIES" not in env
        assert "core.pager" not in " ".join(v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_"))
    assert [r[0] for r in gitbrowse.parse_log(world.run("file_log", "5", "0", "HEAD", "a.txt").out)] == [
        world.c2,
        world.c1,
    ]
    assert world.run("grep", "other").out == b""  # the other repository's content is not reachable


def test_layout_says_where_git_finds_its_directories(world):
    real = os.path.realpath(world.repo)
    lines = world.run("layout").out.decode().splitlines()
    assert lines == [f"{real}/.git", f"{real}/.git", real, f"{real}/.git/objects"]


def test_a_redirecting_git_file_is_refused_by_the_gate_or_shows_in_the_layout(world):
    """`layout` runs unpinned: a `.git` that sends git elsewhere is refused or reported (the caller compares)."""
    elsewhere = world.tmp / "elsewhere"
    elsewhere.mkdir()
    world.git(elsewhere, "init", "-q", "-b", "main")
    plain = world.tmp / "gitfile"
    plain.mkdir()
    (plain / ".git").write_text(f"gitdir: {elsewhere / '.git'}\n")
    real = os.path.realpath(plain)
    try:
        res = gitbrowse.run(str(plain), "layout", os.environ)
    except gitsafe.Unsafe:
        return
    assert res.code != 0 or res.out.decode().splitlines()[0] != f"{real}/.git"

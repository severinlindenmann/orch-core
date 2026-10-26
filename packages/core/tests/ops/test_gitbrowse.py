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
        assert args[0] in {"log", "show", "diff", "cat-file", "grep", "rev-parse", "ls-tree"}


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
        w.run("tree", "HEAD", "dir"),
        w.run("dir_log", "5", "HEAD", "dir"),
        w.run("tree_grep", "alpha", "HEAD", files=("dir/ünï code.txt",)),
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


def test_last_change_lists_only_files_with_their_exact_path(world):
    r = world.repo
    assert gitbrowse.parse_names(world.run("last_change", "HEAD", "a.txt").out) == [("M", "a.txt")]
    assert gitbrowse.parse_names(world.run("last_change", "HEAD", "dir/ünï code.txt").out) == [
        ("M", "dir/ünï code.txt")
    ]
    rows = gitbrowse.parse_names(
        world.run("last_change", "HEAD", "dir").out
    )  # a folder: rows of its files, never itself
    assert rows and all(p != "dir" for _s, p in rows)
    (r / "a.txt").unlink()
    world.git(r, "commit", "-q", "-am", "gone", n=8)
    assert gitbrowse.parse_names(world.run("last_change", "HEAD", "a.txt").out) == [("D", "a.txt")]
    assert world.run("last_change", "HEAD", "never.txt").out == b""


def test_blob_type_and_the_file_name_checks(world):
    assert world.run("blob_type", world.c1, "a.txt").out.strip() == b"blob"
    assert world.run("blob_type", world.c1, "dir").out.strip() == b"tree"
    assert world.run("blob_type", world.c1, "never").code != 0
    assert gitbrowse.parse_names(world.run("commit_file_names", world.c2, "a.txt").out) == [("M", "a.txt")]
    assert [p for _s, p in gitbrowse.parse_names(world.run("commit_file_names", world.c3, "dir").out)] == [
        "dir/ünï code.txt"
    ]
    assert gitbrowse.parse_names(world.run("range_file_names", world.c1, world.c3, "a.txt").out) == [("M", "a.txt")]


def test_orch_local_state_is_on_the_deny_list_in_any_folder():
    assert gitbrowse.denied_name(".STATE") and gitbrowse.denied_name(".state")
    assert ":(exclude,glob,icase)**/.state" in gitbrowse.DENY_SPECS
    assert ":(exclude,glob,icase)**/.state/**" in gitbrowse.DENY_SPECS


def _wiki(world):
    """A `wiki/` folder with a page, a page changed later, a link, a nested folder, a denied name and an odd name."""
    r = world.repo
    (r / "wiki").mkdir()
    (r / "wiki" / "alpha.md").write_text("# Alpha\n")
    (r / "wiki" / "beta.md").write_text("# Beta\n")
    (r / "wiki" / "link.md").symlink_to("/etc/passwd")
    (r / "wiki" / "sub").mkdir()
    (r / "wiki" / "sub" / "deep.md").write_text("deep\n")
    (r / "wiki" / ".env").write_text("SECRET=1\n")
    (r / "wiki" / "new\nline.md").write_text("x\n")
    world.git(r, "add", "-A", "-f")
    world.git(r, "commit", "-q", "-m", "wiki one", n=4)
    (r / "wiki" / "alpha.md").write_text("# Alpha\nmore\n")
    world.git(r, "commit", "-q", "-am", "wiki two", n=5)


def test_tree_lists_the_direct_entries_of_a_folder_with_their_git_modes(world):
    _wiki(world)
    res = world.run("tree", "HEAD", "wiki")
    assert res.code == 0
    rows = {name: (mode, kind, size) for mode, kind, _oid, size, name in gitbrowse.parse_tree(res.out)}
    assert rows["alpha.md"] == ("100644", "blob", 13) and rows["beta.md"][0] == "100644"
    assert rows["link.md"][0] == "120000"  # a link is a blob of mode 120000: callers skip it, nothing follows it
    assert rows["sub"][:2] == ("040000", "tree") and rows["new\nline.md"][1] == "blob"
    assert ".env" in rows  # the operation lists names; the caller's guard decides what is shown
    assert "deep.md" not in rows  # nothing below the folder


def test_tree_of_a_link_a_file_or_a_missing_folder_is_refused_by_git(world):
    _wiki(world)
    world.git(world.repo, "rm", "-q", "-r", "--cached", "wiki")
    assert world.run("tree", "HEAD", "a.txt").code != 0  # a file
    assert world.run("tree", "HEAD", "nothere").code != 0
    (world.repo / "ln").symlink_to("wiki")
    world.git(world.repo, "add", "-f", "ln")
    world.git(world.repo, "commit", "-q", "-m", "a link", n=6)
    assert world.run("tree", "HEAD", "ln").code != 0  # a link to a folder is not a folder


@pytest.mark.parametrize("folder", ["", "/wiki", "../x", "wiki/../..", "a\nb", "wiki//x"])
def test_tree_takes_no_path_that_leaves_the_folder(world, folder):
    with pytest.raises(gitsafe.Unsafe):
        gitbrowse.command(str(world.repo), "tree", os.environ, operand=("HEAD", folder))


def test_dir_log_gives_each_commit_with_the_paths_it_changed_below_the_folder(world):
    _wiki(world)
    res = world.run("dir_log", "50", "HEAD", "wiki")
    rows = gitbrowse.parse_dir_log(res.out)
    assert [r[3] for r in rows] == ["wiki two", "wiki one"]
    assert rows[0][4] == ["wiki/alpha.md"] and rows[0][0] == world.git(world.repo, "rev-parse", "HEAD")
    assert rows[0][1] == "Ann Author" and rows[0][2].startswith("2026-01-05")
    assert "wiki/alpha.md" in rows[1][4] and "wiki/beta.md" in rows[1][4]
    assert not any(".env" in p for p in rows[1][4])  # the deny list is excluded by pathspec on top
    assert all(p.startswith("wiki/") for r in rows for p in r[4])
    assert gitbrowse.parse_dir_log(world.run("dir_log", "1", "HEAD", "wiki").out)[0][3] == "wiki two"


def test_parse_dir_log_is_total_on_cut_and_hostile_output():
    sha = "a" * 40
    ok = f"\0{sha}\0Ann\0d\0subj\0\nwiki/x.md\0"
    assert gitbrowse.parse_dir_log(ok.encode())[0][4] == ["wiki/x.md"]
    assert gitbrowse.parse_dir_log(f"\0{sha}\0Ann\0d".encode()) == []  # cut inside the header
    assert gitbrowse.parse_dir_log(b"") == [] and gitbrowse.parse_dir_log(b"\0\0\0junk") == []
    # an author or a subject that looks like a commit id is data inside its record, never a record start
    twin = f"\0{sha}\0{'b' * 40}\0d\0{'c' * 40}\0\nwiki/y.md\0"
    rows = gitbrowse.parse_dir_log(twin.encode())
    assert len(rows) == 1 and rows[0][1] == "b" * 40 and rows[0][3] == "c" * 40


def test_parse_tree_drops_malformed_records():
    oid = "a" * 40
    good = f"100644 blob {oid}      12\ta.md\x00040000 tree {oid}       -\tsub\x00".encode()
    assert [(m, t, s, n) for m, t, _o, s, n in gitbrowse.parse_tree(good)] == [
        ("100644", "blob", 12, "a.md"),
        ("040000", "tree", None, "sub"),
    ]
    assert gitbrowse.parse_tree(b"garbage\0100644 blob xyz 1\tb\0") == []


def test_tree_grep_reads_exactly_the_named_files_as_committed(world):
    _wiki(world)
    (world.repo / "wiki" / "beta.md").write_text("# Beta\nstaged-only NEEDLE\n")
    world.git(world.repo, "add", "wiki/beta.md")  # in the index, not committed
    (world.repo / "wiki" / "alpha.md").write_text("# Alpha\nworktree-only NEEDLE\n")  # not even staged
    (world.repo / "a.txt").write_text("outside NEEDLE\n")
    world.git(world.repo, "commit", "-q", "-m", "outside", "a.txt", n=7)
    (world.repo / "wiki" / "sub" / "deep.md").write_text("deep NEEDLE\n")
    both = ("wiki/alpha.md", "wiki/beta.md")
    rows = gitbrowse.parse_grep(world.run("tree_grep", "more", "HEAD", files=both).out)
    assert [(p.partition(":")[2], n, t) for p, n, t in rows] == [("wiki/alpha.md", 2, "more")]
    assert gitbrowse.parse_grep(world.run("tree_grep", "needle", "HEAD", files=both).out) == []
    hit = gitbrowse.parse_grep(world.run("tree_grep", "ALPHA", "HEAD", files=both).out)
    assert {p.partition(":")[2] for p, _n, _t in hit} == {"wiki/alpha.md"}  # case-insensitive
    # only the named files are read: a file of the same folder that is not named is not searched, a glob or a pathspec
    # magic names nothing (each is a literal pathspec), and the deny list is excluded on top. (A FOLDER name still
    # matches the files below it, as in every git pathspec: callers pass names of blobs they listed.)
    only = gitbrowse.parse_grep(world.run("tree_grep", "Beta", "HEAD", files=("wiki/alpha.md",)).out)
    assert only == []
    for pattern in (
        "wiki/*.md",
        "wiki/a*",
        "wiki/alph?.md",
        ":(top)wiki/alpha.md",
        ":/wiki/alpha.md",
        ".env",
        "wiki/.env",
    ):
        assert gitbrowse.parse_grep(world.run("tree_grep", "alpha", "HEAD", files=(pattern,)).out) == [], pattern
    assert gitbrowse.parse_grep(world.run("tree_grep", "SECRET", "HEAD", files=("wiki/.env",)).out) == []
    assert gitbrowse.parse_grep(world.run("tree_grep", "x", "HEAD", files=("wiki/link.md",)).out) == []


@pytest.mark.parametrize(
    "files", [(), ("",), ("/etc/passwd",), ("../x",), ("a/../b",), ("a\nb",), ("a",) * (gitbrowse.MAX_FILES + 1)]
)
def test_tree_grep_refuses_no_files_and_bad_files(world, files):
    with pytest.raises(gitsafe.Unsafe):
        gitbrowse.command(str(world.repo), "tree_grep", os.environ, operand=("x", "HEAD"), files=files)


def test_files_are_for_the_operations_that_take_them(world):
    with pytest.raises(gitsafe.Unsafe):
        gitbrowse.command(str(world.repo), "tree", os.environ, operand=("HEAD", "dir"), files=("a",))
    with pytest.raises(gitsafe.Unsafe):
        gitbrowse.command(str(world.repo), "tree_grep", os.environ, operand=("a\nb", "HEAD"), files=("wiki/a.md",))


def test_dir_log_names_the_author_without_the_work_trees_mailmap(world):
    _wiki(world)
    (world.repo / ".mailmap").write_text("Someone Else <ann@example.invalid>\n")  # uncommitted, in the work tree
    rows = gitbrowse.parse_dir_log(world.run("dir_log", "5", "HEAD", "wiki").out)
    assert {r[1] for r in rows} == {"Ann Author"}


def test_cut_off_output_never_yields_a_half_name():
    sha = "a" * 40
    whole = f"\x00{sha}\x00Ann\x00d\x00subj\x00\nwiki/x.md-extra\x00".encode()
    assert gitbrowse.parse_dir_log(whole)[0][4] == ["wiki/x.md-extra"]
    assert gitbrowse.parse_dir_log(whole[:-7])[0][4] == []  # cut inside the name: that name is dropped
    oid = "b" * 40
    tree = f"100644 blob {oid} 5\tx.md-extra\x00".encode()
    assert len(gitbrowse.parse_tree(tree)) == 1 and gitbrowse.parse_tree(tree[:-7]) == []

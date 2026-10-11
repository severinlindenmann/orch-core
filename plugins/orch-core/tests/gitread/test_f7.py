"""F7 and the H3b1 acceptance items about what the reader refuses: a malicious config runs nothing; a ``.git`` file or
a symlinked ``objects/`` pointing elsewhere; reftable; a changed origin against a declared remote; a ``.git`` file
planted before the repo is added; a swapped ``commondir``; loose/packed disagreement; case-colliding refs;
``worktreeConfig``; and the pin races (a component swapped for a symlink after the check)."""

from __future__ import annotations

import base64
import os
import shutil

import pytest

from orch.gitread import config as cfgmod
from orch.gitread import fs, local, pinning
from orch.gitread.client import HostGitReader, PinStore, ReaderPool
from orch.gitread.errors import RemoteMismatch, Unavailable
from orch.gitread.pinning import pin_repo
from orch.gitread.repo import Repo
from orch.ops.caps import Capabilities

from .conftest import git_version
from .test_diff import GIT_DIFF


def two_commits(r):
    r.write("a.txt", "1\n")
    base = r.commit()
    r.git("checkout", "-q", "-b", "feat")
    r.write("a.txt", "2\n")
    head = r.commit()
    return base, head


def observe(r, roots=None):
    return local.observe(r.path, ["refs/heads/main", "refs/heads/feat"], roots)


# ---------------------------------------------------------------- a malicious .git/config runs nothing


def test_a_malicious_config_runs_nothing_and_includes_are_not_followed(make_repo, tmp_path):
    r = make_repo()
    two_commits(r)
    marker = tmp_path.resolve() / "PWNED"
    evil = tmp_path.resolve() / "evil.cfg"
    evil.write_text('[remote "origin"]\n\turl = https://evil.example/x\n')
    cmd = f"touch {marker}"
    with open(r.gitdir / "config", "a") as f:
        f.write(
            f"[core]\n\tfsmonitor = {cmd}\n\thooksPath = /tmp\n\tpager = {cmd}\n\tsshCommand = {cmd}\n"
            f'[filter "x"]\n\tclean = {cmd}\n\tsmudge = {cmd}\n\tprocess = {cmd}\n'
            f'[diff "x"]\n\ttextconv = {cmd}\n\tcommand = {cmd}\n[diff]\n\texternal = {cmd}\n'
            f"[alias]\n\tdiff = !{cmd}\n[include]\n\tpath = {evil}\n"
            f'[includeIf "gitdir:/"]\n\tpath = {evil}\n'
            '[remote "origin"]\n\turl = git@github.com:acme/widget.git\n'
        )
    (r.path / ".gitattributes").write_text("* filter=x diff=x\n")
    hooks = r.gitdir / "hooks"
    (hooks / "post-checkout").write_text(f"#!/bin/sh\n{cmd}\n")
    (hooks / "post-checkout").chmod(0o755)
    host = HostGitReader(ReaderPool(), PinStore())
    assert host.sync([r.path]) == {str(r.path): None}
    got = host.observe(r.path, ["refs/heads/feat"])
    assert got["origin_urls"] == ["git@github.com:acme/widget.git"]  # the include was not read
    d = host.diff(r.path, branch="feat", default_branch=None)
    assert b"+2" in base64.b64decode(d["patch"])  # no textconv, no filter
    caps = Capabilities.host((str(tmp_path.resolve()),), git=host)
    assert caps.git.identity(r.path, "w") == "https://github.com/acme/widget"
    assert not marker.exists()


# ---------------------------------------------------------------- .git file, symlinked objects/, .git symlink


def test_a_git_file_pointing_outside_the_declared_repos_is_refused(make_repo, tmp_path):
    outside = make_repo("outside")
    two_commits(outside)
    victim = tmp_path.resolve() / "declared"
    victim.mkdir()
    (victim / ".git").write_text(f"gitdir: {outside.gitdir}\n")
    with pytest.raises(Unavailable, match="outside every declared repo"):
        pin_repo(str(victim), [str(victim)])
    # it is accepted once the other path is declared too (a linked worktree of a declared repo)
    pins = pin_repo(str(victim), [str(victim), str(outside.path)])
    assert pins.gitdir.path == str(outside.gitdir)


def test_a_symlinked_objects_dir_is_refused(make_repo):
    r = make_repo()
    two_commits(r)
    other = make_repo("other")
    two_commits(other)
    shutil.rmtree(r.gitdir / "objects")
    os.symlink(other.gitdir / "objects", r.gitdir / "objects")
    with pytest.raises(Unavailable, match="symlink"):
        observe(r)


def test_a_git_symlink_is_refused(make_repo, tmp_path):
    r = make_repo()
    two_commits(r)
    wt = tmp_path.resolve() / "wt"
    wt.mkdir()
    os.symlink(r.gitdir, wt / ".git")
    with pytest.raises(Unavailable, match="symlink"):
        pin_repo(str(wt), [str(wt), str(r.path)])


def test_a_symlinked_ref_and_a_loose_symref_are_refused(make_repo):
    r = make_repo()
    two_commits(r)
    os.symlink(r.gitdir / "refs/heads/main", r.gitdir / "refs/heads/alias")
    with pytest.raises(Unavailable, match="symlink"):
        observe(r)
    (r.gitdir / "refs/heads/alias").unlink()
    (r.gitdir / "refs/heads/alias").write_text("ref: refs/heads/main\n")
    with pytest.raises(Unavailable, match="symbolic ref"):
        observe(r)


# ---------------------------------------------------------------- extensions, reftable


def test_worktree_config_is_unavailable(make_repo):
    r = make_repo()
    two_commits(r)
    r.git("config", "extensions.worktreeConfig", "true")
    with pytest.raises(Unavailable, match="worktreeconfig"):
        observe(r)


@pytest.mark.parametrize(
    ("key", "value"),
    [("extensions.refStorage", "reftable"), ("extensions.partialClone", "origin"), ("extensions.preciousObjects", "1")],
)
def test_any_other_extension_is_unavailable(make_repo, key, value):
    r = make_repo()
    two_commits(r)
    r.git("config", "core.repositoryformatversion", "1")
    r.git("config", key, value)
    with pytest.raises(Unavailable, match="extension"):
        observe(r)


def test_noop_and_sha256_are_allowed(make_repo):
    r = make_repo(object_format="sha256")
    base, head = two_commits(r)
    r.git("config", "extensions.noop", "true")
    assert observe(r)["refs"] == {"refs/heads/main": base, "refs/heads/feat": head}


def test_a_reftable_directory_is_unavailable(make_repo):
    r = make_repo()
    two_commits(r)
    (r.gitdir / "reftable").mkdir()
    with pytest.raises(Unavailable, match="reftable"):
        observe(r)


@pytest.mark.skipif(git_version() < (2, 45), reason="git before 2.45 cannot make reftable repos")
def test_a_real_reftable_repo_is_unavailable(make_repo):
    r = make_repo(extra=("--ref-format=reftable",))
    two_commits(r)
    with pytest.raises(Unavailable, match="reftable"):
        observe(r)


def test_config_parser_reads_only_its_keys():
    raw = (
        b"\xef\xbb\xbf# comment\n[core]\n\trepositoryformatversion = 1 ; note\n\tbare\n"
        b'[extensions]\n\tobjectFormat = "sha256"\n'
        b'[remote "origin"]\n\turl = "a b"  \\\n c # tail\n'
        b'[remote "Origin"]\n\turl = not-this\n[Remote.origin]\n\turl = legacy\n'
    )
    c = cfgmod.read_config(raw)
    assert (c.version, c.object_format, c.origin_urls) == (
        1,
        "sha256",
        ["a b   c", "legacy"],
    )  # as git config --get-all says
    for bad in (b"[core\n", b"key = 1\n", b'[core]\n\tx = "open\n', b"[core]\n\tx = \\q\n", b"[a] 1x = 2\n"):
        with pytest.raises(Unavailable):
            cfgmod.read_config(bad)
    with pytest.raises(Unavailable, match="repositoryformatversion"):
        cfgmod.read_config(b"[core]\n\trepositoryformatversion = 2\n")
    with pytest.raises(Unavailable, match="needs core.repositoryformatversion 1"):
        cfgmod.read_config(b"[extensions]\n\tobjectformat = sha256\n")


# ---------------------------------------------------------------- identity: a changed origin against a declared remote


def test_a_changed_origin_against_a_declared_remote_is_a_mismatch(make_repo, tmp_path):
    r = make_repo()
    two_commits(r)
    r.git("remote", "add", "origin", "https://github.com/acme/widget.git")
    caps = Capabilities.direct({})
    kw = {"name": "w", "branch": "feat", "default_branch": None}
    caps.git.diff(r.path, remote="https://github.com/ACME/widget", **kw)  # ASCII case ignored
    r.git("remote", "set-url", "origin", "https://github.com/evil/widget.git")
    with pytest.raises(RemoteMismatch):
        caps.git.diff(r.path, remote="https://github.com/acme/widget", **kw)
    host = HostGitReader(ReaderPool(), PinStore())
    host.sync([r.path])
    hcaps = Capabilities.host((str(tmp_path.resolve()),), git=host)
    with pytest.raises(RemoteMismatch):
        hcaps.git.diff(r.path, remote="https://github.com/acme/widget", **kw)
    r.git("config", "--add", "remote.origin.url", "https://github.com/acme/widget.git")  # two values: local:<name>
    assert hcaps.git.identity(r.path, "w") == "local:w"


# ---------------------------------------------------------------- linked worktrees: commondir pins


def linked(make_repo, tmp_path):
    main = make_repo("main")
    two_commits(main)
    wt = tmp_path.resolve() / "wt"
    main.git("worktree", "add", "-q", str(wt), "-b", "side")
    return main, wt


def test_a_linked_worktree_reads_through_its_commondir(make_repo, tmp_path):
    main, wt = linked(make_repo, tmp_path)
    pins = pin_repo(str(wt), [str(wt), str(main.path)])
    assert pins.commondir.path == str(main.gitdir) and pins.gitdir.path.startswith(str(main.gitdir / "worktrees"))
    with Repo(pins) as rp:
        assert rp.head_ref() == "refs/heads/side" and rp.refs.resolve("refs/heads/feat") == main.sha("feat")
    with pytest.raises(Unavailable, match="commondir lies outside"):  # main's path not declared
        pin_repo(str(wt), [str(wt), str(main.gitdir / "worktrees")])


def test_a_swapped_commondir_is_unavailable(make_repo, tmp_path):
    main, wt = linked(make_repo, tmp_path)
    other = make_repo("other")
    two_commits(other)
    roots = [str(wt), str(main.path), str(other.path)]
    pins = pin_repo(str(wt), roots)
    gitdir = pins.gitdir.path
    with open(os.path.join(gitdir, "commondir"), "w") as f:  # repoint the commondir file at another declared repo
        f.write(f"{other.gitdir}\n")
    with pytest.raises(Unavailable, match="another commondir"):
        Repo(pins)
    with open(os.path.join(gitdir, "commondir"), "w") as f:
        f.write("../..\n")
    Repo(pins).close()
    # the commondir directory itself replaced by another one at the same path
    os.rename(main.gitdir, main.path / "old-git")
    shutil.copytree(main.path / "old-git", main.gitdir, symlinks=True)
    with pytest.raises(Unavailable, match="not the pinned directory"):
        Repo(pins)


def test_a_git_file_planted_before_the_repo_is_added(make_repo, tmp_path):
    """The agent plants ``.git`` (a file) in the working copy before the owner declares it: the pin refuses a target
    outside the declared repos, and a later swap of the file does not move a pin already taken."""
    real = make_repo("real")
    two_commits(real)
    attacker = make_repo("attacker")
    two_commits(attacker)
    planted = tmp_path.resolve() / "planted"
    planted.mkdir()
    (planted / ".git").write_text(f"gitdir: {attacker.gitdir}\n")
    host = HostGitReader(ReaderPool(), PinStore())
    res = host.sync([planted, real.path])
    assert "outside every declared repo" in res[str(planted)]
    with pytest.raises(Unavailable, match="not pinned"):
        host.observe(planted)
    # a pinned repo whose .git is replaced by a file afterwards keeps its pin (only settings.changed re-pins)
    pinned = host.pins.get(str(real.path))
    shutil.move(real.gitdir, real.path / "moved")
    (real.path / ".git").write_text(f"gitdir: {attacker.gitdir}\n")
    host.sync([planted, real.path])
    assert host.pins.get(str(real.path)) is pinned
    with pytest.raises(Unavailable):
        host.observe(real.path)


# ---------------------------------------------------------------- refs


def test_loose_wins_over_packed_as_git_resolves_it(make_repo):
    r = make_repo()
    base, head = two_commits(r)
    r.git("pack-refs", "--all")
    (r.gitdir / "refs/heads/feat").write_text(base + "\n")  # loose disagrees with packed-refs
    assert r.sha("feat") == base  # git: the loose ref wins
    assert observe(r)["refs"]["refs/heads/feat"] == base
    assert head in (r.gitdir / "packed-refs").read_text()


def test_case_colliding_refs_are_unavailable(make_repo):
    r = make_repo()
    two_commits(r)
    r.git("pack-refs", "--all")
    packed = (r.gitdir / "packed-refs").read_text()
    sha = r.sha("feat")
    (r.gitdir / "packed-refs").write_text(packed + f"{sha} refs/heads/FEAT\n")
    with pytest.raises(Unavailable, match="differ only in case"):
        observe(r)


def test_case_collision_across_the_stores(make_repo):
    r = make_repo()
    two_commits(r)
    r.git("pack-refs", "--all")
    (r.gitdir / "packed-refs").write_text(
        (r.gitdir / "packed-refs").read_text().replace("refs/heads/feat", "refs/heads/Feat")
    )
    (r.gitdir / "refs/heads").mkdir(exist_ok=True)
    (r.gitdir / "refs/heads/feat").write_text(r.sha("main") + "\n")
    with pytest.raises(Unavailable, match="differ only in case"):
        observe(r)


@pytest.mark.parametrize("line", ["garbage\n", "0123 refs/heads/x\n", "^" + "a" * 40 + "\n"])
def test_an_unparsable_packed_refs_line_is_unavailable(make_repo, line):
    r = make_repo()
    two_commits(r)
    r.git("pack-refs", "--all")
    (r.gitdir / "packed-refs").write_text("# pack-refs with: peeled fully-peeled sorted \n" + line)
    with pytest.raises(Unavailable, match="packed-refs"):
        observe(r)


def test_head_must_be_a_branch_symref(make_repo):
    r = make_repo()
    base, _ = two_commits(r)
    r.git("checkout", "-q", "--detach", "main")
    got = observe(r)
    assert got["head_ref"] is None and "HEAD is not" in got["head_error"]
    assert got["refs"]["refs/heads/main"] == base


def test_alternates_grafts_and_replace_refs_are_ignored(make_repo):
    r = make_repo()
    base, head = two_commits(r)
    other = make_repo("other")
    other.write("z", "z\n")
    foreign = other.commit()
    (r.gitdir / "objects/info").mkdir(exist_ok=True)
    (r.gitdir / "objects/info/alternates").write_text(str(other.gitdir / "objects") + "\n")
    (r.gitdir / "info").mkdir(exist_ok=True)
    (r.gitdir / "info/grafts").write_text(f"{head} {foreign}\n")
    (r.gitdir / "refs/replace").mkdir(parents=True)
    (r.gitdir / "refs/replace" / head).write_text(base + "\n")
    got = local.diff(r.path, branch="feat", default_branch=None)
    assert (got["base_sha"], got["head_sha"]) == (base, head)
    plain = r.git("--no-replace-objects", *GIT_DIFF, base, head, raw=True)
    assert base64.b64decode(got["patch"]) == plain


# ---------------------------------------------------------------- races: a component swapped after the check


def test_a_component_swapped_for_a_symlink_after_the_check_is_refused_at_pin_time(make_repo, tmp_path, monkeypatch):
    """The gitdir of a linked worktree is checked (under a declared repo) and then opened: a parent directory
    swapped for a symlink to an attacker's tree in between must not be followed."""
    main, wt = linked(make_repo, tmp_path)
    evil = make_repo("evil")
    two_commits(evil)
    real_under = pinning.under
    swapped = []

    def swap_then_check(path, root):
        if not swapped and path.startswith(str(main.gitdir / "worktrees")):
            wts = main.gitdir / "worktrees"
            os.rename(wts, main.gitdir / "worktrees-real")
            (evil.gitdir / "worktrees").mkdir(exist_ok=True)
            shutil.copytree(main.gitdir / "worktrees-real" / "wt", evil.gitdir / "worktrees" / "wt", symlinks=True)
            os.symlink(evil.gitdir / "worktrees", wts)
            swapped.append(1)
        return real_under(path, root)

    monkeypatch.setattr(pinning, "under", swap_then_check)
    with pytest.raises(Unavailable, match="symlink"):
        pin_repo(str(wt), [str(wt), str(main.path)])
    assert swapped


def test_a_git_dir_swapped_after_the_lstat_is_refused(make_repo, monkeypatch):
    r = make_repo()
    two_commits(r)
    other = make_repo("other")
    two_commits(other)
    real_kind = fs.Dir.kind

    def kind_then_swap(self, rel):
        k = real_kind(self, rel)
        if rel == ".git" and k == "dir":
            os.rename(r.gitdir, r.path / "old")
            os.symlink(other.gitdir, r.gitdir)
        return k

    monkeypatch.setattr(fs.Dir, "kind", kind_then_swap)
    with pytest.raises(Unavailable, match="symlink"):
        pin_repo(str(r.path), [str(r.path)])


def test_a_parent_replaced_by_a_symlink_after_pinning_is_refused_at_use(make_repo, tmp_path):
    """The repo moves and a symlink takes its old path: the gitdir is the same inode, but reaching it now follows a
    symlink, which the walk refuses (the old absolute open would have followed it)."""
    r = make_repo("a/repo")
    two_commits(r)
    pins = pin_repo(str(r.path), [str(r.path)])
    base = tmp_path.resolve()
    os.rename(base / "a", base / "b")
    os.symlink(base / "b", base / "a")
    with pytest.raises(Unavailable, match="symlink"):
        Repo(pins)


# ---------------------------------------------------------------- after registration: nothing re-resolves a path


def two_pinned(make_repo):
    a, b = make_repo("a"), make_repo("b")
    two_commits(a)
    b.write("other.txt", "b\n")
    b.commit()
    host = HostGitReader(ReaderPool(), PinStore())
    assert host.sync([a.path, b.path]) == {str(a.path): None, str(b.path): None}
    return a, b, host


def test_a_symlink_swapped_into_a_pinned_path_never_selects_another_repos_pins(make_repo):
    """Repo A's path becomes a symlink to repo B after registration. Resolving it would find B's pins and read B for
    A; the lexical key keeps A's pins, whose O_NOFOLLOW walk refuses the symlink."""
    a, b, host = two_pinned(make_repo)
    head_b = b.sha("HEAD")
    os.rename(a.path, a.path.with_name("a-away"))
    os.symlink(b.path, a.path)
    with pytest.raises(Unavailable, match="symlink"):
        host.observe(a.path)
    assert host.observe(b.path)["head_sha"] == head_b  # the other pinned repo still reads
    a.path.unlink()
    os.rename(a.path.with_name("a-away"), a.path)
    assert host.observe(a.path)["refs"] == {}  # A reads again once its own directory is back


def test_a_gitdir_symlink_swapped_in_after_registration_is_refused(make_repo):
    a, b, host = two_pinned(make_repo)
    os.rename(a.gitdir, a.path / "git-away")
    os.symlink(b.gitdir, a.gitdir)
    with pytest.raises(Unavailable, match="symlink"):
        host.observe(a.path)
    assert host.observe(b.path)["head_ref"] == "refs/heads/main"
    a.gitdir.unlink()
    os.rename(a.path / "git-away", a.gitdir)
    assert host.observe(a.path, ["refs/heads/feat"])["refs"]["refs/heads/feat"] == a.sha("feat")


def test_a_git_file_redirect_planted_after_registration_is_refused(make_repo):
    a, b, host = two_pinned(make_repo)
    os.rename(a.gitdir, a.path / "git-away")
    (a.path / ".git").write_text(f"gitdir: {b.gitdir}\n")
    with pytest.raises(Unavailable, match="not a directory"):
        host.observe(a.path)  # the pin is A's gitdir directory; the .git file is never read again
    assert host.observe(b.path)["head_ref"] == "refs/heads/main"
    host.sync([a.path, b.path])  # no re-pin from the planted file
    (a.path / ".git").unlink()
    os.rename(a.path / "git-away", a.gitdir)
    assert host.observe(a.path, ["refs/heads/main"])["refs"]["refs/heads/main"] == a.sha("main")


def test_a_commondir_file_naming_a_symlink_to_the_pinned_commondir_is_refused(make_repo, tmp_path):
    """Compared lexically, never resolved: even a symlink that leads to the right directory fails closed."""
    main, wt = linked(make_repo, tmp_path)
    pins = pin_repo(str(wt), [str(wt), str(main.path)])
    alias = tmp_path.resolve() / "alias-git"
    os.symlink(main.gitdir, alias)
    path = os.path.join(pins.gitdir.path, "commondir")
    with open(path) as f:
        original = f.read()
    with open(path, "w") as f:
        f.write(f"{alias}\n")
    with pytest.raises(Unavailable, match="another commondir"):
        Repo(pins)
    with open(path, "w") as f:
        f.write(original)
    with Repo(pins) as rp:  # the pinned repo is readable again
        assert rp.head_ref() == "refs/heads/side"


def _case_insensitive(path) -> bool:
    flipped = path.with_name(path.name.swapcase())
    return flipped != path and flipped.exists()


@pytest.mark.parametrize("variant", ["trailing", "dot", "dotdot", "double", "case", "nfd"])
def test_path_variants_never_reach_another_repo(make_repo, tmp_path, variant):
    """One interpretation of a path: the declared string byte for byte. A variant is refused (unclean, or no pin),
    and declaring it as a second repo is refused because it is the same (device, inode)."""
    import unicodedata

    r = make_repo("Repo\u00e9")  # NFC e-acute, mixed case
    two_commits(r)
    host = HostGitReader(ReaderPool(), PinStore())
    assert host.sync([r.path]) == {str(r.path): None}
    p = str(r.path)
    parent, name = p.rsplit("/", 1)
    v = {
        "trailing": p + "/",
        "dot": parent + "/./" + name,
        "dotdot": parent + "/x/../" + name,
        "double": parent + "//" + name,
        "case": parent + "/" + name.swapcase(),
        "nfd": parent + "/" + unicodedata.normalize("NFD", name),
    }[variant]
    assert v != p
    with pytest.raises(Unavailable):
        host.observe(v)  # unclean, or clean but not pinned
    res = host.sync([r.path, v])
    assert res[str(r.path)] is None
    assert res[v] is not None  # refused: unclean, missing, or the same directory as the pinned repo
    if variant in ("case", "nfd") and os.path.isdir(v):
        assert "same directory" in res[v]  # APFS: one directory, two spellings
    assert host.pins.get(v) is None and host.observe(r.path)["head_ref"] == "refs/heads/feat"


def test_a_declared_path_with_a_symlinked_component_is_refused(make_repo, tmp_path):
    r = make_repo()
    two_commits(r)
    alias = tmp_path.resolve() / "alias"
    os.symlink(r.path, alias)
    with pytest.raises(Unavailable, match="symlink"):
        pin_repo(str(alias), [str(alias)])


def test_path_files_are_read_byte_for_byte():
    from orch.gitread.repo import resolve_named

    assert resolve_named("/a/b/.git/worktrees/w", b"../..\n") == "/a/b/.git"
    assert resolve_named("/a", b"/x/y\n") == "/x/y"
    for bad in (b"/x/../y", b"/x//y", b"/x/y/", b"/x/./y", b"../a/../b", b"a/./b", b"../../../../../../.."):
        with pytest.raises(Unavailable):
            resolve_named("/a/b", bad)

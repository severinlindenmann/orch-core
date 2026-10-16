"""The capability object (D-P2-9): direct mode keeps today's powers, host mode is restricted, and the host file
capability has exactly one reading of a path, which is also the open."""

import gc
import hashlib
import os
import sys
from pathlib import Path

import pytest

from orch.ops.base import Context
from orch.ops.caps import Capabilities, CapabilityDenied, Files, NotARegularFile
from tests._fds import open_fds_on, tree_identities


@pytest.fixture
def tree(tmp_path):
    base = tmp_path.resolve()
    root, outside = base / "root", base / "outside"
    (root / "a").mkdir(parents=True)
    outside.mkdir()
    (root / "a" / "f.txt").write_text("inside")
    (outside / "secret.txt").write_text("outside")
    return root, outside


# ------------------------------------------------------------------------------------------------------ direct mode


def test_context_builds_direct_caps_from_env_and_clock():
    ctx = Context(env={"A": "b"}, now=lambda: 7.0)
    assert ctx.caps.env == {"A": "b"} and ctx.env == {"A": "b"}
    assert ctx.caps.clock() == 7.0
    assert ctx.caps.exec.enabled and ctx.caps.git.enabled and ctx.caps.signer.enabled
    ctx.caps.signer.require()


def test_direct_files_reach_any_path(tree):
    root, outside = tree
    f = Capabilities.direct({}).files
    assert f.read_text(outside / "secret.txt") == "outside"
    assert f.getcwd() == os.getcwd()
    data, size = f.read_client(str(outside / "secret.txt"), 100)
    assert (data, size) == (b"outside", 7)
    assert f.read_client(str(outside / "secret.txt"), 3) == (b"", 7)  # too big: not read
    assert f.sha256_client(str(outside / "secret.txt"), 100) == hashlib.sha256(b"outside").hexdigest()
    assert f.sha256_client(str(outside / "secret.txt"), 3) is None
    with pytest.raises(NotARegularFile):
        f.read_client(str(outside), 10)
    f.write_text(outside / "w.txt", "x")
    assert f.lexists(outside / "w.txt")
    f.unlink(outside / "w.txt")
    assert not f.lexists(outside / "w.txt")


def test_direct_exec_runs_and_child_env_merges():
    c = Capabilities.direct({})
    done = c.exec.run([sys.executable, "-c", "print(1)"], capture_output=True)
    assert done.stdout.strip() == b"1"
    assert c.exec.child_env({"X": "1"})["X"] == "1"
    assert "PATH" in c.exec.child_env({}) or "PATH" not in os.environ


# --------------------------------------------------------------------------------------------------------- host mode


def host(tree):
    root, _ = tree
    return Capabilities.host((root,), clock=lambda: 3.0)


def test_host_caps_deny_everything_but_rooted_files(tree):
    c = host(tree)
    assert c.env == {} and c.clock() == 3.0
    with pytest.raises(CapabilityDenied):
        c.exec.run(["true"])
    with pytest.raises(CapabilityDenied):
        c.exec.popen(["true"])
    with pytest.raises(CapabilityDenied):
        c.exec.child_env({})
    with pytest.raises(CapabilityDenied):
        c.git.head(tree[0])
    with pytest.raises(CapabilityDenied):
        c.signer.require()
    with pytest.raises(CapabilityDenied):
        c.files.getcwd()
    with pytest.raises(CapabilityDenied):
        c.files.read_stdin(10)
    assert isinstance(CapabilityDenied("x"), OSError)


def test_host_files_work_inside_the_roots(tree):
    root, _ = tree
    f = host(tree).files
    assert f.read_text(root / "a" / "f.txt") == "inside"
    f.write_text(root / "a" / "g.txt", "new")
    assert f.read_bytes(root / "a" / "g.txt") == b"new"
    f.mkdir(root / "x" / "y", parents=True)
    assert (root / "x" / "y").is_dir()
    assert f.lexists(root / "a" / "g.txt") and not f.lexists(root / "a" / "nope")
    f.unlink(root / "a" / "g.txt")
    f.rmtree(root / "x")
    assert not (root / "x").exists()


def test_host_files_refuse_outside_the_roots(tree):
    root, outside = tree
    f = host(tree).files
    for bad in (outside / "secret.txt", "/etc/passwd", "relative.txt", str(root) + "x/a"):
        with pytest.raises(CapabilityDenied):
            f.read_bytes(bad)


def test_traversal_with_dotdot_is_refused(tree):
    root, outside = tree
    f = host(tree).files
    for bad in (f"{root}/a/../../outside/secret.txt", f"{root}/a/../../etc", f"{root}/./a/f.txt", f"{root}//a/f.txt"):
        with pytest.raises(CapabilityDenied):
            f.read_bytes(bad)


def test_nul_byte_is_refused(tree):
    root, _ = tree
    with pytest.raises(CapabilityDenied):
        host(tree).files.read_bytes(f"{root}/a/f.txt\0/../../x")


def test_symlinked_directory_component_is_refused(tree):
    root, outside = tree
    (root / "link").symlink_to(outside)
    f = host(tree).files
    with pytest.raises(OSError):
        f.read_bytes(root / "link" / "secret.txt")
    with pytest.raises(OSError):
        f.write_text(root / "link" / "new.txt", "x")
    assert not (outside / "new.txt").exists()
    with pytest.raises(OSError):
        f.mkdir(root / "link" / "d", parents=True)


def test_symlinked_final_component_is_refused(tree):
    root, outside = tree
    (root / "a" / "l.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(OSError):
        host(tree).files.read_bytes(root / "a" / "l.txt")


def test_symlink_swapped_after_validation_cannot_escape(tree):
    """The check is the open: a swap between the string split and the walk is refused by the kernel (``O_NOFOLLOW``)."""
    root, outside = tree

    class Racy(Files):
        def _split(self, path):
            got = super()._split(path)  # validated ...
            (root / "a" / "f.txt").unlink()
            os.rename(root / "a", root / "a.moved")
            (root / "a").symlink_to(outside)  # ... then swapped
            (outside / "f.txt").write_text("outside")
            return got

    f = Racy(roots=(root,), cwd_allowed=False, stdin_allowed=False, client_paths=False)
    with pytest.raises(OSError):
        f.read_bytes(root / "a" / "f.txt")


def test_normpath_and_raw_open_cannot_differ(tree):
    """``link/../x`` is ``root/x`` to ``normpath`` but ``outside/../x`` to the kernel: there is one reading, and it
    refuses the form."""
    root, outside = tree
    (root / "link").symlink_to(outside)
    (root / "x.txt").write_text("root-x")
    (outside.parent / "x.txt").write_text("parent-x")
    p = f"{root}/link/../x.txt"
    assert os.path.normpath(p) == str(root / "x.txt")
    with pytest.raises(CapabilityDenied):
        host(tree).files.read_bytes(p)


def test_root_spelling_is_exact(tree, tmp_path):
    root, _ = tree
    link = tmp_path / "rootlink"
    link.symlink_to(root)
    with pytest.raises(CapabilityDenied):
        host(tree).files.read_bytes(link / "a" / "f.txt")
    with pytest.raises(CapabilityDenied):
        host(tree).files.read_bytes(str(root).upper() + "/a/f.txt")


def test_roots_must_be_absolute_and_free_of_nul():
    with pytest.raises(ValueError):
        Files.host(("rel",))


# ------------------------------------------------------------------------------------------------- special files


def test_fifo_does_not_hang_and_is_refused(tree):
    root, _ = tree
    os.mkfifo(root / "a" / "fifo")
    f = host(tree).files
    with pytest.raises(CapabilityDenied):
        f.read_bytes(root / "a" / "fifo")  # the type, not a bare OSError
    for call in (f.read_bytes, lambda p: f.read_client(str(p), 10), f.read_text):
        with pytest.raises(OSError):  # CapabilityDenied (not regular); never a hang
            call(root / "a" / "fifo")
    with pytest.raises(OSError):
        f.write_text(root / "a" / "fifo", "x")


def test_device_is_refused(tree, tmp_path):
    root, _ = tree
    if not os.path.exists("/dev/zero"):
        pytest.skip("no /dev/zero")
    # a device node cannot be made unprivileged, so reach one through the root spelled as /dev
    f = Files.host((Path("/dev"),))
    with pytest.raises(CapabilityDenied):
        f.read_bytes("/dev/zero")


def test_socket_file_is_refused():
    import shutil
    import socket
    import tempfile

    short = Path(tempfile.mkdtemp(prefix="oc", dir="/tmp")).resolve()  # AF_UNIX paths are short
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(str(short / "s"))
        with pytest.raises(OSError):
            Files.host((short,)).read_bytes(short / "s")
    finally:
        s.close()
        shutil.rmtree(short, ignore_errors=True)


def test_whole_file_reads_are_capped(tree, monkeypatch):
    root, _ = tree
    monkeypatch.setattr("orch.ops.caps.WHOLE_FILE_LIMIT", 4)
    (root / "a" / "big").write_bytes(b"12345")
    with pytest.raises(CapabilityDenied):
        host(tree).files.read_bytes(root / "a" / "big")
    (root / "a" / "ok").write_bytes(b"1234")
    assert host(tree).files.read_bytes(root / "a" / "ok") == b"1234"


# ------------------------------------------------------------------------------------ the review round (#375)


def test_host_refuses_client_named_paths_even_inside_the_roots(tmp_path):
    """The state directory is a root; a client must not be able to name the workspace key in it."""
    base = tmp_path.resolve()
    ws, state = base / "ws", base / "state"
    (state / "hosts" / "W" / "keys").mkdir(parents=True)
    ws.mkdir()
    wsk = state / "hosts" / "W" / "keys" / "wsk"
    wsk.write_bytes(b"WSK-PRIVATE-KEY-BYTES")
    ctx = Context(caps=Capabilities.host((ws, state)))
    from orch.ops.errors import OrchError
    from orch.ops.runtime import Call, input_digest

    c = Call.__new__(Call)
    c.ctx = ctx
    for fn in (c.read_artifact, c.read_file_text):
        with pytest.raises(OrchError) as e:
            fn(str(wsk))
        assert "WSK" not in str(e.value) and "client" in e.value.message
    assert input_digest(ctx, str(wsk)) is None
    # what the client sent with the call is the only source
    ctx.files["art.txt"] = b"uploaded"
    assert c.read_artifact("art.txt") == b"uploaded"
    assert c.read_file_text("art.txt") == "uploaded"
    # the capability itself refuses, so a new caller cannot forget
    with pytest.raises(CapabilityDenied):
        ctx.caps.files.read_client(str(wsk), 100)
    with pytest.raises(CapabilityDenied):
        ctx.caps.files.sha256_client(str(wsk), 100)


def test_denied_and_non_regular_messages_are_words_not_none(tree):
    from orch.ops.errors import OrchError
    from orch.ops.runtime import Call

    root, _ = tree
    os.mkfifo(root / "ff")
    c = Call.__new__(Call)
    c.ctx = Context(caps=Capabilities.direct({}))
    with pytest.raises(OrchError) as e:
        c.read_artifact(str(root / "ff"))
    assert "None" not in e.value.message and "regular" in e.value.message
    c.ctx = Context(caps=Capabilities.host((root,)))
    with pytest.raises(OrchError) as e:
        c.read_artifact(str(root / "a" / "f.txt"))
    assert "None" not in e.value.message


def test_root_fds_are_released(tree):
    root, _ = tree
    ids = tree_identities(root)  # only descriptors that name the root's own tree count, nothing else in the process

    def fds() -> int:
        return open_fds_on(ids)

    assert fds() == 0
    for _ in range(200):
        Capabilities.host((root,))  # dropped at once: the finalizer closes
    gc.collect()
    assert fds() == 0
    held = [Files.host((root,)) for _ in range(5)]
    assert fds() == 5
    for f in held:
        f.close()
        f.close()  # idempotent
    assert fds() == 0
    with Files.host((root,)) as f:
        assert f.read_text(root / "a" / "f.txt") == "inside"
        assert fds() == 1
    gc.collect()
    assert fds() == 0


def test_hard_links_are_refused_and_never_truncated(tree):
    root, outside = tree
    os.link(outside / "secret.txt", root / "hl")
    f = host(tree).files
    with pytest.raises(CapabilityDenied):
        f.read_bytes(root / "hl")
    with pytest.raises(CapabilityDenied):
        f.write_text(root / "hl", "overwritten")
    assert (outside / "secret.txt").read_text() == "outside"  # not truncated either


def test_write_truncates_only_after_the_checks(tree):
    root, _ = tree
    f = host(tree).files
    f.write_text(root / "a" / "f.txt", "x")
    assert (root / "a" / "f.txt").read_text() == "x"
    os.mkfifo(root / "ff")
    with pytest.raises(OSError):
        f.write_text(root / "ff", "x")


def test_host_rmtree_removes_trees_without_following_links_and_reports_failures(tree):
    root, outside = tree
    d = root / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f").write_text("x")
    (d / "link").symlink_to(outside)
    f = host(tree).files
    f.rmtree(d)
    assert not d.exists()
    assert (outside / "secret.txt").exists()  # the link was removed, not followed
    with pytest.raises(OSError):
        f.rmtree(root / "missing")
    f.rmtree(root / "missing", ignore_errors=True)
    with pytest.raises(CapabilityDenied):
        f.rmtree(outside)


def test_a_bare_capabilities_does_not_exist_and_the_clock_is_wired(tree):
    with pytest.raises(TypeError):
        Capabilities()
    with pytest.raises(TypeError):
        Files()
    ctx = Context(caps=Capabilities.host((tree[0],), clock=lambda: 42.0))
    assert ctx.now() == 42.0
    explicit = Context(caps=Capabilities.host((tree[0],), clock=lambda: 42.0), now=lambda: 1.0)
    assert explicit.now() == 1.0


def test_git_off_is_a_problem_line_not_an_internal_error(tree):
    c = host(tree)
    problems: list[str] = []
    assert c.git.observe(object(), "T-1", problems) == []
    assert problems and "git" in problems[0]

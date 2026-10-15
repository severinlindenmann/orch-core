"""The host audit hook. Audit hooks cannot be removed, so every case runs in a subprocess."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from tests.conftest import child_env

PRELUDE = textwrap.dedent(
    """
    import json, os, subprocess, sys
    root, outside = sys.argv[1], sys.argv[2]
    from orch.host import audit, launcher
    from orch.ops.caps import Capabilities
    out = {}
    def t(name, fn):
        try:
            fn()
            out[name] = "ok"
        except PermissionError:
            out[name] = "denied"
        except Exception as e:
            out[name] = "error:" + type(e).__name__
    """
)

MAIN = PRELUDE + textwrap.dedent(
    """
    caps = Capabilities.host((root,))
    import ctypes  # preloaded, as the host would
    from orch.store.rootfs import RootFS
    fs = RootFS.at(root)
    audit.install([root])

    def rootfs_ops():  # the store's pinned root (#516): every call below works under the hook
        fs.write_atomic("d/a.txt", b"x")
        fs.append_durable("d/b.txt", b"y")
        assert fs.read_or_none("d/b.txt") == b"y" and sorted(fs.listdir("d")) == ["a.txt", "b.txt"]
        fs.replace("d/a.txt", "e/a.txt")
        fs.rmtree("e")
        fs.unlink("d/b.txt")

    t("rootfs_in_root", rootfs_ops)
    t("cwd_is_root", lambda: os.getcwd() == root or (_ for _ in ()).throw(PermissionError()))
    t("open_in_root", lambda: open(root + "/in.txt").close())
    t("open_in_root_write", lambda: open(root + "/new.txt", "w").close())
    t("open_relative_in_root_cwd", lambda: os.close(os.open("in.txt", os.O_RDONLY | os.O_NOFOLLOW)))
    t("open_relative_follow", lambda: open("in.txt").close())
    t("open_outside", lambda: open(outside + "/secret.txt").close())
    t("open_dotdot", lambda: open(root + "/../outside/secret.txt").close())
    t("open_etc", lambda: open("/etc/hosts").close())
    t("open_tty", lambda: open("/dev/tty").close())
    t("caps_files_in_root", lambda: caps.files.read_bytes(root + "/in.txt"))
    t("chdir_outside", lambda: os.chdir(outside))
    t("rename_out", lambda: os.rename(root + "/in.txt", outside + "/moved"))
    t("rename_in", lambda: os.rename(root + "/new.txt", root + "/new2.txt"))
    t("listdir_outside", lambda: os.listdir(outside))
    t("listdir_in", lambda: os.listdir(root))
    t("truncate_outside", lambda: os.truncate(outside + "/secret.txt", 0))
    t("symlink_outside", lambda: os.symlink("x", outside + "/sl"))
    t("link_outside", lambda: os.link(root + "/in.txt", outside + "/hl"))
    t("mkdir_outside", lambda: os.mkdir(outside + "/d"))
    t("remove_outside", lambda: os.remove(outside + "/secret.txt"))
    t("chmod_outside", lambda: os.chmod(outside + "/secret.txt", 0o600))
    t("dlopen", lambda: ctypes.CDLL(None))
    t("subprocess_run", lambda: subprocess.run(["true"]))
    t("subprocess_popen_shell", lambda: subprocess.Popen("true", shell=True))
    t("subprocess_fake_gitread", lambda: subprocess.Popen([sys.executable, "-I", "-m", "orch.gitread"]))
    t("os_system", lambda: os.system("true"))
    t("posix_spawn", lambda: os.posix_spawn("/usr/bin/true", ["true"], {}))
    t("execv", lambda: os.execv("/usr/bin/true", ["true"]))
    t("fork", lambda: os.fork())

    def forged_frame():
        ns = {"__name__": "orch.host.launcher", "subprocess": subprocess, "launcher": launcher}
        exec(
            "def spawn_gitread():\\n"
            "    return subprocess.Popen(launcher.gitread_argv(), executable='/bin/sh', env={}, close_fds=True)",
            ns,
        )
        ns["spawn_gitread"]()
    t("forged_frame_other_executable", forged_frame)

    def forged_name_same_args():
        ns = {"__name__": "orch.host.launcher", "subprocess": subprocess, "launcher": launcher}
        exec(
            "def spawn_gitread():\\n"
            "    return subprocess.Popen(launcher.gitread_argv(), env={}, close_fds=True)",
            ns,
        )
        ns["spawn_gitread"]()
    t("forged_frame_same_args", forged_name_same_args)

    def patched_launcher_subprocess():
        real = subprocess.Popen
        class Sneaky(real):
            def __init__(self, argv, **kw):
                super().__init__(argv, executable="/bin/echo", **kw)
        class Fake:
            Popen = Sneaky
            PIPE, DEVNULL = subprocess.PIPE, subprocess.DEVNULL
        launcher.subprocess = Fake
        try:
            launcher.spawn_gitread()
        finally:
            launcher.subprocess = subprocess
    t("patched_launcher_subprocess", patched_launcher_subprocess)

    def launch():
        p = launcher.spawn_gitread()
        p.stdin.close(); p.stdout.close(); p.wait(timeout=30)
    t("launcher", launch)
    print(json.dumps(out))
    """
)

OUTSIDE_CWD = PRELUDE + textwrap.dedent(
    """
    hook = audit.make_hook([root])   # no chdir: the cwd (outside) is not a root
    sys.addaudithook(hook)
    t("relative_open_from_outside_cwd", lambda: os.close(os.open("secret.txt", os.O_RDONLY | os.O_NOFOLLOW)))
    print(json.dumps(out))
    """
)


def _setup(tmp_path):
    base = tmp_path.resolve()
    root, outside = base / "root", base / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "in.txt").write_text("x")
    (outside / "secret.txt").write_text("y")
    return root, outside


def _run(script, root, outside, cwd):
    done = subprocess.run(
        [sys.executable, "-c", script, str(root), str(outside)],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=120,
        check=False,
        env=child_env(Path(root).parent),  # HOME is a temporary directory, never the owner's
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_hook_in_host_mode(tmp_path):
    root, outside = _setup(tmp_path)
    r = _run(MAIN, root, outside, cwd=outside)  # starts outside; install moves it into the root
    for ok in (
        "cwd_is_root",
        "open_in_root",
        "open_in_root_write",
        "open_relative_in_root_cwd",
        "caps_files_in_root",
        "rename_in",
        "listdir_in",
        "launcher",
        "rootfs_in_root",
    ):
        assert r[ok] == "ok", (ok, r)
    for refused in (
        "open_relative_follow",
        "open_outside",
        "open_dotdot",
        "open_etc",
        "open_tty",
        "chdir_outside",
        "rename_out",
        "listdir_outside",
        "truncate_outside",
        "symlink_outside",
        "link_outside",
        "mkdir_outside",
        "remove_outside",
        "chmod_outside",
        "dlopen",
        "subprocess_run",
        "subprocess_popen_shell",
        "subprocess_fake_gitread",
        "os_system",
        "posix_spawn",
        "execv",
        "fork",
        "forged_frame_other_executable",
        "forged_frame_same_args",
        "patched_launcher_subprocess",
    ):
        assert r[refused] == "denied", (refused, r)
    assert (outside / "secret.txt").read_text() == "y" and not (outside / "moved").exists()


def test_relative_open_from_a_cwd_outside_the_roots_is_refused(tmp_path):
    root, outside = _setup(tmp_path)
    r = _run(OUTSIDE_CWD, root, outside, cwd=outside)
    assert r["relative_open_from_outside_cwd"] == "denied"


PERSON = PRELUDE + textwrap.dedent(
    """
    ws = "0123456789abcdef0123456789abcdef"
    os.makedirs(root + "/person/" + ws + "/devices", exist_ok=True)
    os.makedirs(root + "/hosts/" + ws + "/keys", exist_ok=True)
    open(root + "/person/" + ws + "/devices/d.key", "w").close()
    open(root + "/hosts/" + ws + "/keys/wsk", "w").close()
    audit.warm()
    out["warm_skipped_never"] = sorted(m for m in sys.modules if audit._never(m))
    audit.install([root])
    t("open_person_key", lambda: open(root + "/person/" + ws + "/devices/d.key").close())
    t("open_person_dir_file", lambda: open(root + "/person/click.jsonl", "a").close())
    t("listdir_person", lambda: os.listdir(root + "/person"))
    t("open_host_key", lambda: open(root + "/hosts/" + ws + "/keys/wsk").close())
    t("import_click", lambda: __import__("orch.click.signer"))
    t("import_convenience", lambda: __import__("orch.custody.convenience"))
    t("import_keychain", lambda: __import__("orch.custody.keychain"))
    t("load_security", lambda: __import__("ctypes").CDLL("/System/Library/Frameworks/Security.framework/Security"))
    print(json.dumps(out))
    """
)


def test_the_host_never_handles_person_custody(tmp_path):
    root, outside = _setup(tmp_path)
    r = _run(PERSON, root, outside, cwd=root)
    assert r["warm_skipped_never"] == []  # warm imports every orch module except the custody and signer ones
    assert r["open_host_key"] == "ok"
    for refused in (
        "open_person_key",
        "open_person_dir_file",
        "listdir_person",
        "import_click",
        "import_convenience",
        "import_keychain",
        "load_security",
    ):
        assert r[refused] == "denied", (refused, r)


WALKS = PRELUDE + textwrap.dedent(
    """
    ws = "0123456789abcdef0123456789abcdef"
    dev = "d_" + "1" * 32
    for d in ("/person/" + ws + "/devices", "/hosts/" + ws + "/person"):
        os.makedirs(root + d, exist_ok=True)
    with open(root + "/person/" + ws + "/devices/" + dev + ".key", "w") as f:
        f.write("SECRET")
    with open(root + "/hosts/" + ws + "/person/dk.key.json", "w") as f:
        f.write("SECRET")
    audit.warm()
    audit.install([root])
    flags = os.O_RDONLY | os.O_NOFOLLOW
    def dir_fd_walk():
        fd = os.open(root, flags)
        for part in ("person", ws, "devices"):
            fd = os.open(part, flags, dir_fd=fd)
        os.close(os.open(dev + ".key", flags, dir_fd=fd))
    def chdir_walk():
        os.chdir(root)
        for part in ("person", ws, "devices"):
            os.chdir(part)
        os.close(os.open(dev + ".key", flags))
    t("dir_fd_walk", dir_fd_walk)
    os.chdir(root)
    t("chdir_walk", chdir_walk)
    os.chdir(root)
    t("legacy_abs", lambda: open(root + "/hosts/" + ws + "/person/dk.key.json").close())
    def legacy_walk():
        fd = os.open(root, flags)
        for part in ("hosts", ws, "person"):
            fd = os.open(part, flags, dir_fd=fd)
        os.close(os.open("dk.key.json", flags, dir_fd=fd))
    t("legacy_dir_fd_walk", legacy_walk)
    print(json.dumps(out))
    """
)


def test_the_host_cannot_walk_to_a_person_key_relatively(tmp_path):
    root, outside = _setup(tmp_path)
    r = _run(WALKS, root, outside, cwd=root)
    for refused in ("dir_fd_walk", "chdir_walk", "legacy_abs", "legacy_dir_fd_walk"):
        assert r[refused] == "denied", (refused, r)

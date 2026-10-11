"""The host audit hook. Audit hooks cannot be removed, so every case runs in a subprocess."""

import json
import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(
    """
    import json, os, subprocess, sys
    root, outside = sys.argv[1], sys.argv[2]
    from orch.host import audit, launcher
    from orch.ops.caps import Capabilities
    import orch.ops.caps
    caps = Capabilities.host((root,))
    audit.install([root])
    out = {}
    def t(name, fn):
        try:
            fn()
            out[name] = "ok"
        except PermissionError as e:
            out[name] = "denied"
        except Exception as e:
            out[name] = "error:" + type(e).__name__
    t("open_in_root", lambda: open(root + "/in.txt").close())
    t("open_in_root_write", lambda: open(root + "/new.txt", "w").close())
    t("open_outside", lambda: open(outside + "/secret.txt").close())
    t("open_dotdot", lambda: open(root + "/../outside/secret.txt").close())
    t("open_relative", lambda: open("in.txt").close())
    t("open_etc", lambda: open("/etc/hosts").close())
    t("open_tty", lambda: open("/dev/tty").close())
    t("caps_files_in_root", lambda: caps.files.read_bytes(root + "/in.txt"))
    t("subprocess_run", lambda: subprocess.run(["true"]))
    t("subprocess_popen_shell", lambda: subprocess.Popen("true", shell=True))
    t("subprocess_fake_gitread", lambda: subprocess.Popen([sys.executable, "-I", "-m", "orch.gitread"]))
    t("os_system", lambda: os.system("true"))
    t("posix_spawn", lambda: os.posix_spawn("/usr/bin/true", ["true"], {}))
    t("execv", lambda: os.execv("/usr/bin/true", ["true"]))
    t("fork", lambda: os.fork())
    def launch():
        p = launcher.spawn_gitread()
        p.stdin.close(); p.stdout.close(); p.wait(timeout=30)
    t("launcher", launch)
    print(json.dumps(out))
    """
)


def run_hooked(tmp_path):
    root = (tmp_path / "root").resolve() if (tmp_path / "root").exists() else None
    base = tmp_path.resolve()
    root, outside = base / "root", base / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "in.txt").write_text("x")
    (outside / "secret.txt").write_text("y")
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(root), str(outside)],
        capture_output=True,
        text=True,
        cwd=root,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_hook_in_host_mode(tmp_path):
    r = run_hooked(tmp_path)
    assert r["open_in_root"] == "ok"
    assert r["open_in_root_write"] == "ok"
    assert r["caps_files_in_root"] == "ok"
    for refused in ("open_outside", "open_dotdot", "open_relative", "open_etc", "open_tty"):
        assert r[refused] == "denied", (refused, r)
    for refused in (
        "subprocess_run",
        "subprocess_popen_shell",
        "subprocess_fake_gitread",
        "os_system",
        "posix_spawn",
        "execv",
        "fork",
    ):
        assert r[refused] == "denied", (refused, r)
    assert r["launcher"] == "ok", r

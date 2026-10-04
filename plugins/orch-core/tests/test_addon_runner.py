import os
import sys
import time
from pathlib import Path

import pytest

from addon_fixtures import GOOD
from orch.addons.api import AddonContext
from orch.addons.manifest import parse_manifest
from orch.addons.runner import AddonRunError, RunResult, SubprocessRunner, rendering, scrubbed_env


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        return RunResult(tuple(argv), 0, "out", "")


def _ctx(ws, runner, **over):
    m = parse_manifest({**GOOD, **over})
    return AddonContext(ws, m.name, manifest=m, runner=runner).provider_context()


def test_run_allows_only_listed_binaries(ws):
    rec = Recorder()
    ctx = _ctx(ws, rec)
    assert ctx.run(["git", "status"]).stdout == "out"
    assert rec.calls == [(("git", "status"), 20.0)]
    for argv in (["gh", "pr", "list"], ["/usr/bin/git", "status"], "git status", [], ["git", 3]):
        with pytest.raises(AddonRunError):
            ctx.run(argv)


def test_setting_binary_must_be_an_absolute_path(ws):
    from orch.addons import userfiles
    over = {"binaries": ["setting:cli_path"],
            "settings_schema": [*GOOD["settings_schema"], {"key": "cli_path", "label": "CLI", "type": "text"}]}
    rec = Recorder()
    userfiles.save_addon_config(ws.root, "hello-status", {"cli_path": "relative/tix"})
    with pytest.raises(AddonRunError):
        _ctx(ws, rec, **over).run(["relative/tix", "list"])
    tix = str(Path(ws.root, "bin", "tix").resolve())
    userfiles.save_addon_config(ws.root, "hello-status", {"cli_path": tix})
    _ctx(ws, rec, **over).run([tix, "list"])
    assert rec.calls[-1][0] == (tix, "list")


def test_run_refuses_while_a_page_renders(ws):
    ctx = _ctx(ws, Recorder())
    with rendering():
        with pytest.raises(AddonRunError, match="while a page renders"):
            ctx.run(["git", "status"])


def test_env_is_scrubbed():
    env = scrubbed_env(("GH_HOST",), environ={"PATH": "/bin", "HOME": "/h", "GH_TOKEN": "t", "GH_HOST": "ghe",
                                              "DATABRICKS_TOKEN": "d", "AWS_SECRET_ACCESS_KEY": "a"})
    assert env == {"PATH": "/bin", "HOME": "/h", "GH_HOST": "ghe"}


def test_env_refuses_orch_prefix_even_if_requested():
    env = scrubbed_env(("ORCH_HARNESS", "GH_HOST"), environ={"PATH": "/bin", "ORCH_HARNESS": "claude-code",
                                                              "ORCH_STATE_DIR": "/secret", "GH_HOST": "ghe"})
    assert env == {"PATH": "/bin", "GH_HOST": "ghe"}


def test_setting_binary_that_does_not_exist_is_refused(ws):
    from orch.addons import userfiles
    over = {"binaries": ["setting:cli_path"],
            "settings_schema": [*GOOD["settings_schema"], {"key": "cli_path", "label": "CLI", "type": "text"}]}
    missing = str(Path(ws.root, "bin", "does-not-exist").resolve())
    userfiles.save_addon_config(ws.root, "hello-status", {"cli_path": missing})
    # allowed by the manifest's binaries allowlist (it is an absolute path), but the real
    # SubprocessRunner must still refuse it because the file is not actually there.
    with pytest.raises(AddonRunError, match="not installed"):
        _ctx(ws, None, **over).run([missing, "list"])


def test_subprocess_runner_uses_argv_without_a_shell(tmp_path):
    exe = Path(sys.executable)
    runner = SubprocessRunner(env_names=(), cwd=tmp_path)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = str(exe.parent) + os.pathsep + old
    try:
        r = runner([exe.name, "-c", "import sys; print(sys.argv[1])", "a;echo pwned"], 10)
    finally:
        os.environ["PATH"] = old
    assert r.returncode == 0 and r.stdout.strip() == "a;echo pwned"


def test_subprocess_runner_timeout_and_missing_binary(tmp_path):
    exe = Path(sys.executable)
    runner = SubprocessRunner(env_names=(), cwd=tmp_path)
    with pytest.raises(AddonRunError, match="not installed"):
        runner(["definitely-not-a-binary-xyz"], 5)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = str(exe.parent) + os.pathsep + old
    try:
        with pytest.raises(AddonRunError, match="timed out"):
            runner([exe.name, "-c", "import time; time.sleep(5)"], 0.5)
    finally:
        os.environ["PATH"] = old


def test_subprocess_runner_refuses_while_a_page_renders(tmp_path):
    runner = SubprocessRunner(env_names=(), cwd=tmp_path)
    with rendering():
        with pytest.raises(AddonRunError, match="while a page renders"):
            runner([sys.executable, "-c", "pass"], 5)


def test_timeout_kills_the_whole_process_group_including_grandchildren(tmp_path):
    exe = Path(sys.executable)
    runner = SubprocessRunner(env_names=(), cwd=tmp_path)
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn_grandchild.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "pid_file = sys.argv[1]\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "open(pid_file, 'w').write(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = str(exe.parent) + os.pathsep + old
    try:
        with pytest.raises(AddonRunError, match="timed out"):
            runner([exe.name, str(script), str(pid_file)], 1.0)
    finally:
        os.environ["PATH"] = old

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.05)
    grandchild_pid = int(pid_file.read_text())

    deadline = time.monotonic() + 3
    dead = False
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except (ProcessLookupError, PermissionError):
            dead = True
            break
        time.sleep(0.1)
    assert dead, "the grandchild process is still alive"


def test_output_over_the_cap_is_refused_and_the_process_is_killed(tmp_path):
    exe = Path(sys.executable)
    runner = SubprocessRunner(env_names=(), cwd=tmp_path)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = str(exe.parent) + os.pathsep + old
    try:
        with pytest.raises(AddonRunError, match="output too large"):
            runner([exe.name, "-c", "import sys; sys.stdout.write('x' * 20_000_000); sys.stdout.flush()"], 10)
    finally:
        os.environ["PATH"] = old


def test_timeout_kills_promptly_before_a_scheduled_grandchild_fires(tmp_path):
    """Regression for the ~4 s-late kill: a reader blocked in a read with no data yet must not delay
    the kill. The grandchild is scheduled to write its marker 2 s in; with timeout=0.8 s, `run` must
    come back well under that 2 s mark, and the marker must never appear."""
    exe = Path(sys.executable)
    runner = SubprocessRunner(env_names=(), cwd=tmp_path)
    marker = tmp_path / "marker.txt"
    grandchild_script = tmp_path / "grandchild_timer.py"
    grandchild_script.write_text(
        "import sys, time\n"
        "time.sleep(2)\n"
        "with open(sys.argv[1], 'w') as f:\n"
        "    f.write('x')\n",
        encoding="utf-8",
    )
    parent_script = tmp_path / "spawn_timer.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "grandchild_script, marker = sys.argv[1], sys.argv[2]\n"
        "subprocess.Popen([sys.executable, grandchild_script, marker])\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = str(exe.parent) + os.pathsep + old
    start = time.monotonic()
    try:
        with pytest.raises(AddonRunError, match="timed out"):
            runner([exe.name, str(parent_script), str(grandchild_script), str(marker)], 0.8)
    finally:
        os.environ["PATH"] = old
    elapsed = time.monotonic() - start
    assert elapsed < 1.5, f"run() took {elapsed:g}s to come back"

    time.sleep(1.5)  # well past the grandchild's scheduled 2 s mark
    assert not marker.exists(), "the grandchild fired after all"


def test_normal_exit_kills_a_backgrounded_grandchild_and_leaves_no_reader_thread(tmp_path):
    """A reader uses a blocking read on the raw fd; if the child exits but leaves a grandchild
    holding the inherited stdout pipe open, the reader must not hang waiting for EOF forever. `run`
    collects the output already written, kills the process group (closing the grandchild's copy of
    the pipe), and returns promptly with no reader thread left behind."""
    import threading

    from orch.addons.runner import _ReaderThread

    exe = Path(sys.executable)
    runner = SubprocessRunner(env_names=(), cwd=tmp_path)
    script = tmp_path / "print_and_spawn.py"
    script.write_text(
        "import subprocess, sys\n"
        "print('hi', flush=True)\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])\n",
        encoding="utf-8",
    )
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = str(exe.parent) + os.pathsep + old
    start = time.monotonic()
    try:
        r = runner([exe.name, str(script)], 5.0)
    finally:
        os.environ["PATH"] = old
    elapsed = time.monotonic() - start

    assert r.stdout == "hi\n"
    assert elapsed < 1.0, f"run() took {elapsed:g}s to come back"
    assert not any(isinstance(t, _ReaderThread) for t in threading.enumerate())


def test_kill_live_processes_cuts_a_long_running_call_short(tmp_path):
    """A process the scheduler started (e.g. a long-poll fetch, timeout up to 35 s) must die at once when the
    server shuts down, not hold up exit for however long it had left: kill_live_processes() force-kills every
    SubprocessRunner call currently in flight, from any thread."""
    import threading

    from orch.addons.runner import kill_live_processes

    exe = Path(sys.executable)
    runner = SubprocessRunner(env_names=(), cwd=tmp_path)
    result: dict = {}

    def go():
        try:
            runner([exe.name, "-c", "import time; time.sleep(30)"], 30.0)
        except AddonRunError as e:
            result["error"] = str(e)

    t = threading.Thread(target=go)
    start = time.monotonic()
    t.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if kill_live_processes():
            break
        time.sleep(0.02)
    t.join(timeout=5)
    elapsed = time.monotonic() - start

    assert not t.is_alive()
    assert elapsed < 5, f"shutdown took {elapsed:g}s instead of killing the process promptly"


def test_kill_live_processes_is_a_noop_with_nothing_running():
    from orch.addons.runner import kill_live_processes
    assert kill_live_processes() == 0

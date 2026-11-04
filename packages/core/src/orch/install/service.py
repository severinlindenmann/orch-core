"""Run a part in the background as the person: a launchd agent (macOS), ``systemd --user`` (Linux) or a pidfile
fallback. The runner never needs root. ``FakeRunner`` in the tests replaces all three."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from orch import childenv
from orch.privdir import make_private_dir

from .state import Layout

__all__ = ["Runner", "PidfileRunner", "LaunchdRunner", "SystemdRunner", "pick_runner"]


class Runner(Protocol):
    kind: str

    def start(self, part: str, argv: Sequence[str], env: Mapping[str, str], log: Path) -> dict[str, Any]: ...
    def stop(self, part: str) -> None: ...
    def pause(self, part: str) -> None:  # stopped until resume(), also after a reboot; the service stays installed
        ...
    def resume(self, part: str) -> None: ...
    def info(self, part: str) -> dict[str, Any]:  # {"running": bool, "pid": int | None, "service": str}
        ...


def _tag(layout: Layout) -> str:
    return hashlib.sha256(str(layout.root).encode()).hexdigest()[:6]


class PidfileRunner:
    kind = "pidfile"

    def __init__(self, layout: Layout) -> None:
        self.l = layout

    def _pidfile(self, part: str) -> Path:
        return self.l.root / "parts" / part / "pid"

    def _pid(self, part: str) -> int | None:
        try:
            pid = int(self._pidfile(part).read_text())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def start(self, part, argv, env, log):
        self.stop(part)
        make_private_dir(log.parent)
        with open(log, "ab") as lf:
            p = subprocess.Popen(
                list(argv),
                env=childenv.sanitize({**os.environ, **env}, absolute_pythonpath=True),
                stdin=subprocess.DEVNULL,
                stdout=lf,
                stderr=lf,
                start_new_session=True,
            )
        self._pidfile(part).parent.mkdir(parents=True, exist_ok=True)
        self._pidfile(part).write_text(str(p.pid))
        cmd = self._cmdfile(part)  # what resume() starts again
        cmd.write_text(json.dumps({"argv": list(argv), "env": dict(env), "log": str(log)}))
        cmd.chmod(0o600)
        return {"service": f"pidfile:{self._pidfile(part)}", "pid": p.pid}

    def _cmdfile(self, part: str) -> Path:
        return self.l.root / "parts" / part / "cmd.json"

    def pause(self, part):
        self.stop(part)  # nothing restarts a pidfile process after a reboot

    def resume(self, part):
        try:
            rec = json.loads(self._cmdfile(part).read_text())
            self.start(part, rec["argv"], rec["env"], Path(rec["log"]))
        except (OSError, ValueError, KeyError):
            raise RuntimeError(f"cannot start {part} again: reinstall it with orch install {part}") from None

    def stop(self, part):
        pid = self._pid(part)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    time.sleep(0.1)
                    os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self._pidfile(part).unlink(missing_ok=True)

    def info(self, part):
        pid = self._pid(part)
        return {"running": pid is not None, "pid": pid, "service": f"pidfile:{self._pidfile(part)}"}


class LaunchdRunner:
    kind = "launchd"

    def __init__(self, layout: Layout, run=subprocess.run) -> None:
        self.l, self.run = layout, run
        self.uid = os.getuid()

    def label(self, part: str) -> str:
        return f"io.orch.{_tag(self.l)}.{part}"

    def plist(self, part: str) -> Path:
        return self.l.home / "Library" / "LaunchAgents" / f"{self.label(part)}.plist"

    def start(self, part, argv, env, log):
        self.stop(part)
        path = self.plist(part)
        path.parent.mkdir(parents=True, exist_ok=True)
        make_private_dir(log.parent)
        path.write_bytes(
            plistlib.dumps(
                {
                    "Label": self.label(part),
                    "ProgramArguments": list(argv),
                    "EnvironmentVariables": dict(env),
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "StandardOutPath": str(log),
                    "StandardErrorPath": str(log),
                }
            )
        )
        self._load(part)
        return {"service": self.label(part), "pid": self.info(part)["pid"]}

    def _load(self, part: str) -> None:
        path = self.plist(part)
        # a label an earlier `orch stop` disabled would refuse to load
        self.run(["launchctl", "enable", f"gui/{self.uid}/{self.label(part)}"], capture_output=True)
        # bootout answers before the job is gone: a bootstrap right after it fails with exit 5 (re-run of an install)
        for attempt in range(10):
            r = self.run(["launchctl", "bootstrap", f"gui/{self.uid}", str(path)], capture_output=True)
            if r.returncode == 0:
                break
            if attempt == 9:
                self.run(["launchctl", "bootstrap", f"gui/{self.uid}", str(path)], check=True, capture_output=True)
            time.sleep(0.5)

    def pause(self, part):
        self.run(["launchctl", "disable", f"gui/{self.uid}/{self.label(part)}"], capture_output=True)
        self.run(["launchctl", "bootout", f"gui/{self.uid}/{self.label(part)}"], capture_output=True)

    def resume(self, part):
        if not self.plist(part).exists():
            raise RuntimeError(f"cannot start {part} again: reinstall it with orch install {part}")
        self._load(part)

    def stop(self, part):
        self.run(["launchctl", "bootout", f"gui/{self.uid}/{self.label(part)}"], capture_output=True)
        self.plist(part).unlink(missing_ok=True)

    def info(self, part):
        r = self.run(["launchctl", "list", self.label(part)], capture_output=True, text=True)
        pid = None
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if '"PID"' in line:
                    pid = int(line.split("=")[1].strip(" ;"))
        return {"running": pid is not None, "pid": pid, "service": self.label(part)}


class SystemdRunner:
    kind = "systemd"

    def __init__(self, layout: Layout, run=subprocess.run) -> None:
        self.l, self.run = layout, run

    def unit(self, part: str) -> str:
        return f"orch-{_tag(self.l)}-{part}.service"

    def path(self, part: str) -> Path:
        return self.l.home / ".config" / "systemd" / "user" / self.unit(part)

    def start(self, part, argv, env, log):
        path = self.path(part)
        path.parent.mkdir(parents=True, exist_ok=True)
        make_private_dir(log.parent)
        envs = "".join(f"Environment={shlex.quote(f'{k}={v}')}\n" for k, v in env.items())
        path.write_text(
            f"[Unit]\nDescription=orch {part}\n\n[Service]\nExecStart={shlex.join(argv)}\n{envs}Restart=on-failure\n"
            f"StandardOutput=append:{log}\nStandardError=append:{log}\n\n[Install]\nWantedBy=default.target\n"
        )
        self.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
        self.run(["systemctl", "--user", "enable", "--now", self.unit(part)], check=True, capture_output=True)
        self.run(["systemctl", "--user", "restart", self.unit(part)], check=True, capture_output=True)
        return {"service": self.unit(part), "pid": self.info(part)["pid"]}

    def pause(self, part):
        self.run(["systemctl", "--user", "disable", "--now", self.unit(part)], capture_output=True)

    def resume(self, part):
        if not self.path(part).exists():
            raise RuntimeError(f"cannot start {part} again: reinstall it with orch install {part}")
        self.run(["systemctl", "--user", "enable", "--now", self.unit(part)], check=True, capture_output=True)

    def stop(self, part):
        self.run(["systemctl", "--user", "disable", "--now", self.unit(part)], capture_output=True)
        self.path(part).unlink(missing_ok=True)
        self.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    def info(self, part):
        r = self.run(
            ["systemctl", "--user", "show", self.unit(part), "-p", "MainPID", "-p", "ActiveState"],
            capture_output=True,
            text=True,
        )
        kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        pid = int(kv.get("MainPID", "0") or 0) or None
        return {"running": kv.get("ActiveState") == "active", "pid": pid, "service": self.unit(part)}


def pick_runner(layout: Layout, env: Mapping[str, str]) -> Runner:
    want = env.get("ORCH_INSTALL_SERVICE", "auto")
    if want == "pidfile":
        return PidfileRunner(layout)
    if want == "launchd" or (want == "auto" and sys.platform == "darwin"):
        return LaunchdRunner(layout)
    if want == "systemd" or (want == "auto" and sys.platform.startswith("linux") and _systemd_user()):
        return SystemdRunner(layout)
    return PidfileRunner(layout)


def _systemd_user() -> bool:
    try:
        return subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            timeout=5,
            env=childenv.sanitize(os.environ),
        ).returncode in (0, 1)
    except (OSError, subprocess.SubprocessError):
        return False

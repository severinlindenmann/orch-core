"""Starting the remover (#1463, Opus design check changes 11, 13, 14): the order file, the pre-flight dry run and the detached
launch of ``<abs orch> host prune --hub-order``. Argv lists only (no shell). The unit name is built from the hex
``removal_id`` alone. The environment is an allow list; ``--i-know-dev`` is never passed. Which runner the hub was installed
with is read from the install record (``<state>/install/install.json``) two directories above the hub's data dir."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .keys.fsutil import StateError, read_file, write_atomic

ORDER_FILE = "remove-order.json"
RESULT_FILE = "remove-result.json"
ARGV_TAIL = ("host", "prune", "--hub-order")
HEX32 = re.compile(r"[0-9a-f]{32}")
#: the only variables the remover gets (it scrubs the rest itself): home, path, locale and the state-dir selectors the hub started with
ENV_ALLOW = ("HOME", "PATH", "LANG", "ORCH_STATE_DIR", "XDG_CONFIG_HOME", "ORCH_INSTALL_SERVICE")
DRY_RUN_TIMEOUT_S = 60


class LaunchError(Exception):
    pass


def install_root(state_dir: Path) -> Path | None:
    """``<root>/install`` when the hub state is ``<root>/install/parts/hub/data`` (the installer's layout), else None."""
    p = Path(os.path.realpath(state_dir))
    if p.name == "data" and p.parent.name == "hub" and p.parent.parent.name == "parts":
        return p.parent.parent.parent
    return None


def find_orch(env: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if env is None else env
    found = shutil.which("orch", path=env.get("PATH"))
    if found is None:
        sib = Path(sys.argv[0]).resolve().parent / "orch"
        found = str(sib) if sib.is_file() else None
    return None if found is None else Path(os.path.realpath(found))


class Launcher:
    def __init__(
        self,
        state_dir: Path,
        *,
        env: Mapping[str, str] | None = None,
        run: Callable[..., Any] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
        orch: Path | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.env = os.environ if env is None else env
        self._run, self._popen, self._orch = run, popen, orch

    # --- the install record --------------------------------------------------------------------------------------

    def record(self) -> dict[str, Any]:
        root = install_root(self.state_dir)
        if root is None:
            return {}
        try:
            d = json.loads(read_file(root / "install.json"))
        except (StateError, ValueError):
            try:
                d = json.loads((root / "install.json").read_text())
            except (OSError, ValueError):
                return {}
        return d if type(d) is dict else {}

    def parts(self) -> frozenset[str]:
        p = self.record().get("parts")
        return frozenset(k for k in p if k in ("hub", "relay", "publish")) if type(p) is dict else frozenset()

    def runner_kind(self) -> str | None:
        hub = (self.record().get("parts") or {}).get("hub")
        kind = hub.get("runner") if type(hub) is dict else None
        return kind if kind in ("systemd", "launchd", "pidfile") else None

    # --- files -----------------------------------------------------------------------------------------------------

    def write_order(self, state_dir: Path, doc: Mapping[str, Any]) -> None:
        write_atomic(Path(state_dir) / ORDER_FILE, json.dumps(dict(doc), sort_keys=True).encode())

    def remove_order(self, state_dir: Path) -> None:
        Path(state_dir, ORDER_FILE).unlink(missing_ok=True)

    def read_result(self, state_dir: Path) -> dict[str, Any] | None:
        try:
            d = json.loads(read_file(Path(state_dir) / RESULT_FILE))
        except (StateError, ValueError):
            return None
        return d if type(d) is dict else None

    # --- argv and environment --------------------------------------------------------------------------------------

    def orch(self) -> Path:
        o = self._orch or find_orch(self.env)
        if o is None:
            raise LaunchError("The orch tool was not found on this computer.")
        return o

    def argv(self, *extra: str) -> list[str]:
        return [str(self.orch()), *ARGV_TAIL, *extra]

    def child_env(self) -> dict[str, str]:
        return {k: self.env[k] for k in ENV_ALLOW if k in self.env}

    # --- pre-flight ---------------------------------------------------------------------------------------------------

    def dry_run(self, state_dir: Path) -> tuple[bool, str]:
        """The same argv and environment as the real launch, plus ``--dry-run``; passes only with exit status 0."""
        if install_root(self.state_dir) is None or self.runner_kind() is None:
            return False, "This hub was not set up with the orch installer, so it cannot remove itself."
        try:
            argv = self.argv("--dry-run")
        except LaunchError as e:
            return False, str(e)
        try:
            r = self._run(
                argv,
                env=self.child_env(),
                cwd="/",
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=DRY_RUN_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False, "The removal check could not run."
        if r.returncode == 0:
            return True, ""
        res = self.read_result(state_dir)
        plain = str((res or {}).get("message_plain") or "").strip()
        return False, plain or "The removal check refused. Nothing was changed."

    # --- launch -------------------------------------------------------------------------------------------------------

    def launch(self, state_dir: Path, removal_id: str) -> None:
        if HEX32.fullmatch(removal_id) is None:
            raise LaunchError("bad removal id")
        kind = self.runner_kind()
        argv = self.argv()
        env = self.child_env()
        if kind == "systemd":
            cmd: Sequence[str] = [
                "systemd-run",
                "--user",
                "--unit",
                f"orch-hub-remove-{removal_id}",
                "--collect",
                "--no-block",
                *[f"--setenv={k}={v}" for k, v in env.items()],
                "--",
                *argv,
            ]
            r = self._run(cmd, env=env, cwd="/", stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
            if r.returncode != 0:
                raise LaunchError("systemd-run failed")
        elif kind == "launchd":
            cmd = ["launchctl", "submit", "-l", f"io.orch.hub-remove.{removal_id}", "--", "/usr/bin/env", *[f"{k}={v}" for k, v in env.items()], *argv]
            r = self._run(cmd, env=env, cwd="/", stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
            if r.returncode != 0:
                raise LaunchError("launchctl submit failed")
        elif kind == "pidfile":
            self._popen(
                argv,
                env=env,
                cwd="/",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        else:
            raise LaunchError("no runner")

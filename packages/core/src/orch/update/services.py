"""The real seams of ``orch update``: this machine's services, the installer, the release source.

Services go through :mod:`orch.lifecycle.units` (launchctl bootout / systemd stop, the pidfile runner), exactly what
``orch stop`` and ``orch start`` use. The new code starts through ITS OWN ``orch host enable`` (it writes the units
that run the new version), then every other service that was running is started.
"""

from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from orch import childenv
from orch.lifecycle.units import ORDER_START, ORDER_STOP, Env, LifecycleError, Svc, discover, start_svc, stop_svc
from orch.ops.errors import OrchError

from .layout import InstallLayout

__all__ = ["GithubSource", "LifecycleServices", "run_orch", "uv_install"]

REPO = "severinlindenmann/orch-core"
QUARANTINE_KEYS = ("host", "dashboard", "hub", "relay", "publish")  # the runner and the records timer stay off


def run_orch(orch_bin: Path, args: Sequence[str]) -> tuple[int, str]:
    """Run a subprocess of one version's entry point (never from a workspace folder, never with agent variables)."""
    env = childenv.sanitize(os.environ)
    for k in ("ORCH_GRANT", "ORCH_SESSION"):
        env.pop(k, None)
    try:
        r = subprocess.run(
            [str(orch_bin), *args], capture_output=True, text=True, timeout=900, env=env, cwd=str(Path.home())
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def uv_install(layout: InstallLayout, ver: str, files: Mapping[str, Path]) -> None:
    """``uv tool install --require-hashes`` of the verified files into ``tools/orch-<ver>`` (its own UV_TOOL_DIR)."""
    tool = layout.tool_dir(ver)
    tool.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv = ["uv", "tool", "install", "--force", "--python", "3.14", "--require-hashes",
            "--constraints", str(files["constraints"]), str(files["core"])]  # fmt: skip
    for role in ("dashboard", "hub", "relay", "publish"):
        if role in files:
            argv += ["--with", str(files[role])]
    env = childenv.sanitize(os.environ)
    env["UV_TOOL_DIR"], env["UV_TOOL_BIN_DIR"] = str(tool), str(tool / "bin")
    r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=1800)
    if r.returncode != 0:
        raise OrchError("update.unverified", f"uv could not install the new version: {(r.stdout + r.stderr)[-300:]}")


class GithubSource:
    """Release assets of ``severinlindenmann/orch-core``. The repo is a code constant, never configuration."""

    def __init__(self, opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.open = opener

    def _get(self, url: str, limit: int) -> bytes:
        if urlsplit(url).scheme != "https":
            raise OrchError("update.unreachable", "only https downloads are allowed")
        try:
            with self.open(url, timeout=30) as r:
                data = r.read(limit + 1)
        except (urllib.error.URLError, OSError) as exc:
            raise OrchError(
                "update.unreachable", f"could not reach the release server ({exc}); nothing was changed"
            ) from None
        if len(data) > limit:
            raise OrchError("update.unverified", "a download is larger than the signed size")
        return data

    def envelope(self, channel: str) -> bytes:
        return self._get(f"https://github.com/{REPO}/releases/download/channel-{channel}/channel.json", 1 << 20)

    def fetch(self, entry: Mapping[str, Any]) -> bytes:
        return self._get(str(entry["url"]), int(entry["size"]))


class LifecycleServices:
    def __init__(
        self,
        e: Env,
        run: Callable[[Path, Sequence[str]], tuple[int, str]] = run_orch,
        http_get: Callable[[str], int] | None = None,
    ) -> None:
        self.e, self.run, self.get = e, run, http_get or _http_status

    def _svcs(self) -> list[Svc]:
        return discover(self.e)

    def running(self) -> list[str]:
        return [s.key for s in self._svcs() if s.installed and s.kind != "unsupported" and s.state == "running"]

    def stop(self) -> None:
        svcs = {s.key: s for s in self._svcs()}
        for key in ORDER_STOP:
            s = svcs.get(key)
            if s is not None and s.installed and s.state == "running":
                try:
                    stop_svc(self.e, s)
                except LifecycleError as exc:
                    raise OrchError("migrate.running", str(exc)) from None

    def start(self, orch_bin: Path, names: Sequence[str], *, quarantine: bool) -> None:
        want = [k for k in ORDER_START if k in names and (not quarantine or k in QUARANTINE_KEYS)]
        if "host" in want:  # the new (or old) code writes its own units; then the service manager starts them
            self.run(orch_bin, ["host", "enable", "--json"])
        by = {s.key: s for s in self._svcs()}
        for key in want:
            s = by.get(key)
            if s is not None and s.installed:
                try:
                    start_svc(self.e, s)
                except LifecycleError as exc:
                    raise OrchError("host.unavailable", str(exc)) from None

    def health(self, *, quarantine: bool) -> list[str]:
        out: list[str] = []
        if not self.e.answers(self.e.state / "run" / "control" / "host.sock"):
            out.append("the host does not answer")
        for s in self._svcs():
            if not s.installed or s.key in ("host", "runner", "records"):
                continue
            if s.key == "dashboard" and s.state != "running":
                out.append("the dashboard is not running")
            elif s.key in ("hub", "relay", "publish"):
                if s.state != "running":
                    out.append(f"{s.label} is not running")
                elif s.detail and self.get(s.detail.rstrip("/") + "/health") != 200:
                    out.append(f"{s.label} does not answer /health")
        return out


def _http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - loopback or the part's own address
            return int(r.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return 0

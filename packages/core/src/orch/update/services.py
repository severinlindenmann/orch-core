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

from .layout import InstallLayout, Record, requirements_text
from .quarantine import QUARANTINE_KEYS
from .uvenv import check_uv, install_argv, uv_env, venv_argv

__all__ = ["GithubSource", "LifecycleServices", "run_orch", "uv_install"]

REPO = "severinlindenmann/orch-core"
PYTHON_VERSION = "3.14"


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


def uv_install(
    layout: InstallLayout, ver: str, files: Mapping[str, Path], rec: Record, signed: Sequence[Mapping[str, Any]]
) -> None:
    """Install ``ver`` the way install.sh does: the pinned uv, a venv in ``tools/orch-<ver>/venv``, then
    ``uv pip install --require-hashes --no-deps`` of the verified files only. ``.complete`` is written last."""
    import hashlib
    import shutil

    uv = check_uv(layout.data, rec.uv.get("path") or "")
    pinned = [(f["role"], files[f["role"]], f["sha256"]) for f in signed]  # the SIGNED hashes, never a re-hash of disk
    held: dict[str, bytes] = {}
    for role, path, sha in pinned:
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha or len(data) != next(f["size"] for f in signed if f["role"] == role):
            raise OrchError(
                "update.unverified", f"{path.name} no longer matches the signed release; nothing was installed"
            )
        held[role] = data
    reqs = requirements_text(held["constraints"].decode("utf-8"), pinned)
    tool = layout.tool_dir(ver)
    if tool.exists():
        shutil.rmtree(tool)  # an unfinished earlier try (no .complete)
    tool.mkdir(parents=True, mode=0o700)
    venv = tool / "venv"
    env = uv_env()

    def uv_run(argv: list[str], timeout: int = 1800) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)

    find = [uv, "python", "find", "--no-config", PYTHON_VERSION]
    py = uv_run(find).stdout.strip()
    if not py:
        uv_run([uv, "python", "install", "--no-config", PYTHON_VERSION])
        py = uv_run(find).stdout.strip()
    if not py:
        raise OrchError("update.unverified", f"Python {PYTHON_VERSION} is not available")
    req_file = tool / "requirements.txt"
    req_file.write_text(reqs)
    for argv in (venv_argv(uv, py, venv), install_argv(uv, venv / "bin" / "python", req_file)):
        r = uv_run(argv)
        if r.returncode != 0:
            tail = (r.stdout + r.stderr)[-300:]
            raise OrchError("update.unverified", f"uv could not install the new version: {tail}")
    r = subprocess.run(
        [str(venv / "bin" / "orch"), "--version"], capture_output=True, env=childenv.sanitize(os.environ), timeout=120
    )
    if r.returncode != 0:
        raise OrchError("update.unverified", "the new orch does not start")
    (tool / ".complete").write_text("")


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

        if not quarantine:  # also reapply trust for an installed Publish that was stopped before the update
            from orch.install.cli import _HUB_ID, default_ctx
            from orch.install.parts import InstallError
            from orch.install.state import load
            from orch.install.trust import apply

            ctx = default_ctx(self.e.env, self.e.out, self.e.err)
            try:
                apply(ctx, self.e.env, fetch_fingerprint=False)
            except (InstallError, OSError) as exc:
                try:
                    st = load(ctx.layout)
                except (InstallError, OSError, ValueError):
                    raise OrchError("host.unavailable", str(exc)) from None
                hub_id = (st.get("edge_trust") or {}).get("hub_id")
                if not isinstance(hub_id, str) or not _HUB_ID.fullmatch(hub_id):
                    raise OrchError("host.unavailable", str(exc)) from None
                reason = "".join(c if c.isprintable() else " " for c in str(exc))
                reason = " ".join(reason.split())[:200].rstrip(".")
                ctx.say(f"Publish could not confirm its trust in hub {hub_id}: {reason}. Run the edge setup again.")
                if (st["parts"].get("publish") or {}).get("pinned_hub_id") != hub_id:
                    raise OrchError("host.unavailable", str(exc)) from None

    def _host_health(self) -> list[str]:
        """``host.health``: the host opens every workspace root with the real rule (a read: works in quarantine)."""
        from orch.host.control_client import ControlClient

        try:
            with ControlClient(self.e.state / "run" / "control" / "host.sock", timeout=20.0) as c:
                frame = c.call("host.health")
        except Exception as exc:  # noqa: BLE001
            return [f"the host could not be asked for its health ({exc})"]
        if not frame.get("ok"):
            return ["the host refused the health question"]
        return [str(p) for p in (frame.get("result") or {}).get("data", frame.get("result") or {}).get("problems", [])]

    def health(self, *, quarantine: bool) -> list[str]:
        out: list[str] = []
        if not self.e.answers(self.e.state / "run" / "control" / "host.sock"):
            out.append("the host does not answer")
        if not out:
            out += self._host_health()
        if quarantine:  # before the commit only the host runs; the other parts are checked after it (warnings)
            return out
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

"""The hub's service helper, made by the installer: ``<hub data>/helper.json`` (which units the hub's Services page may
start and stop) plus the user unit ``orch-<tag>-hub-helper.service`` that runs ``orch-hub helper --state <data>``.

Only systemd user units are handled: launchd and pidfile parts keep "not set up" (the helper's own backends refuse
them). A system unit on a hand-built server needs a sudoers drop-in that only the server owner installs; orch never
writes sudoers and never calls sudo (docs/design/hub-service-helper.md, "System units on a hand-built server").
Nothing secret goes on the helper's command line."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .state import load, save

if TYPE_CHECKING:
    from .cli import Ctx

__all__ = ["HELPER_PART", "helper_config", "remove_helper", "sync_helper"]

HELPER_PART = "hub-helper"  # the runner's part name; the unit is orch-<tag>-hub-helper.service
CONFIG_FILE = "helper.json"
SOCK_FILE = "helper.sock"
LABELS = {"hub": "Hub", "relay": "Relay", "publish": "Publish"}
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
READY_WAIT_S = 5.0


def _host_id() -> str:
    raw = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname()).strip("-._")[:40]
    return raw if HOST_RE.fullmatch(raw) else "this-server"


def helper_config(st: dict[str, Any], host_id: str | None = None) -> dict[str, Any] | None:
    """The helper.json body for the parts systemd (user) runs here; ``None`` without the hub or such a part."""
    parts = st.get("parts") or {}
    if not isinstance(parts.get("hub"), dict):
        return None  # no hub, nothing asks the helper
    out: dict[str, Any] = {}
    for name in ("hub", "relay", "publish"):
        rec = parts.get(name)
        if not isinstance(rec, dict) or rec.get("runner") != "systemd" or not isinstance(rec.get("service"), str):
            continue
        out[name] = {"label": LABELS[name], "kind": "systemd-user", "unit": rec["service"]}
    if not out:
        return None
    return {"v": 1, "host_id": host_id or _host_id(), "parts": out}


def _body(cfg: dict[str, Any]) -> bytes:
    return (json.dumps(cfg, indent=2, sort_keys=True) + "\n").encode()


def _read_plain(path: Path) -> bytes | None:
    """The file's bytes, or ``None`` when it is missing; a symlink or anything but a plain file raises OSError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError("not a plain file")
        return f.read()


def _write_config(path: Path, body: bytes) -> bool:
    """Atomic, 0600, no symlink followed. ``True`` when the file changed. Raises OSError for a symlink/odd file."""
    if _read_plain(path) == body:
        return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return True


def wait_for_socket(sock: Path, wait: float = READY_WAIT_S) -> bool:
    """Whether the helper's socket file shows up within ``wait`` seconds (the default ``Ctx.helper_ready``)."""
    end = time.monotonic() + wait
    while True:
        if os.path.lexists(sock):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)


def sync_helper(ctx: Ctx) -> None:
    """Make helper.json and the helper unit match the installed parts. Idempotent: the file is rewritten only when its
    bytes change, and the helper restarts (it reads helper.json once) only then or when it is not running."""
    lay = ctx.layout
    st = load(lay)
    parts = st.get("parts") or {}
    cfg = helper_config(st)
    if cfg is None:
        if st.get("helper"):
            remove_helper(ctx, st)
            save(lay, st)
        if "hub" in parts:
            ctx.say("  service buttons: off (the Services page can only start and stop parts that systemd runs here)")
        return
    data = lay.data("hub")
    try:
        changed = _write_config(data / CONFIG_FILE, _body(cfg))
    except OSError as e:
        ctx.say(f"  service buttons: left off ({data / CONFIG_FILE} is not a plain file orch can replace: {e})")
        return
    rec = st.get("helper") if isinstance(st.get("helper"), dict) else {}
    argv = [parts["hub"]["binary"], "helper", "--state", str(data)]  # no secret, no option but --state
    if changed or rec.get("argv") != argv or not ctx.runner.info(HELPER_PART).get("running"):
        started = ctx.runner.start(HELPER_PART, argv, {}, lay.logs / f"{HELPER_PART}.log")
        st["helper"] = {
            "argv": argv,
            "service": started["service"],
            "runner": ctx.runner.kind,
            "state": str(data),
            "started": int(time.time()),
        }
        save(lay, st)
    if ctx.helper_ready(data / SOCK_FILE):
        ctx.say("  service buttons: on (the Services page can start and stop the parts)")
    else:
        ctx.say(
            "  service buttons: the helper was started but does not answer yet (the Services page may say so briefly)"
        )
    for name in ("relay", "publish"):
        if name in parts and name not in cfg["parts"]:
            ctx.say(f"  service buttons: {name} is not on the Services page (systemd does not run it)")


def remove_helper(ctx: Ctx, st: dict[str, Any] | None = None) -> None:
    """Stop the helper this install made and delete its helper.json; a helper set up by hand (no record) is left alone.
    With ``st`` the caller saves the state."""
    own = st is None
    st = load(ctx.layout) if st is None else st
    if not st.get("helper"):
        return
    ctx.runner.stop(HELPER_PART)
    (ctx.layout.data("hub") / CONFIG_FILE).unlink(missing_ok=True)  # unlink never follows a symlink
    st.pop("helper", None)
    if own:
        save(ctx.layout, st)

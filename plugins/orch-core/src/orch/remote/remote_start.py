"""Starting a dashboard with the remote bridge: the preflight, the workspace channel key, the host and the start-up
listing. Imported only when the remote flag is given, so a plain local dashboard never loads any of the bridge.

Core holds no relay-specific code. The relay is reached through the relay addon: the installed addon that pairs
phones (its manifest has `remote_humans`) and names exactly one command-line tool in its settings (`binaries`
`setting:<key>`). That tool answers three commands for the host: `space show --json` (this workspace's space and
whether this device owns it), `bridge-key --workspace <space>` (the workspace channel key) and `bridge-host
--workspace <space>` (the transport child, orch.remote.transport).

The preflight runs before the dashboard binds its port and names everything that is missing at once, each with its
fix. The workspace channel key K_ws comes from `bridge-key` with its stdout piped into this process (the command
refuses a terminal); it is held in memory only and never written, printed or logged (bridge protocol §2.6).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from orch.errors import OrchError

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_NOT_APPROVED = {"pending", "unauthenticated", "unauthorized", "revoked"}
_AUDIT_LIMIT = 8 << 20
BASELINE = "start-baseline.json"
DOCS = "see docs/remote.md"


class RemoteNotReady(OrchError):
    """The remote bridge cannot start here; the message lists what is missing and the hint how to fix each."""
    exit_code = 8


@dataclass(frozen=True)
class Missing:
    what: str
    fix: str


def relay_addons() -> list:
    """The installed addons that can carry the bridge: they pair phones and name one command-line tool."""
    from orch.addons import discovery
    return [f for f in discovery.discover() if f.manifest is not None and f.error is None
            and f.manifest.remote_humans and len(f.manifest.setting_binaries()) == 1]


def _space_show(tool: str, cwd) -> tuple[dict | None, str]:
    """(the space, "") or (None, the tool's error code)."""
    try:
        r = subprocess.run([tool, "space", "show", "--json"], cwd=str(cwd), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None, "run_failed"
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if r.returncode != 0:
        code = data.get("error")
        return None, code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else "failed"
    return data, ""


def _config_dir_problem(ws) -> bool:
    from orch.dashboard.launch import config_dir
    from orch.remote.bridge_host import files
    try:
        base = config_dir()
        if base.resolve().is_relative_to(Path(ws.root).resolve()):
            return True
        files.ensure_dir(files.ensure_dir(files.ensure_dir(base) / "permits") / "bridge")
        return False
    except Exception:  # noqa: BLE001 - any doubt: not usable
        return True


def preflight(ws) -> dict:
    """{"tool", "space", "server"} when everything the bridge needs is here; RemoteNotReady listing every missing
    piece otherwise. Reads no addon code and no secret."""
    from orch.addons.userfiles import trust_state, workspace_addons
    from orch.remote import bridge_host
    missing: list[Missing] = []
    if not bridge_host.available():
        missing.append(Missing("the Python package cryptography", bridge_host.CRYPTO_HINT))
    if _config_dir_problem(ws):
        missing.append(Missing("a usable orch config directory outside the workspace (for the guarded bridge records)",
                               "make sure the orch config directory (ORCH_STATE_DIR, or ~/.config/orch) is a plain, "
                               "writable directory outside every workspace"))
    saved = workspace_addons(ws.root)
    relays = relay_addons()
    enabled = [f for f in relays if saved.get(f.name, {}).get("enabled")]
    addon = tool = None
    if len(enabled) == 1:
        addon = enabled[0]
    elif enabled:
        missing.append(Missing("exactly one relay addon enabled in this workspace",
                               "disable all but one of " + ", ".join(sorted(f.name for f in enabled))))
    else:
        names = ", ".join(sorted(f.name for f in relays))
        missing.append(Missing("the relay addon enabled in this workspace",
                               f"enable {names} in Mission Control → Workspace & addons" if names else
                               f"install the relay addon and enable it in Workspace & addons ({DOCS})"))
    if addon is not None:
        try:
            trusted = trust_state(addon) == "trusted"
        except Exception:  # noqa: BLE001
            trusted = False
        if not trusted:
            missing.append(Missing(f"the {addon.name} addon trusted", "trust it in Workspace & addons"))
        key = addon.manifest.setting_binaries()[0]
        value = saved.get(addon.name, {}).get("config", {}).get(key)
        tool = value if isinstance(value, str) else ""
        if not (os.path.isabs(tool) and os.path.isfile(tool) and os.access(tool, os.X_OK)):
            missing.append(Missing(f"the {addon.name} addon's command-line tool (its {key} setting)",
                                   f"set {key} in Workspace & addons → {addon.name} to the tool's absolute path"))
            tool = None
    space = server = None
    if tool is None:
        missing.append(Missing("this device approved on the relay, and a space there for this workspace",
                               "not checked: needs the relay addon's command-line tool first"))
    else:
        data, code = _space_show(tool, ws.root)
        if code in _NOT_APPROVED:
            missing.append(Missing("this device approved on the relay", "approve this device in your browser"))
        elif code == "no_space" or (data is not None and not _HEX32.fullmatch(str(data.get("space_id") or ""))):
            missing.append(Missing("a space on the relay for this workspace",
                                   f"create one with `{os.path.basename(tool)} space create --label NAME` here"))
        elif code:
            missing.append(Missing("a reachable relay server", f"the tool answered `{code}`; check the network "
                                                               f"and run `{os.path.basename(tool)} space show` here"))
        elif data.get("owner") is not True:
            missing.append(Missing("this device as the owner of the workspace's space",
                                   "only the space's owner device can host it: ask to take it over from the other "
                                   "device first"))
        else:
            space, server = data["space_id"], str(data.get("server") or "")
            if not server.startswith("https://") or not urlsplit(server).hostname:
                missing.append(Missing("the relay server address", "the tool reports no https server address"))
    if missing:
        raise RemoteNotReady(f"the remote bridge cannot start: {len(missing)} thing(s) missing",
                             hint="\n".join(f"- {m.what}: {m.fix}" for m in missing))
    return {"tool": tool, "space": space, "server": server}


_KEY_REFUSALS = {2: "the space does not exist on the relay", 3: "this device is not approved (or was revoked)",
                 6: "the tool refused to hand out the key (this device must own the space)"}


def read_key(tool: str, space: str, cwd) -> bytes:
    """K_ws from `<tool> bridge-key`, stdout piped (never a terminal). In memory only: its output is never shown,
    logged or written, not even on failure."""
    try:
        r = subprocess.run([tool, "bridge-key", "--workspace", space], cwd=str(cwd), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RemoteNotReady("the workspace key could not be read", hint=DOCS) from e
    out, code = r.stdout.strip(), r.returncode
    del r
    if code != 0 or not _HEX64.fullmatch(out.decode("ascii", "replace")):
        raise RemoteNotReady("the workspace key could not be read",
                             hint=_KEY_REFUSALS.get(code, f"the tool exited with code {code}"))
    return bytes.fromhex(out.decode("ascii"))


def _stamp(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _line(entry: dict) -> str:
    from orch.textsafe import visible
    parts = [_stamp(entry.get("at")), visible(str(entry.get("event", "?")))[:40]]
    if entry.get("device"):
        parts.append("device " + visible(str(entry["device"]))[:12])
    for k in ("scope", "label", "why"):
        if entry.get(k):
            parts.append(f"{k} {visible(str(entry[k]))[:80]}")
    if "ok" in entry:
        parts.append("ok" if entry["ok"] is True else "refused")
    return "  " + "  ".join(parts)


def startup_report(host, out) -> None:
    """What every remote start shows (bridge protocol §2.7, D8): the paired devices and their scopes, every registry
    change since the last start from the audit log, and any damage the owner must know about (Host.health())."""
    import hashlib
    from orch.remote.bridge_host import files
    from orch.textsafe import visible
    health = host.health()
    if not health["registry_readable"]:
        out("remote: the device registry cannot be read, so every request is dropped until it is repaired")
    if health["damaged_records"]:
        out(f"remote: {len(health['damaged_records'])} request record(s) are damaged; they are kept and never run")
    try:
        devices = host.registry.devices()
    except files.Damaged:
        devices = {}
    live = [d for d in devices.values() if not d.revoked]
    out(f"remote: {len(live)} paired device(s)" + ("" if live else " (pair one in Workspace & addons → Remote)"))
    for d in sorted(live, key=lambda d: d.paired_at):
        out(f"  {visible(d.label)[:40] or '(no label)'}  scope {d.scope}  device {d.id[:12]}  paired {_stamp(d.paired_at)}")
    root = Path(host.root)
    try:
        raw = files.read(root / "audit.jsonl", _AUDIT_LIMIT) or b""
    except files.Damaged:
        out("remote: the audit log cannot be read: registry changes since the last start cannot be shown")
        return
    lines = raw.splitlines(keepends=True)
    try:
        base = json.loads((files.read(root / BASELINE, 4096) or b"{}").decode("utf-8"))
        seen, digest = int(base.get("lines", 0)), str(base.get("sha256", ""))
    except (files.Damaged, ValueError, TypeError, AttributeError, UnicodeDecodeError):
        seen, digest = 0, ""
    if seen > len(lines) or (seen and hashlib.sha256(b"".join(lines[:seen])).hexdigest() != digest):
        out("remote: the audit log changed since the last start (earlier entries were rewritten or removed); "
            "all of it follows")
        seen = 0
    new = lines[seen:]
    out(f"remote: {len(new)} registry change(s) since the last start" if new else
        "remote: no registry changes since the last start")
    for raw_line in new[-50:]:
        try:
            entry = json.loads(raw_line)
        except ValueError:
            entry = None
        out(_line(entry) if isinstance(entry, dict) else "  (an unreadable audit line)")
    if len(new) > 50:
        out(f"  … and {len(new) - 50} earlier one(s)")
    try:
        files.replace(root / BASELINE, json.dumps({"lines": len(lines), "sha256": hashlib.sha256(raw).hexdigest()})
                      .encode("ascii"))
    except OSError:
        out("remote: could not record this start's audit baseline")


@dataclass
class Remote:
    """What the dashboard needs to run the bridge: the host, and the transport child's command line."""
    host: object
    child_argv: list[str]
    ws: object

    def loop(self, app):
        from orch.dashboard.bridge_loop import HostLoop
        return HostLoop(app, self.host, self.child_argv, self.ws)


def prepare(ws, *, take_over: bool = False, out=print) -> Remote:
    """Preflight, K_ws, the host and the start-up listing; RemoteNotReady before anything binds."""
    info = preflight(ws)
    from orch.dashboard.app import dashboard_routes
    from orch.dashboard.bridge_loop import route_hook
    from orch.dashboard.launch import config_dir
    from orch.remote import store
    from orch.remote.bridge_host import files
    from orch.remote.bridge_host.host_check import Host, load_host_key
    root = files.bridge_dir(config_dir(), info["space"])
    try:
        host_key = load_host_key(root)
    except (files.Damaged, OSError) as e:
        raise RemoteNotReady("the bridge's host key cannot be read; it is never replaced silently, because that "
                             "would unpin every paired device", hint=DOCS + ", \"Damaged records\"") from e
    k_ws = read_key(info["tool"], info["space"], ws.root)

    def phone_key(phone_id):
        p = store.find(ws.root, phone_id)
        return p.key if p is not None and p.revoked_at is None else None

    server = urlsplit(info["server"])
    try:
        host = Host(workspace=bytes.fromhex(info["space"]), k_ws=k_ws, host_key=host_key, root=root,
                    clock=lambda: time.time_ns() // 1_000_000, route=route_hook(dashboard_routes()),
                    phone_key=phone_key, rp_id=server.hostname, origin=f"{server.scheme}://{server.netloc}")
    except (files.Damaged, OSError) as e:
        raise RemoteNotReady("the bridge's records cannot be opened", hint=DOCS + ", \"Damaged records\"") from e
    del k_ws
    startup_report(host, out)
    argv = [info["tool"], "bridge-host", "--workspace", info["space"]] + (["--take-over"] if take_over else [])
    return Remote(host, argv, ws)

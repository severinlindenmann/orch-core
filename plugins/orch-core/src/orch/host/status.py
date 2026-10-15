"""``orch host status`` (alias ``orch host doctor``): one look at host mode for phase 1 (H7).

What it reports, each part read on its own so one failure does not hide the rest:

* **service**: the per-user unit (LaunchAgent or systemd user unit), whether it is current, loaded and running; on a
  platform without one, ``null`` and direct mode;
* **host**: whether a host answers on the control socket, and its workspaces (``host.list``);
* **workspaces**: every entry of this person's registry, with the host's state for it, whether its socket answers,
  the person this UID is mapped to, and the click-to-sign flag (``orch host convenience``, off by default);
* **here**: the workspace the CLI finds from the current directory and the mode the CLI uses for it (``host`` when it is
  registered, else ``direct``), exactly as :func:`orch.hostclient.locate` decides;
* **relay** (R5, orch-v2-host §11A.4), per workspace with a relay directory: the link state, what keeps the relay
  worker from starting (the stop marker, the link, the R6 gate) and the outbox entries queued for publication; a
  linked workspace whose worker cannot start for another reason than the operator's stop is a problem;
* **problems**: what keeps host mode from working, each with the command that fixes it.

Nothing here writes, starts or repairs anything.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["render", "report"]

#: the listener refuses a socket path longer than this (UTF-8 bytes, §2)
SOCKET_PATH_MAX = 100


def _service(state: Path, env: Mapping[str, str], run: Path) -> dict[str, Any] | None:
    from . import cli
    from . import enable as svc
    from .release import releases_dir

    try:
        unit = svc.plan(env, state, run, releases_dir(dict(env)) / "current")
    except svc.Unsupported:
        return None
    try:
        return cli.SERVICE_MANAGER(unit).status()
    except OSError as exc:
        return {"kind": unit.kind, "unit": str(unit.unit_path), "error": str(exc)[:200]}


def _host(control: Path) -> dict[str, Any]:
    from .control_client import ControlClient

    try:
        with ControlClient(control, timeout=5) as client:
            res = client.call("host.list")
    except Exception as exc:  # noqa: BLE001 - no socket, refused, wrong owner: all "no host answers"
        return {"answers": False, "control": str(control), "error": str(exc)[:200], "workspaces": []}
    rows = (res.get("result") or {}).get("data", {}).get("workspaces", []) if res.get("ok") else []
    return {"answers": bool(res.get("ok")), "control": str(control), "workspaces": rows}


def _socket_answers(path: Path, host_uid: int, workspace_id: str) -> bool:
    from .client import Connection, Pin

    try:
        Connection(Pin(host_uid, workspace_id, path), client="orch host status", timeout=5).close()
        return True
    except Exception:  # noqa: BLE001 - any failure: it does not answer
        return False


def _here(env: Mapping[str, str], state: Path) -> dict[str, Any] | None:
    import time

    from orch import hostclient
    from orch.ops.caps import Files
    from orch.ops.errors import OrchError
    from orch.ops.runtime import Workspace

    ws = Workspace(env, time.time, files=Files.direct())
    try:
        root = ws.root
    except OrchError:
        root = None
    if root is None:
        return None
    try:
        target = hostclient.locate(env, ws, state=state)
    except OrchError as exc:
        return {"root": str(root), "mode": "refused", "error": exc.code, "message": exc.message}
    if target is None:
        return {"root": str(root), "mode": "direct"}
    return {"root": str(root), "mode": "host", "workspace_id": target.workspace_id, "socket": str(target.socket)}


def _relay(state: Path, wid: str, host_uid: int) -> dict[str, Any] | None:
    """The relay part of a row (R5): ``None`` for a workspace without a relay directory."""
    from . import relaylink

    if not os.path.lexists(relaylink.relay_dir(state, wid)):
        return None
    try:
        linked = os.path.lexists(relaylink.link_path(state, wid))
        blocker = relaylink.start_blocker(state, wid, host_uid)
    except Exception as exc:  # noqa: BLE001 - reported, never the reason the status fails
        return {"error": type(exc).__name__}
    try:
        outbox = sum(1 for n in os.listdir(state / "hosts" / wid / "outbox") if len(n) == 25 and n.endswith(".json"))
    except FileNotFoundError:
        outbox = 0
    except OSError:
        outbox = None
    return {"linked": linked, "blocker": blocker, "outbox_pending": outbox}


def report(state: Path, env: Mapping[str, str], control: Path) -> dict[str, Any]:
    from orch import hostclient

    from . import convenience
    from .custody import read_json
    from .errors import HostError
    from .registry import registry_path

    uid = os.getuid()
    run = control.parent.parent
    problems: list[dict[str, str]] = []
    service = _service(state, env, run)
    host = _host(control)
    try:
        registry = read_json(registry_path(state), uid) or {"workspaces": []}
    except HostError as exc:
        registry = {"workspaces": []}
        problems.append(
            {"what": f"the registry is unreadable: {exc.message}", "fix": f"look at {registry_path(state)}"}
        )
    try:
        flags = convenience.read(state, uid).get("workspaces") or {}
    except HostError as exc:
        flags = {}
        problems.append(
            {"what": f"the click-to-sign flags are unreadable: {exc.message}", "fix": "orch host convenience show"}
        )
    served = {row.get("workspace_id"): row for row in host["workspaces"]}
    rows = []
    for entry in registry.get("workspaces") or []:
        wid = str(entry.get("workspace_id"))
        host_uid = entry.get("host_uid") if type(entry.get("host_uid")) is int else uid
        sock = hostclient.workspace_socket(env, state, host_uid, wid)
        live = served.get(wid)
        flag = flags.get(wid)
        row = {
            "workspace_id": wid,
            "prefix": entry.get("prefix"),
            "root": entry.get("root"),
            "state": live.get("state") if live else "not served",
            "socket": str(sock),
            "socket_answers": _socket_answers(sock, host_uid, wid) if host["answers"] else False,
            "person": ((entry.get("peers") or {}).get(str(uid)) or {}).get("person"),
            "click_to_sign": bool(isinstance(flag, dict) and flag.get("enabled") is True),
        }
        relay = _relay(state, wid, host_uid)
        if relay is not None:
            row["relay"] = relay
            why = relay.get("blocker") or relay.get("error")
            if relay.get("linked") and why and why != "stopped":
                problems.append(
                    {
                        "what": f"{entry.get('prefix')}: relay-linked, but its relay worker cannot start: {why}",
                        "fix": "nothing to do until R6 lands (orch-v2-host §11A.1)"
                        if str(why).startswith("r6_not_ready")
                        else "orch host relay start --workspace " + wid,
                    }
                )
        rows.append(row)
        if len(str(sock).encode()) > SOCKET_PATH_MAX:
            problems.append(
                {
                    "what": f"{row['prefix']}: the socket path is longer than {SOCKET_PATH_MAX} bytes",
                    "fix": "a shorter ORCH_STATE_DIR",
                }
            )
        elif host["answers"] and not row["socket_answers"]:
            problems.append(
                {"what": f"{row['prefix']}: registered, but its socket does not answer", "fix": "orch host restart"}
            )
    if rows and not host["answers"]:
        fix = "orch host enable" if service is not None else "orch host run --foreground"
        problems.append(
            {
                "what": "workspaces are registered but no host answers: their commands answer host.unavailable",
                "fix": fix,
            }
        )
    differs = (service or {}).get("differs") or []
    if {"state", "run"} & set(differs):
        serves = service.get("serves") or {}
        problems.append(
            {
                "what": f"the installed service serves another setup ({', '.join(differs)} differ: state "
                f"{serves.get('state')}, run {serves.get('run')}); workspaces of this state dir are not served by it",
                "fix": "decide which setup the service runs (orch host enable --help); nothing is changed for you",
            }
        )
    # an interpreter that differs alone is no problem: the service keeps the one it was enabled with (H7 review)
    here = _here(env, state)
    if here is not None and here.get("mode") == "refused":
        problems.append({"what": f"this directory: {here['message']}", "fix": "orch host status in the workspace root"})
    return {
        "ok": not problems,
        "state_dir": str(state),
        "service": service,
        "host": {k: v for k, v in host.items() if k != "workspaces"},
        "workspaces": rows,
        "here": here,
        "problems": problems,
    }


def _yes(value: Any) -> str:
    return "yes" if value is True else "no" if value is False else "unknown"


def render(rep: Mapping[str, Any]) -> str:
    """The report as text for a person: one line per fact, the problems last with their fixes."""
    lines = [f"state dir {rep['state_dir']}"]
    svc = rep["service"]
    if svc is None:
        lines.append("service: none on this platform (workspaces stay in direct mode)")
    elif "error" in svc:
        lines.append(f"service: {svc['kind']} {svc['unit']}: {svc['error']}")
    else:
        lines.append(
            f"service: {svc['kind']} {svc['unit']} installed={_yes(svc['unit_present'])} "
            f"current={_yes(svc['unit_current'])} loaded={_yes(svc['loaded'])} running={_yes(svc['running'])}"
        )
    host = rep["host"]
    lines.append(f"host: {'answers' if host['answers'] else 'does not answer'} on {host['control']}")
    if not rep["workspaces"]:
        lines.append("workspaces: none registered (orch host register <dir> turns host mode on for one)")
    for w in rep["workspaces"]:
        lines.append(
            f"workspace {w['prefix']} ({w['workspace_id']}) {w['state']} "
            f"socket={'answers' if w['socket_answers'] else 'silent'} person={w['person'] or 'none'} "
            f"click-to-sign={'on' if w['click_to_sign'] else 'off'}"
        )
        lines.append(f"  root {w['root']}")
        if "relay" in w:
            r = w["relay"]
            lines.append(
                f"  relay linked={_yes(r.get('linked'))} worker={'may start' if not r.get('blocker') else r['blocker']}"
                f" outbox={r.get('outbox_pending')}"
            )
    here = rep["here"]
    if here is None:
        lines.append("here: not in a workspace")
    elif here["mode"] == "host":
        lines.append(f"here: {here['root']}: host mode ({here['socket']})")
    elif here["mode"] == "direct":
        lines.append(f"here: {here['root']}: direct mode (not registered)")
    else:
        lines.append(f"here: {here['root']}: refused ({here['error']})")
    if svc and svc.get("differs"):
        lines.append(
            f"service: the installed unit differs in {', '.join(svc['differs'])} from what this orch would install"
        )
    for p in rep["problems"]:
        lines.append(f"problem: {p['what']} · fix: {p['fix']}")
    lines.append("ok" if rep["ok"] else f"{len(rep['problems'])} problem(s)")
    from orch.canon.text import clean

    # every value printed comes from files a crafted directory name can reach: no control character reaches the terminal
    return "\n".join(clean(line).replace("\n", "⟨U+000A⟩") for line in lines) + "\n"

"""``orch remote on|off|level|status|device|key``: the owner's remote access to this workspace (R2 #1590, spec D84-D93).

    orch remote on --level view|work|terminals --premises yes|no [--days N] [--agents-can-act-as-me]
                                       a person: turn remote access on (signed, with your backup key)
    orch remote level LEVEL [--days N] [--agents-can-act-as-me]
                                       a person: change the level or end date while it is on (signed, backup key)
    orch remote off                    a person: turn remote access off now (no signature needed: it only removes power)
    orch remote status [--json]        a person: on or off, the devices (with their six words) and waiting codes
    orch remote device add [--level L] [--days N]
                                       a person: a one-time code, then the device that used it (its six words), then
                                       your backup key adds it
    orch remote device list | remove DEVICE
    orch remote sessions [--json]      a person: the remote sessions open now (a number each, who, level, since when)
    orch remote end N                  a person: end session number N of that list (no signature: it only removes power)
    orch remote key rotate             a person: a new workspace remote key; every device must be added again

These sit next to ``orch remote enable|disable|reset`` (the HTTPS front of the dashboard, ``orch.cli.remote``),
which are unchanged. ``on``, ``level`` and ``device add`` are signed by the owner on the owner's own terminal with
the backup key (the remote owner proof); ``off``, ``device remove`` and ``key rotate`` only reduce power and need
no proof. Everything here is refused to agents (``ORCH_GRANT`` or ``ORCH_SESSION`` set: ``op.not_allowed``;
the host refuses them again).
There is no word to type: the question is one of two answers, and the review shows what will be signed."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from orch.canon.text import clean_line
from orch.cli.publish import PubError, _call, _sign, _target, _tty_ok

__all__ = ["VERBS", "main"]

VERBS = frozenset({"on", "off", "level", "status", "device", "key", "sessions", "end"})
_LEVELS = ("view", "work", "terminals")
_USAGE = (
    "usage: orch remote {on --level LEVEL --premises yes|no [--days N] [--agents-can-act-as-me] "
    "| level LEVEL [--days N] [--agents-can-act-as-me] | off | status "
    "| device add [--level LEVEL] [--days N] | device list | device remove DEVICE | key rotate | sessions "
    "| end N} [--json]"
)
#: how long ``device add`` waits for the device to use the code (the code lives 15 minutes), and how often it asks
WAIT_S = 15 * 60
POLL_S = 2.0
_DEVICE_ID = "rd_"


def _parser() -> argparse.ArgumentParser:
    class Quiet(argparse.ArgumentParser):
        def error(self, message: str) -> Any:  # type: ignore[override]
            raise PubError("invalid.input", f"{message}\n{_USAGE}", 2)

    p = Quiet(prog="orch remote", add_help=False)
    p.add_argument("cmd", choices=sorted(VERBS))
    p.add_argument("rest", nargs="*")
    p.add_argument("--level", choices=_LEVELS)
    p.add_argument("--premises", choices=("yes", "no"))
    p.add_argument("--days", type=int)
    p.add_argument("--agents-can-act-as-me", action="store_true", dest="agents_risk")
    p.add_argument("--json", action="store_true")
    return p


def _emit(out: TextIO, as_json: bool, data: Mapping[str, Any], text: str) -> None:
    out.write((json.dumps({"ok": True, "data": data}) if as_json else text) + "\n")


def _day(ms: Any) -> str:
    if type(ms) is not int:
        return "you turn it off"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000))


def _status_text(d: Mapping[str, Any]) -> str:
    if d.get("on"):
        lines = [f"Remote access is on ({d.get('level')}), until {_day(d.get('until_ms'))}"]
    else:
        why = {
            "owner": "you turned it off",
            "device": "a device of yours turned it off",
            "expired": "its end date passed",
            "hub_full": "the hub reads this workspace (full mode)",
        }.get(str(d.get("off_reason")))
        lines = ["Remote access is off" + (f" ({why})" if why else "")]
    lines.append(_devices_text(d))
    if d.get("codes"):
        lines.append(f"  {d['codes']} code(s) waiting to be used")
    return "\n".join(lines)


def _devices_text(d: Mapping[str, Any]) -> str:
    rows = []
    for dev in d.get("devices") or []:
        words = " ".join(str(w) for w in dev.get("words") or [])
        rows.append(
            f"  {clean_line(str(dev.get('device_id')))}  {clean_line(str(dev.get('name')))}  up to "
            f"{dev.get('level_cap')}  until {_day(dev.get('expires_ms'))}  words: {clean_line(words)}"
        )
    return "\n".join(rows) or "  no devices"


def _sessions_text(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for n, r in enumerate(rows, 1):
        who = clean_line(str(r.get("name"))) if r.get("name") else "(connecting)"
        since = f"  since {_day(r['since_ms'])}" if type(r.get("since_ms")) is int else ""
        lines.append(f"  {n}  {who}  {r.get('level') or ''}{since}")
    return "\n".join(lines) or "  no remote sessions open"


def _check_shape(a: argparse.Namespace) -> None:
    def usage(why: str) -> PubError:
        return PubError("invalid.input", f"{why}\n{_USAGE}", 2)

    if a.days is not None and not 1 <= a.days <= 366:
        raise usage("--days is a number of days from 1 to 366")
    rest = a.rest
    if a.cmd == "on":
        if rest or a.level is None or a.premises is None:
            raise usage("remote on takes --level and --premises")
    elif a.cmd == "level":
        if len(rest) != 1 or rest[0] not in _LEVELS or a.level or a.premises:
            raise usage("remote level takes one of view, work, terminals")
    elif a.cmd == "end":
        if len(rest) != 1 or not rest[0].isdigit() or int(rest[0]) < 1 or a.level or a.premises or a.days is not None:
            raise usage("remote end takes the number of a session from orch remote sessions")
    elif a.cmd in ("off", "status", "sessions"):
        if rest or a.level or a.premises or a.days is not None or a.agents_risk:
            raise usage(f"remote {a.cmd} takes no arguments")
    elif a.cmd == "device":
        ok = (
            (rest == ["add"] and a.premises is None)
            or (rest == ["list"] and a.level is None and a.days is None)
            or (len(rest) == 2 and rest[0] == "remove" and rest[1].startswith(_DEVICE_ID) and a.level is None)
        )
        if not ok:
            raise usage("remote device takes add, list or remove DEVICE")
    elif rest != ["rotate"] or a.level or a.premises or a.days is not None:
        raise usage("remote key takes rotate")
    if a.agents_risk and a.cmd not in ("on", "level"):
        raise usage(f"remote {a.cmd} does not take --agents-can-act-as-me")


def main(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    from orch.ops.errors import OrchError

    out = out if out is not None else sys.stdout
    as_json = "--json" in argv

    def fail(exc: PubError) -> int:
        if as_json:
            doc = {"ok": False, "code": exc.code, "message": exc.message, **({"hint": exc.hint} if exc.hint else {})}
            out.write(json.dumps(doc) + "\n")
        else:
            out.write(f"error {exc.code}: {exc.message}\n" + (f"hint: {exc.hint}\n" if exc.hint else ""))
        return exc.exit

    try:
        a = _parser().parse_intermixed_args(list(argv))
        if env.get("ORCH_GRANT") or env.get("ORCH_SESSION"):
            raise PubError(
                "op.not_allowed", "a person does this at their own terminal; an agent never changes remote access"
            )
        _check_shape(a)
        target = _target(env)
        if a.cmd == "status":
            d = _call(target, "remote.status", {}, env)
            _emit(out, as_json, d, _status_text(d))
            return 0
        if a.cmd == "off":
            d = _call(target, "remote.off", {}, env)
            text = "ok remote access is off" if d.get("changed") else "ok remote access was already off"
            _emit(out, as_json, d, text)
            return 0
        if a.cmd == "sessions":
            rows = _call(target, "remote.sessions", {}, env).get("sessions") or []
            _emit(out, as_json, {"sessions": rows}, _sessions_text(rows))
            return 0
        if a.cmd == "end":
            rows = _call(target, "remote.sessions", {}, env).get("sessions") or []
            n = int(a.rest[0])
            if n > len(rows):
                raise PubError("remote.no_stream", "there is no session with that number; see orch remote sessions", 3)
            _call(target, "remote.session_end", {"stream_id": rows[n - 1]["stream_id"]}, env)
            _emit(out, as_json, {"ended": n}, f"ok session {n} ended")
            return 0
        if a.cmd == "key":
            d = _call(target, "remote.key_rotate", {}, env)
            _emit(out, as_json, d, "ok remote key replaced; every device must be added again (orch remote device add)")
            return 0
        if a.cmd == "device" and a.rest[0] == "list":
            d = _call(target, "remote.status", {}, env)
            _emit(out, as_json, {"devices": d.get("devices") or []}, _devices_text(d))
            return 0
        if a.cmd == "device" and a.rest[0] == "remove":
            d = _call(target, "remote.device_remove", {"device_id": a.rest[1]}, env)
            _emit(out, as_json, d, f"ok device {clean_line(a.rest[1])} removed; its sessions ended")
            return 0
        if not _tty_ok():
            raise PubError("tty.required", "no terminal to sign on: run this in a terminal; nothing was signed", 3)
        if a.cmd == "device":
            return _device_add(target, env, a, out)
        return _on_or_level(target, env, a, out, as_json)
    except PubError as exc:
        return fail(exc)
    except OrchError as exc:
        return fail(PubError(exc.code, str(exc.message), getattr(exc, "wire_exit", 3), getattr(exc, "hint", None)))


def _on_or_level(target: Any, env: Mapping[str, str], a: argparse.Namespace, out: TextIO, as_json: bool) -> int:
    if a.cmd == "on":
        args: dict[str, Any] = {"level": a.level, "premises": a.premises}
    else:  # level: the premises answer stays what the owner said when remote access was turned on
        d = _call(target, "remote.status", {}, env)
        if not d.get("on") or d.get("premises") not in ("yes", "no"):
            raise PubError("invalid.input", "remote access is off: turn it on first (orch remote on)", 5)
        args = {"level": a.rest[0], "premises": d["premises"]}
    if a.days is not None:
        args["days"] = a.days
    if a.agents_risk:
        args["agents_risk"] = True
    out.write("Review it, then sign with your backup key.\n")
    _sign(target, "remote.set", args)
    out.write("signed: remote access is recorded in your workspace log\n")
    if not as_json:
        out.write("next: add a device with orch remote device add\n")
    return 0


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _device_add(target: Any, env: Mapping[str, str], a: argparse.Namespace, out: TextIO) -> int:
    """One code, shown once; wait for the device that used it; show its six words; the backup key adds it. Nothing is
    kept here: the host holds the code and the waiting device in memory (a restart drops both)."""
    d = _call(target, "remote.code", {}, env)
    out.write(
        "On the new device, open the orch app and enter this code (it works once, for 15 minutes):\n\n"
        f"  {clean_line(str(d['code']))}\n\n"
        "Waiting for the device...\n"
    )
    out.flush()
    stop = _now() + WAIT_S
    pending: dict[str, Any] | None = None
    while pending is None:
        rows = _call(target, "remote.pending", {}, env).get("pending") or []
        if rows:
            pending = rows[0]
            break
        if _now() >= stop or (type(d.get("expires_ms")) is int and _now() * 1000 >= d["expires_ms"]):
            raise PubError("remote.no_pending", "no device used the code in time; make a new code", 3)
        _sleep(POLL_S)
    words = " ".join(str(w) for w in pending.get("words") or [])
    out.write(
        f"Add device {clean_line(str(pending.get('name')))}, fingerprint {clean_line(words)}?\n"
        "Check that the same six words are on the device. Review it, then sign with your backup key.\n"
    )
    args = {"pending": pending["pending"], "level_cap": a.level or "work", "days": a.days or 30}
    _sign(target, "remote.device_confirm", args)
    out.write("signed: the device is added\n")
    return 0

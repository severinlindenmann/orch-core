"""``orch publish``: share files of a ticket with a link, on a server you choose (the Publish add-on, P5).

    orch publish setup   --server ADDRESS --share ADDRESS     a person: where files go (signed)
    orch publish register                                      a person: tell the server this workspace exists
    orch publish prepare REF FILE... [--once [N]] [--days D]   copy artifacts of a ticket aside; nothing leaves
    (D for prepare: 1, 3, 7 or 30 days; default 7)
    orch publish share   STAGED                                a person: sign, then send; the link shows once
    orch publish send    STAGED                                a person: send a copy that is already signed
    orch publish revoke  SHARE [--yes]                          a person: the link stops working at once (asks first)
    orch publish extend  SHARE --days D                         a person: the link works D more days from now (1..30)
    orch publish password SHARE                                 a person: a generated password for the link, shown once
    orch publish list    [REF] [--live]                        staged copies and shares (never a link)
    orch publish discard STAGED                                drop a staged copy

An agent (``ORCH_GRANT`` set) may ``prepare``, ``list`` and ``discard`` on its own ticket and nothing else: ``share``,
``send``, ``revoke``, ``extend``, ``password``, ``setup`` and ``register`` are refused here (``op.not_allowed``), and
the host refuses them again (column P only). Every share needs a person's signature over the exact files, server,
link kind and days.

The link is written to the person's own terminal (``/dev/tty``) and nowhere else: not stdout, not a pipe, not a log.
This command keeps no copy. Without a terminal ``share`` and ``send`` stop before anything is sent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from orch.ops.human import review_prompt
from orch.publishclient import approval

__all__ = ["main"]

_USAGE = (
    "usage: orch publish {setup --server ADDRESS --share ADDRESS | register | prepare REF FILE... [--once [N]] "
    "[--days D] | share STAGED | send STAGED | revoke SHARE | extend SHARE --days D | password SHARE | "
    "list [REF] [--live] | discard STAGED} [--json]"
)
_AGENT_OK = frozenset({"prepare", "list", "discard"})
_PERSON_ONLY_MESSAGE = "a person does this; an agent only prepares (orch publish prepare)"


class PubError(Exception):
    def __init__(self, code: str, message: str, exit_: int = 3, hint: str | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.exit, self.hint = code, message, exit_, hint


def _parser() -> argparse.ArgumentParser:
    class Quiet(argparse.ArgumentParser):
        def error(self, message: str) -> Any:  # type: ignore[override]
            raise PubError("invalid.input", f"{message}\n{_USAGE}", 2)

    p = Quiet(prog="orch publish", add_help=False)
    p.add_argument(
        "cmd",
        choices=("setup", "register", "prepare", "share", "send", "revoke", "extend", "password", "list", "discard"),
    )
    p.add_argument("rest", nargs="*")
    p.add_argument("--server")
    p.add_argument("--share", dest="share_addr")
    p.add_argument("--once", nargs="?", const=1, type=int, default=None)
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--live", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--workspace")
    return p


def _target(env: Mapping[str, str]) -> Any:
    from orch import hostclient
    from orch.ops.caps import Files
    from orch.ops.runtime import Workspace

    ws = Workspace(env, time.time, files=Files.direct())
    target = hostclient.locate(env, ws)
    if target is None:
        raise PubError(
            "host.required",
            "Publish needs the orch host; this workspace is not served by one",
            hint="run: orch host enable",
        )
    return target


def _call(target: Any, op: str, args: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    from orch import hostclient

    auth = {k: v for k, v in (("grant", env.get("ORCH_GRANT")), ("session", env.get("ORCH_SESSION"))) if v} or None
    doc = hostclient._request(target, op, args, auth=auth, idem=None, timeout=300.0)
    return doc.get("data") if isinstance(doc, dict) and isinstance(doc.get("data"), dict) else {}


def _tty_ok() -> bool:
    from orch.ops.caps import open_tty

    try:
        fd = open_tty(os.O_WRONLY)
    except OSError:
        return False
    try:
        return os.isatty(fd)
    finally:
        os.close(fd)


def _sign(target: Any, op: str, args: dict[str, Any], capture: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The person's signature of ``op`` on their own terminal (the same composite as every person command). ``capture``
    receives the prepared review (``human.prepare``'s answer): the events the person was shown and signed."""
    from orch import hostclient
    from orch.custody import CustodyError
    from orch.host import signer
    from orch.host.control_client import new_idem
    from orch.host.errors import HostError
    from orch.identity import device_ref
    from orch.ops import human

    entry = target.entry
    person = ((entry or {}).get("peers") or {}).get(str(os.getuid()), {}).get("person") if entry else None
    if target.role != "person" or entry is None or not person:
        raise PubError("human_only", "this UID is not mapped to a person in this workspace")
    wid = target.workspace_id
    backend = human.device_backend(target.state, wid, convenience=True)
    name = Path(str(entry.get("root") or wid)).name
    conn = hostclient._connect(target)
    idem = new_idem()
    link = [conn]
    try:
        device = device_ref(backend.public_key(human.KEY_ID))
        call = hostclient._durable(target, link, idem)
        if capture is not None:
            inner = call

            def call(op_: str, args_: dict[str, Any], **kw: Any) -> Any:  # noqa: F811 - the same call, recording the review
                got = inner(op_, args_, **kw)
                if op_ == "human.prepare":
                    capture.append(got["data"])
                return got

        return signer.run(
            call,
            op,
            args,
            backend=backend,
            key_id=human.KEY_ID,
            workspace_id=wid,
            person=person,
            device=device,
            workspace=lambda w: (name, str(entry.get("prefix"))) if w == wid else None,
            confirm=human.review_prompt,
            show_secret=human.show_secret,
            idem=idem,
        )
    except signer.SignerRefused as exc:
        text = "not confirmed: nothing was signed" if str(exc) == "not confirmed" else f"{exc}; nothing was signed"
        raise PubError("invalid.input", text[:400], 5) from None
    except HostError as exc:
        raise PubError(exc.code, exc.message, exc.exit) from None
    except CustodyError as exc:
        raise PubError("custody.no_key", str(exc)[:300], 3) from None
    finally:
        link[0].close()


def _admin_lines(info: Mapping[str, Any], server: str | None) -> list[str]:
    ws, owner = info.get("workspace"), info.get("owner")
    return [
        "The server does not know this workspace yet. Its admin must allow it once, on the server:",
        f"  orch-publish admin --data DATA_DIR owners add {owner}",
        f"  orch-publish admin --data DATA_DIR ws allow {ws} {owner}",
        f"  (a local test server instead: restart it with  --owner {owner} --workspace {ws}={owner})",
        "Then run: orch publish register",
    ]


def _allow_on_local_server(info: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """A full server on this computer (``orch install publish``) is the person's own: allow the workspace owner and the
    workspace on it, as its admin would. False when no publish server is installed here or the admin tool fails."""
    import subprocess

    from orch import childenv
    from orch.install.state import Layout, load

    ws, owner = info.get("workspace"), info.get("owner")
    lay = Layout(env)
    rec = (load(lay).get("parts") or {}).get("publish")
    if not rec or not ws or not owner:
        return False
    base = [str(rec["binary"]), "admin", "--data", str(lay.data("publish"))]
    child = childenv.sanitize(env)  # the admin tool is a program of the person's: no loader variable reaches it
    for tail in (["owners", "add", str(owner)], ["ws", "allow", str(ws), str(owner)]):
        try:
            if subprocess.run([*base, *tail], capture_output=True, timeout=30, env=child).returncode != 0:
                return False
        except (OSError, subprocess.SubprocessError):
            return False
    return True


def _register(target: Any, env: Mapping[str, str], out: TextIO, _again: bool = True) -> int:
    """Tell the server this workspace exists. A server that does not know it yet is said plainly, with what its admin
    runs; that is not a failure of the command's other work, but it is exit 3."""
    from orch.ops.errors import OrchError

    try:
        got = _call(target, "publish.register", {}, env)
    except OrchError as exc:
        if exc.code == "publish.server_refused" and "knows this workspace" in str(exc.message):
            info = _call(target, "publish.list", {}, env)
            if _again and _allow_on_local_server(info, env):
                out.write("allowed this workspace on the publish server of this computer\n")
                return _register(target, env, out, _again=False)
            out.write("\n".join(_admin_lines(info, None)) + "\n")
            return 3
        if exc.code == "hub.not_joined":
            out.write(
                "Publishing goes through a hub, and this workspace has none connected. "
                "Join a hub (orch hub join), then run: orch publish register\n"
            )
            return 3
        if exc.code == "runner.disabled":
            out.write(
                "Publish is off in this workspace. Turn it on in Settings, Add-ons (or: orch addon grant publish), "
                "then run: orch publish register\n"
            )
            return 3
        raise
    out.write(f"ok publish register {got.get('result', 'registered')}\n")
    return 0


def _link_text(link: str, expires: str) -> str:
    return (
        "\n  The link (shown once, on this terminal only):\n\n    "
        + link
        + f"\n\n  It works until {expires}. You will not see this link again; orch keeps no copy.\n"
        "  Copy it now. If you lose it: orch publish revoke the share, then share again.\n\n"
    )


def _send(target: Any, staged: str, env: Mapping[str, str], out: TextIO, as_json: bool) -> int:
    from orch.ops import human

    if not _tty_ok():
        raise PubError("invalid.input", "no terminal to show the link on: run this in a terminal; nothing was sent", 5)
    got = _call(target, "publish.send", {"staged_id": staged}, env)
    link = got.pop("link", None)
    if not isinstance(link, str):
        raise PubError("internal", "the host sent no link", 1)
    try:
        human.show_secret(_link_text(link, str(got.get("expires_at"))))
    except Exception:  # noqa: BLE001 - the share exists; say so, never print the link anywhere else
        out.write(
            "the files are shared but the link could not be shown; revoke it: orch publish revoke "
            + str(got.get("share_id"))
            + "\n"
        )
        return 1
    if as_json:
        out.write(json.dumps({"ok": True, "data": {**got, "link_shown": True}}) + "\n")
    else:
        out.write(
            f"ok publish shared {got.get('share_id')} ({got.get('kind')}) for {got.get('ticket')}: "
            "the link is on your terminal only\n"
        )
    return 0


def _password(target: Any, share_id: str, env: Mapping[str, str], out: TextIO, as_json: bool) -> int:
    """The generated password goes to the person's terminal only, like a link: not stdout, not a pipe, not a log."""
    from orch.ops import human

    if not _tty_ok():
        raise PubError(
            "invalid.input", "no terminal to show the password on: run this in a terminal; nothing was set", 5
        )
    got = _call(target, "publish.password", {"share_id": share_id}, env)
    password = got.pop("password", None)
    if not isinstance(password, str):
        raise PubError("internal", "the host sent no password", 1)
    try:
        human.show_secret(
            f"\n  The password (shown once, on this terminal only):\n\n    {password}\n\n"
            "  Give it to the people you share the link with, by another way than the link.\n"
            "  It is not kept anywhere. To change it: orch publish password again (a new one replaces this).\n\n"
        )
    except Exception:  # noqa: BLE001 - the password is set; say so, never print it anywhere else
        out.write(f"the password is set but could not be shown; set a new one: orch publish password {share_id}\n")
        return 1
    if as_json:
        out.write(json.dumps({"ok": True, "data": {**got, "password_shown": True}}) + "\n")
    else:
        out.write(f"ok publish password {got.get('share_id')}: the password is on your terminal only\n")
    return 0


def _local(iso: Any) -> Any:
    """A server time in this computer's time with the zone (``--json`` keeps the UTC text)."""
    from orch.host.signcontent import local_time

    return local_time(iso, 48) or iso


def _list_text(data: Mapping[str, Any]) -> str:
    lines = []
    if not data.get("configured"):
        lines.append("no server set up yet: orch publish setup --server ADDRESS --share ADDRESS")
    for s in data.get("staged") or []:
        who = "an agent" if s.get("origin") == "agent" else "you"
        lines.append(
            f"staged {s['staged_id']} {s['ticket']} {s['kind']} {len(s['files'])} file(s) by {who}: "
            f"orch publish share {s['staged_id']}"
        )
    for r in data.get("shares") or []:
        views = (
            f" views={r.get('views')}/{r.get('view_limit')}" if r.get("kind") == "once" else f" views={r.get('views')}"
        )
        end = _local(r.get("expires_at"))
        lines.append(f"share {r['id']} {r['ticket']} {r['kind']} {r['state']} until {end}{views}")
    return "\n".join(lines) if lines else "nothing staged or shared"


def main(
    argv: list[str],
    *,
    env: Mapping[str, str],
    out: TextIO | None = None,
    err: TextIO | None = None,
    now: Any = time.time,
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
        if os.name == "nt":
            raise PubError("unsupported.platform", "orch publish needs POSIX filesystem semantics")
        a = _parser().parse_intermixed_args(argv)
        if env.get("ORCH_GRANT") and a.cmd not in _AGENT_OK:
            raise PubError("op.not_allowed", _PERSON_ONLY_MESSAGE)
        _check_shape(a)  # everything that needs no host fails here, before a socket or a key is touched
        target = _target(env)
        if a.cmd == "setup":
            return _setup(a, target, env, out)
        if a.cmd == "register":
            return _register(target, env, out)
        if a.cmd == "prepare":
            ref, files = a.rest[0], a.rest[1:]
            args: dict[str, Any] = {"ref": ref, "artifacts": files, "days": 7 if a.days is None else a.days}
            if a.once is not None:
                args.update(kind="once", views=a.once)
            data = _call(target, "publish.prepare", args, env)
            if as_json:
                out.write(json.dumps({"ok": True, "data": data}) + "\n")
                return 0
            kind = "secret link" if data["kind"] == "secret" else f"show-once link ({data['views']} view(s))"
            out.write(
                f"ok publish staged {data['staged_id']} ({len(data['files'])} file(s), {kind}, {data['days']} "
                f"days) for {data['ticket']}\n"
            )
            out.write(
                "nothing has left this computer. next: a person signs and sends it: "
                f"orch publish share {data['staged_id']}\n"
            )
            return 0
        if a.cmd in ("share", "send", "discard"):
            staged = a.rest[0]
            if a.cmd == "discard":
                _call(target, "publish.discard", {"staged_id": staged}, env)
                out.write(f"ok publish discarded {staged}\n")
                return 0
            if a.cmd == "share":
                if not _tty_ok():
                    raise PubError(
                        "tty.required",
                        "no terminal to sign on and show the link: run this in a terminal; nothing was signed",
                        3,
                    )
                _sign(target, "publish.share", {"staged_id": staged})
                try:
                    return _send(target, staged, env, out, as_json)
                except (PubError, OrchError):
                    out.write(f"your signature stays good for an hour: orch publish send {staged}\n")
                    raise
            return _send(target, staged, env, out, as_json)
        if a.cmd == "extend":
            got = _call(target, "publish.extend", {"share_id": a.rest[0], "days": a.days}, env)
            out.write(
                f"ok publish extended {got.get('share_id')}: the link works until {_local(got.get('expires_at'))}\n"
            )
            return 0
        if a.cmd == "password":
            return _password(target, a.rest[0], env, out, as_json)
        if a.cmd == "revoke":
            if not a.yes and not review_prompt(
                f"Revoke share {a.rest[0]}? Its link stops working at once.", verb="continue"
            ):
                raise PubError("sign.cancelled", "not confirmed: nothing was revoked", 3)
            got = _call(target, "publish.revoke", {"share_id": a.rest[0]}, env)
            out.write(f"ok publish revoked {got.get('share_id')}: the link no longer works\n")
            return 0
        data = _call(
            target,
            "publish.list",
            {**({"ref": a.rest[0]} if a.rest else {}), **({"live": True} if a.live else {})},
            env,
        )
        out.write((json.dumps({"ok": True, "data": data}) if as_json else _list_text(data)) + "\n")
        return 0
    except PubError as exc:
        return fail(exc)
    except OrchError as exc:
        return fail(PubError(exc.code, str(exc.message), getattr(exc, "wire_exit", 3), getattr(exc, "hint", None)))


def _check_shape(a: argparse.Namespace) -> None:
    """The counts and values of a command's arguments (no host needed)."""
    want = {"prepare": (2, 10**6), "share": (1, 1), "send": (1, 1), "revoke": (1, 1), "discard": (1, 1),
            "extend": (1, 1), "password": (1, 1),
            "list": (0, 1), "register": (0, 0), "setup": (0, 0)}[a.cmd]  # fmt: skip
    if not want[0] <= len(a.rest) <= want[1]:
        raise PubError("invalid.input", _USAGE, 2)
    if a.cmd == "setup":
        _check_setup(a)
    if a.cmd == "prepare" and a.days is not None and a.days not in approval.DAYS:
        days = ", ".join(str(d) for d in approval.DAYS[:-1]) + f" or {approval.DAYS[-1]}"
        raise PubError("invalid.input", f"a link works for {days} days: use --days {days}\n" + _USAGE, 2)
    if a.cmd == "extend" and (a.days is None or not 1 <= a.days <= 30):
        raise PubError("invalid.input", "extend needs --days D with D from 1 to 30\n" + _USAGE, 2)


def _check_setup(a: argparse.Namespace) -> None:
    from orch.wsproto import wire

    if not a.server or not a.share_addr:
        raise PubError("invalid.input", _USAGE, 2)
    for what, value in (("--server", a.server), ("--share", a.share_addr)):
        try:
            wire.check_origin(value)
        except wire.WireError:
            raise PubError(
                "invalid.input",
                f"{what} {value[:80]!r}: use https://host[:port], or http://127.0.0.1:PORT on this computer; no path",
                5,
            ) from None


def _setup(a: argparse.Namespace, target: Any, env: Mapping[str, str], out: TextIO) -> int:
    if not _tty_ok():
        raise PubError("tty.required", "no terminal to sign on: run this in a terminal; nothing was signed", 3)
    _sign(target, "settings.set", {"set": {"publish": {"server": a.server, "share": a.share_addr}}})
    out.write("ok publish setup\n")
    return _register(target, env, out)

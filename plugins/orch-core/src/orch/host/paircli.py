"""R6c-2: ``orch host relay pair ...`` and ``orch host relay primary set`` (orch-v2-host 11A.15.9), thin wrappers over
the pairing control ops (``host.relay_pair_*``, ``host.relay_primary_set``).

* ``orch host relay pair --workspace W [--scopes look,decide] [--ttl 10m] [--json [--json-fd N]] [--no-qr]
  [--resume --offer O] --primary external``: makes the offer (or attaches to one with ``--resume``), prints the
  native link (in a QR when ``segno`` is installed) and the HTTPS link labelled "fallback: your relay operator's page
  can read it", then **stays attached**: it shows the incoming request (the label "named by the device", the scopes,
  a warning when a second key used the link), asks the person to **type** the six characters the phone shows (3
  attempts; an empty line rejects), waits while the separate primary signs the certificate (``--primary external``),
  drives the ``relay.pair_device`` signature with the inline terminal passphrase signer (step 9; never click or
  convenience), and shows the progress to ``approved``.
* ``--primary inline`` (the design's default) runs the primary steps itself through ``orch.relay_owner``; it is **not
  built yet** (JUDGEMENT CALL, R6c-2: a follow-up), so it is refused with a fix naming ``--primary external``.
* ``orch host relay pair list|cancel|abandon --workspace W [--offer O]`` and ``orch host relay primary set
  --workspace W --cert FILE --label TEXT``.

**The link** is written only to the person's terminal (stdout) or, with ``--json``, to stdout when it is a TTY or to
the descriptor ``--json-fd`` names (the dev path's private pipe); never to a file, never as an argument of another
process, never logged. A resumed offer has no link (it is never stored). Human only: refused under ``ORCH_GRANT``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from .errors import HostError

__all__ = ["main", "parse_ttl"]

TERMINAL = ("rejected", "expired", "cancelled", "failed")
POLL_S = 1.0
FALLBACK = "fallback: your relay operator's page can read it"


def parse_ttl(text: str | None) -> int:
    """``10m``, ``90s`` or milliseconds; at most 10 minutes."""
    if text is None:
        return 600_000
    t = text.strip().lower()
    try:
        ms = int(t[:-1]) * 60_000 if t.endswith("m") else int(t[:-1]) * 1000 if t.endswith("s") else int(t)
    except ValueError:
        raise HostError("invalid.input", "--ttl: like 10m, 90s or milliseconds") from None
    if not 60_000 <= ms <= 600_000:
        raise HostError("invalid.input", "--ttl: between 1 and 10 minutes")
    return ms


class _Refused(Exception):
    def __init__(self, error: Mapping[str, Any], exit_code: int) -> None:
        super().__init__(error.get("code"))
        self.error, self.exit = dict(error), exit_code


def _caller(client: Any) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    def call(op: str, args: dict[str, Any]) -> dict[str, Any]:
        res = client.call(op, args)
        if not res.get("ok"):
            raise _Refused(res.get("error") or {"code": "internal"}, int(res.get("exit", 1)))
        return res["result"]["data"]

    return call


def _qr(link: str) -> str | None:
    try:
        import segno  # type: ignore[import-not-found]  # optional: orch[qr]
    except ImportError:
        return None
    import io

    buf = io.StringIO()
    segno.make(link, error="m").terminal(out=buf, compact=True)
    return buf.getvalue()


def _tty_ask(prompt: str) -> str:
    """One line from the person's terminal (``/dev/tty``; never stdin, which a script may feed)."""
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline().strip()
    except OSError:
        raise HostError("custody.no_prompt", "the code is typed at a terminal: run this in one") from None


def attach(
    call: Callable[[str, dict[str, Any]], dict[str, Any]],
    wid: str,
    offer_id: str,
    out: TextIO,
    *,
    ask: Callable[[str], str],
    step9: Callable[[str], None],
    emit: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_polls: int | None = None,
) -> int:
    """The attached part (11A.15.9): one line per state; the typed SAS; the external primary's wait; step 9 once the
    certificate is in hand; progress to ``approved``. Returns the exit code (0 approved, 3 otherwise)."""
    last: str | None = None
    signed = False
    waiting_shown = False
    polls = 0
    while True:
        offer = call("host.relay_pair_show", {"workspace_id": wid, "offer_id": offer_id})["offer"]
        state = offer["state"]
        if state != last:
            last = state
            out.write(f"state: {state}\n")
            out.flush()
            if emit is not None:
                emit({"offer_id": offer_id, "state": state, "expires_ms": offer["expires_ms"]})
        if state == "approved":
            out.write(f"approved: {offer.get('label') or 'the device'} is paired ({', '.join(offer['scopes'])})\n")
            return 0
        if state in TERMINAL:
            out.write(f"the pairing ended: {state}\n")
            return 3
        if state == "pending":
            if offer.get("second_key"):
                out.write("WARNING: someone else has this link (a second key used it): reject\n")
            out.write(f"request from: {offer.get('label')!r} (named by the device; not authenticated)\n")
            out.write(f"scopes: {', '.join(offer['scopes'])}\n")
            code = ask("type the 6 characters the phone shows (empty: reject): ")
            got = call(
                "host.relay_pair_answer",
                {"workspace_id": wid, "offer_id": offer_id, "code": code, "approve": bool(code)},
            )
            if got["state"] == "pending":
                out.write(f"that is not the code: {got['attempts_left']} attempt(s) left\n")
            continue
        if state in ("confirmed", "cert_pending") and offer.get("has_cert") and not signed:
            signed = True
            out.write("the certificate is verified: sign the pairing (device.added) with your passphrase\n")
            out.flush()
            step9(offer_id)
            continue
        if state == "cert_pending" and not offer.get("has_cert") and not waiting_shown:
            waiting_shown = True
            out.write("waiting for the primary (external) to sign the certificate; compare its code with the phone\n")
        polls += 1
        if max_polls is not None and polls >= max_polls:
            return 3
        sleep(POLL_S)


def _workspace(state: Path, given: str) -> dict[str, Any]:
    from .custody import read_json
    from .registry import registry_path

    reg = read_json(registry_path(state), os.getuid()) or {"workspaces": []}
    match = [w for w in reg.get("workspaces") or [] if given in (w.get("workspace_id"), w.get("prefix"))]
    if len(match) != 1:
        raise HostError("not_found", f"no single registered workspace {given}")
    return match[0]


def _step9(env: Mapping[str, str], state: Path, entry: Mapping[str, Any]) -> Callable[[str], None]:
    """Step 9 with the **inline terminal passphrase signer** (the person path, as every person command in host
    mode: ``human.prepare`` of ``relay.pair_device``, the review on ``/dev/tty``, the device key, ``human.submit``).
    Never the click signer, never ``auth:"convenience"`` (the host refuses both for this op)."""

    def run(offer_id: str) -> None:
        from orch.hostclient import Target, _connect, _durable, workspace_socket
        from orch.identity import device_ref
        from orch.ops import human

        from . import signer
        from .control_client import new_idem

        wid = entry["workspace_id"]
        host_uid = entry.get("host_uid")
        if type(host_uid) is not int:
            raise HostError("host.unavailable", "the registry entry has no host UID")
        person = ((entry.get("peers") or {}).get(str(os.getuid())) or {}).get("person")
        if not person:
            raise HostError("human_only", "this UID is not mapped to a person in this workspace")
        directory = human.keys_dir(state, wid)
        if not (directory / human.KEY_FILE).is_file():
            raise HostError("custody.no_key", "this machine holds no device key of yours for this workspace")
        target = Target(wid, host_uid, workspace_socket(env, state, host_uid, wid), "person", state, entry)
        link = [_connect(target)]
        idem = new_idem()
        name = Path(str(entry.get("root") or wid)).name
        try:
            backend = human.open_backend(directory)
            device = device_ref(backend.public_key(human.KEY_ID))
            signer.run(
                _durable(target, link, idem), "relay.pair_device", {"offer_id": offer_id}, backend=backend,
                key_id=human.KEY_ID, workspace_id=wid, person=person, device=device,
                workspace=lambda w: (name, str(entry.get("prefix"))) if w == wid else None,
                confirm=human.review_prompt, show_secret=human.show_secret, idem=idem,
            )  # fmt: skip
        finally:
            link[0].close()

    return run


def main(
    args: Any,
    env: Mapping[str, str],
    out: TextIO,
    *,
    state: Path,
    control: Path,
    ask: Callable[[str], str] = _tty_ask,
    step9: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    from orch.custody.cli import write_error

    from .control_client import ControlClient

    as_json = bool(getattr(args, "json", False))
    if env.get("ORCH_GRANT"):
        return write_error(out, as_json, "human_only", "an agent never pairs a device or sets the primary")
    sub = getattr(args, "sub", None)
    try:
        entry = _workspace(state, args.workspace)
        wid = entry["workspace_id"]
        try:
            client = ControlClient(control)
        except (OSError, ValueError) as exc:  # no socket, nobody listening, or an unsafe socket path
            raise HostError("host.unavailable", f"the orch host is not reachable ({type(exc).__name__})") from None
        with client:
            call = _caller(client)
            if args.action == "primary":
                if sub != "set" or not args.cert or not args.label:
                    raise HostError(
                        "invalid.input", "orch host relay primary set --workspace W --cert FILE --label TEXT"
                    )
                cert = json.loads(Path(args.cert).read_text())
                data = call("host.relay_primary_set", {"workspace_id": wid, "cert": cert, "label": args.label})
                out.write(json.dumps({"ok": True, "data": data}, sort_keys=True) + "\n")
                return 0
            if sub == "list":
                data = call("host.relay_pair_list", {"workspace_id": wid})
                if as_json:
                    out.write(json.dumps({"ok": True, "data": data}, sort_keys=True) + "\n")
                else:
                    for o in data["offers"]:
                        label, req = o.get("label") or "-", o.get("outstanding_request") or "-"
                        out.write(
                            f"{o['offer_id']}  {o['state']:<12} {','.join(o['scopes'])}  {label}  request={req}\n"
                        )
                return 0
            if sub in ("cancel", "abandon"):
                if not args.offer:
                    raise HostError("invalid.input", f"orch host relay pair {sub} needs --offer")
                data = call(f"host.relay_pair_{sub}", {"workspace_id": wid, "offer_id": args.offer})
                out.write(json.dumps({"ok": True, "data": data}, sort_keys=True) + "\n")
                return 0
            if sub is not None:
                raise HostError("invalid.input", "orch host relay pair [list|cancel|abandon]")
            if (args.primary or "inline") != "external":
                raise HostError(
                    "invalid.input",
                    "the inline primary is not built yet: pass --primary external (the separate primary signs the "
                    "certificate)",
                )
            emit = None
            if as_json:
                fd = getattr(args, "json_fd", None)
                if fd is not None:
                    sink = os.fdopen(int(fd), "w", closefd=False)
                elif out.isatty():
                    sink = out
                else:
                    raise HostError("invalid.input", "--json writes the link only to a TTY or to --json-fd")

                def emit(obj: dict[str, Any], sink: TextIO = sink) -> None:
                    sink.write(json.dumps(obj, sort_keys=True) + "\n")
                    sink.flush()

            if args.resume:
                if not args.offer:
                    raise HostError("invalid.input", "--resume needs --offer")
                offer_id = args.offer
            else:
                scopes = [s for s in (args.scopes or "look,decide").split(",") if s]
                offer = call(
                    "host.relay_pair_offer",
                    {"workspace_id": wid, "scopes": scopes, "ttl_ms": parse_ttl(args.ttl)},
                )
                offer_id = offer["offer_id"]
                link = offer["link"]
                if emit is not None:
                    emit({"offer_id": offer_id, "state": offer["state"], "link": link["https"],
                          "native_link": link["native"], "expires_ms": offer["expires_ms"]})  # fmt: skip
                else:
                    out.write(f"offer {offer_id} expires_ms={offer['expires_ms']}\n")
                    qr = None if args.no_qr else _qr(link["native"])
                    out.write(qr if qr else ("" if args.no_qr else "install orch[qr] for a QR\n"))
                    out.write(f"link: {link['native']}\n")
                    out.write(f"{FALLBACK}: {link['https']}\n")
                    out.flush()
                del link
            driver = step9 or _step9(env, state, entry)
            return attach(call, wid, offer_id, out, ask=ask, step9=driver, emit=emit, sleep=sleep)
    except _Refused as exc:
        out.write(json.dumps({"ok": False, "error": exc.error, "exit": exc.exit}, sort_keys=True) + "\n")
        return exc.exit
    except HostError as exc:
        return write_error(out, as_json, exc.code, exc.message, hint=exc.hint)
    except (FileNotFoundError, ConnectionRefusedError):
        return write_error(out, as_json, "host.unavailable", "the orch host is not running: orch host start")
    except (OSError, ValueError) as exc:
        return write_error(out, as_json, "invalid.input", f"{type(exc).__name__}: {exc}"[:300])
    except KeyboardInterrupt:
        out.write("interrupted: the offer stays; attach again with --resume --offer\n")
        return 130

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
* ``--primary inline`` (the default, B1b, 11A.15.23) runs the primary steps itself in this process
  (:func:`orch.relay_owner.inline.serve_request`): the primary passphrase, then the owner's recovery code (typed once,
  after the request is verified and shown), then the phone's second code; ``--primary external`` waits for a separate
  primary.
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

__all__ = ["main", "parse_ttl", "sign_genesis_pin"]

TERMINAL = ("rejected", "expired", "cancelled", "failed")
STATES = frozenset({"open", "pending", "confirmed", "cert_pending", "allocated", "published", "approved", *TERMINAL})
_LINK_CHARS = frozenset(chr(c) for c in range(0x21, 0x7F))


def safe(text: object, cap: int = 80) -> str:
    """Terminal-safe text from the relay, a phone or the host (output injection): ``clean_label`` (protocol §2.5:
    C0/C1 controls, format and bidi characters, zero-width and other default-ignorable code points removed), then
    every whitespace (a newline too) a space, only printable characters, runs collapsed, at most ``cap`` characters.
    Never printed raw next to the code the person compares."""
    from orch.identity.pairing import clean_label

    if not isinstance(text, str):
        return "?"
    try:
        cleaned = clean_label(text.replace("\n", " "))
    except Exception:  # noqa: BLE001 - lone surrogates
        return "?"
    kept = "".join(" " if c.isspace() else c for c in cleaned if c.isspace() or c.isprintable())
    return " ".join(kept.split())[:cap]


def _word(value: object, allowed: frozenset[str] | None = None) -> str:
    """A token the host sends (a state, a scope, an id): shown only when it is one we know, else ``?``."""
    if not isinstance(value, str) or not value or (allowed is not None and value not in allowed):
        return "?"
    return value if all(c.isalnum() or c in "_-." for c in value) and len(value) <= 64 else "?"


def _link(value: object) -> str:
    """A pairing link: printable ASCII only (the host builds it from a checked origin and b64u fields); anything
    else is never written to the terminal."""
    if not isinstance(value, str) or not value or len(value) > 2048 or not set(value) <= _LINK_CHARS:
        raise HostError("internal", "the host returned a link that is not printable ASCII; nothing is shown")
    return value


POLL_S = 1.0
LINK_NOTICE = "valid 10 min, single use; anyone who sees this can pair. It leaves the screen once the phone connects."


def _is_tty(out: TextIO) -> bool:
    try:
        return bool(out.isatty())
    except (AttributeError, ValueError):
        return False


def _width() -> int:
    import shutil

    return max(20, shutil.get_terminal_size((80, 24)).columns)


def _rows(text: str, width: int) -> int:
    """Terminal rows ``text`` takes at ``width`` columns (a long line wraps); QR half-blocks count one cell each."""
    return sum(max(1, -(-len(line) // width)) for line in text.split("\n")[:-1])


def _erase(out: TextIO, rows: int) -> None:
    """Move up ``rows`` rows and clear to the end of the screen (``ESC[nA`` ``ESC[J``). Never ``ESC[3J``: the
    scrollback is the terminal's, not ours (best effort, stated in the owner note)."""
    out.write(f"\x1b[{rows}A\x1b[J")
    out.write("the pairing link was removed from the screen (your terminal's scrollback may still hold it)\n")
    out.flush()
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
    """The link as a Unicode half-block QR (``segno``, a hard dependency of orch-core, pinned in the lock; in memory
    only). ``None`` only when the import fails (a broken install): the caller says so and the link text is still
    shown."""
    try:
        import segno  # type: ignore[import-not-found]
    except ImportError:
        return None
    import io

    buf = io.StringIO()
    segno.make(link, error="m").terminal(out=buf, compact=True)
    return buf.getvalue()


def _tty_ask(prompt: str, timeout: float | None = None) -> str | None:
    """One line from the person's terminal (``/dev/tty``; never stdin, which a script may feed). With ``timeout``
    (seconds) ``None`` when nothing was typed in time (L1: the read ends with the request, the caller then closes the
    person-key source)."""
    import select

    try:
        # two handles, not "r+": a terminal is not seekable, so a read-write text file on it raises
        # UnsupportedOperation (an OSError) on macOS and the prompt could never be answered
        with open("/dev/tty", "w", encoding="utf-8") as out, open("/dev/tty", encoding="utf-8") as tty:
            out.write(prompt)
            out.flush()
            if timeout is not None and not select.select([tty], [], [], max(0.0, timeout))[0]:
                out.write("\n")
                out.flush()
                return None
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
    primary: Callable[[str, str, Mapping[str, Any]], Any] | None = None,
    erase: Callable[[], None] | None = None,
) -> int:
    """The attached part (11A.15.9): one line per state; the typed SAS; the primary's step (``primary`` inline, else
    the external primary's wait); step 9 once the certificate is in hand; progress to ``approved``. Returns the exit
    code (0 approved, 3 otherwise).

    ``primary(offer_id, request_id, offer)`` is the inline primary (:func:`orch.relay_owner.inline.serve_request`); it
    is called once, when the offer waits for the certificate and the host reports the request, and returns an object
    with ``cert`` / ``rejection``, which is handed to ``host.relay_pair_cert``. If it raises, the offer is cancelled
    and the command ends (never a hang): a ``KeyboardInterrupt`` leaves the offer for ``--resume``."""
    last: str | None = None
    signed = False
    waiting_shown = False
    primary_done = False
    sas_bad = 0
    polls = 0
    was_open = False
    while True:
        offer = call("host.relay_pair_show", {"workspace_id": wid, "offer_id": offer_id})["offer"]
        state = _word(offer.get("state"), STATES)
        scopes = ", ".join(_word(x, frozenset({"look", "decide"})) for x in offer.get("scopes") or [])
        label = safe(offer.get("label"))
        if state != last:
            if was_open and erase is not None:
                erase()  # L3: the QR and both link lines leave the screen once the phone has used the link
                was_open = False
            last = state
            was_open = state == "open"
            out.write(f"state: {state}\n")
            out.flush()
            if emit is not None:
                emit({"offer_id": offer_id, "state": state, "expires_ms": offer.get("expires_ms")})
        if state == "approved":
            out.write(f"approved: {label or 'the device'} is paired ({scopes})\n")
            return 0
        if state in TERMINAL:
            out.write(f"the pairing ended: {state}\n")
            return 3
        if state == "pending":
            if offer.get("second_key"):
                out.write("WARNING: someone else has this link (a second key used it): reject\n")
            out.write(f"request from: [{label}] (named by the device; not authenticated)\n")
            out.write(f"scopes: {scopes}\n")
            code = ask(SAS_PROMPT)
            if code.strip():
                from orch.identity.pairing import normalize_code

                norm = normalize_code(code)
                if norm is None:  # M2: not the shape of a code (maybe a secret typed in the wrong place): sent nowhere
                    sas_bad += 1
                    more = len(code.split()) >= 2
                    code = ""
                    out.write("that did not look like the phone's code; nothing was sent"
                              + ("; never type your recovery code here" if more else "") + "\n")  # fmt: skip
                    if sas_bad < 3:
                        continue
                else:
                    code = norm
            got = call(
                "host.relay_pair_answer",
                {"workspace_id": wid, "offer_id": offer_id, "code": code, "approve": bool(code)},
            )
            if got["state"] == "pending":
                left = got.get("attempts_left")
                out.write(f"that is not the code: {left if type(left) is int else '?'} attempt(s) left\n")
            continue
        if state in ("confirmed", "cert_pending") and offer.get("has_cert") and not signed:
            signed = True
            out.write("the certificate is verified: sign the pairing (device.added) with your passphrase\n")
            out.flush()
            step9(offer_id)
            continue
        if state == "cert_pending" and not offer.get("has_cert") and primary is not None and not primary_done:
            rid = offer.get("outstanding_request")
            if rid:
                primary_done = True
                if not _inline_primary_step(call, wid, offer_id, str(rid), offer, primary, out):
                    return 3
                continue
        if state == "cert_pending" and not offer.get("has_cert") and primary is None and not waiting_shown:
            waiting_shown = True
            out.write("waiting for the primary (external) to sign the certificate; compare its code with the phone\n")
        polls += 1
        if max_polls is not None and polls >= max_polls:
            return 3
        sleep(POLL_S)


SAS_PROMPT = "step 1 of 2, pairing code: type the 6 characters the phone shows (empty: reject): "


def _describe(exc: BaseException) -> str:
    """A failure of the inline primary as one line of our own words (never the relay's text, a body or a secret)."""
    code = getattr(exc, "code", None)
    if type(getattr(exc, "status", None)) is int and type(exc).__name__ == "OwnerRefused":
        return f"the relay refused a request ({exc.status} {code or 'no code'})"
    if isinstance(exc, HostError):
        return _cut(f"{exc.code}: {exc.message}")
    if hasattr(exc, "message") and isinstance(code, str):
        return _cut(f"{code}: {exc.message}")
    if isinstance(code, str) and type(exc).__module__.startswith("orch.custody"):
        return _cut(f"{code}: {exc}")
    return f"{type(exc).__name__}"


def _inline_primary_step(
    call: Callable[[str, dict[str, Any]], dict[str, Any]],
    wid: str,
    offer_id: str,
    rid: str,
    offer: Mapping[str, Any],
    primary: Callable[..., Any],
    out: TextIO,
) -> bool:
    """Run the inline primary and hand its certificate (or rejection) to R6 (``relay.pair_cert``, which verifies it
    itself). On any failure the offer is cancelled and the person is told a new pairing can start; ``False``."""
    try:
        got = primary(offer_id, rid, offer)
        args: dict[str, Any] = {"workspace_id": wid, "offer_id": offer_id}
        if got.cert is not None:
            args["cert"] = got.cert
        elif got.rejection is not None:
            args["rejection"] = got.rejection
        else:  # pragma: no cover - the outcome always holds one of the two
            raise HostError("internal", "the primary decided nothing")
        call("host.relay_pair_cert", args)
        return True
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure ends the pairing cleanly; nothing hangs
        out.write(f"the primary step ended: {_describe(exc)}\n")
        try:
            call("host.relay_pair_cancel", {"workspace_id": wid, "offer_id": offer_id})
            out.write("the pairing is cancelled; start a new one with orch host relay pair --workspace W\n")
        except Exception as cancel_exc:  # noqa: BLE001
            why = _describe(cancel_exc)
            out.write(
                f"the offer could not be cancelled ({why}); orch host relay pair cancel --offer {_word(offer_id)}\n"
            )
        return False


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


def sign_genesis_pin(
    call: Callable[[str, dict[str, Any]], dict[str, Any]],
    workspace_id: str,
    *,
    backend: Any,
    key_id: str,
    name: str,
    confirm: Callable[[str, str], bool],
) -> dict[str, Any]:
    """The genesis pin's person path (11A.2, ``relay.genesis_pin``), the terminal signer. The host sends facts (its own
    ``workspace.created`` event and the relay origin); the signer recomputes the pin from them
    (:func:`orch.identity.genesispin.check_facts`: this workspace, this genesis device), shows the review, signs only
    after ``confirm(text, word)``, and sends the signed pin back. Reusable by an owner client (the dev path, T2)."""
    from orch.identity import genesispin
    from orch.identity.errors import Refused

    from .signer import SignerRefused

    facts = call("host.relay_genesis_pin_facts", {"workspace_id": workspace_id})["facts"]
    try:
        verified = genesispin.check_facts(
            facts, workspace_id=workspace_id, device_pub=bytes(backend.public_key(key_id))
        )
    except Refused as exc:
        raise SignerRefused(f"refusing to sign: {exc.code}: {exc.detail}") from None
    if not confirm(genesispin.render(verified, name=safe(name)), genesispin.confirm_word(workspace_id)):
        raise SignerRefused("not confirmed")
    pin = genesispin.sign_pin(backend, key_id, verified)
    return call("host.relay_genesis_pin_submit", {"workspace_id": workspace_id, "pin": pin})


def _genesis_pin_run(
    call: Callable[[str, dict[str, Any]], dict[str, Any]], entry: Mapping[str, Any], state: Path
) -> dict:
    from orch.ops import human

    wid = entry["workspace_id"]
    directory = human.keys_dir(state, wid)
    if not (directory / human.KEY_FILE).is_file():
        raise HostError("custody.no_key", "this machine holds no device key of yours for this workspace")
    backend = human.open_backend(directory)
    name = Path(str(entry.get("root") or wid)).name
    return sign_genesis_pin(call, wid, backend=backend, key_id=human.KEY_ID, name=name, confirm=human.review_prompt)


class _Step9Failed(HostError):
    """A step-9 refusal; ``main`` prints that the offer stays and how to sign it later."""

    def __init__(self, code: str, message: str, *, offer: str) -> None:
        super().__init__(code, message)
        self.offer = offer


def _cut(text: object) -> str:
    return safe(str(text), 200)


def _install_origin(state: Path, given: str | None) -> str | None:
    """One relay per install (owner decision 15 Oct, 11A.15.23): the relay the primary was registered on
    (``relay.json``, or the pending registration's) is the default for ``--relay``; another origin is
    ``primary.relay_mismatch``. ``None`` and nothing stored: the caller asks for ``--relay``."""
    from orch.custody import primary_register

    try:
        stored = primary_register.stored_origin(state)
    except primary_register.RegisterError as exc:
        raise HostError(exc.code, str(exc)) from None
    if stored is None or given is None:
        return given or stored
    if given != stored:
        raise HostError(
            "primary.relay_mismatch",
            "this install already uses another relay (one relay per install)",
            hint=f"use --relay {safe(stored, 200)}",
        )
    return given


def _primary_set_args(state: Path, entry: Mapping[str, Any], args: Any) -> tuple[dict[str, Any], str]:
    """``orch host relay primary set --workspace W [--cert FILE --label TEXT]``: without ``--cert`` the certificate and
    label are the registered primary's (``cert.json``/``relay.json``, made by ``orch keys primary init``), refused
    unless the certificate is for **this** machine's primary device and the workspace owner's person, and the relay is
    the one the workspace is linked to (R6 re-verifies the certificate anyway, S:7870)."""
    from orch.custody import primary, primary_register
    from orch.custody.base import CustodyError

    if args.cert:  # an explicit certificate (an external primary, the dev path): as before; R6 verifies it
        if not args.label:
            raise HostError("invalid.input", "orch host relay primary set --workspace W [--cert FILE --label TEXT]")
        return json.loads(Path(args.cert).read_text()), str(args.label)
    try:
        stored, rel = primary_register.read_cert(state), primary_register.read_relay(state)
        if stored is None or rel is None or not stored["registered"]:
            raise HostError(
                "primary.not_registered",
                "this install has no registered primary",
                hint="orch keys primary init --workspace W --relay ORIGIN --invite-file F",
            )
        cert, label = stored["cert"], args.label or rel.get("label") or "primary"
        o = cert.get("o") if isinstance(cert, dict) else None
        if not isinstance(o, dict):
            raise HostError("invalid.input", "the stored certificate is not a device certificate")
        if o.get("device_id") != primary.load(state).device_id.hex():
            raise HostError("invalid.input", "the stored certificate is not for this machine's primary device")
        from orch import crypto
        from orch.custody.cli import _Fail, owner_pk_from_workspace

        try:
            owner = owner_pk_from_workspace(state, entry)
        except _Fail as exc:
            raise HostError(exc.code, exc.message) from None
        if o.get("person_id") != crypto.person_id(owner).hex():
            raise HostError("invalid.input", "the stored certificate is not for the workspace owner's person")
        _link_origin_check(state, entry)
    except (CustodyError, primary_register.RegisterError) as exc:
        raise HostError(str(getattr(exc, "code", "custody")), _cut(str(exc))) from None
    return cert, str(label)


def _link_origin_check(state: Path, entry: Mapping[str, Any]) -> None:
    """The stored relay must be the one the workspace is linked to (else ``primary.relay_mismatch``)."""
    from orch.custody import primary_register

    from .custody import read_json
    from .relaylink import link_path

    link = read_json(link_path(state, entry["workspace_id"]), int(entry.get("host_uid", os.getuid())))
    mine = primary_register.stored_origin(state)
    if isinstance(link, dict) and mine is not None and link.get("origin") != mine:
        raise HostError("primary.relay_mismatch", "the workspace is linked to another relay than this primary's")


LINK_WAIT_S = 90.0
OFF_GRACE_S = 5.0


def _link_run(call: Callable[[str, dict[str, Any]], dict[str, Any]], entry: Mapping[str, Any], state: Path, args: Any,
          out: TextIO, sleep: Callable[[float], None]) -> dict[str, Any]:  # fmt: skip
    """``orch host relay link --workspace W --relay ORIGIN --max-publication-bytes N [--ca-file F] [--proxy U]``
    (11A.4, 11A.5, 11A.15.11 step 7): the reservation (relay-info first), the owner's genesis-pin signature at this
    terminal (:func:`sign_genesis_pin`), then ``host.relay_start``: the worker drives the card, login, hold, genesis,
    the first publication and ``linked``. Waits (bounded) for ``linked`` and says where it stopped otherwise."""
    wid = entry["workspace_id"]
    origin = _install_origin(state, args.relay)
    if not origin or not args.max_publication_bytes:
        raise HostError("invalid.input", "orch host relay link --workspace W --relay ORIGIN --max-publication-bytes N")
    req: dict[str, Any] = {
        "workspace_id": wid,
        "origin": origin,
        "max_publication_bytes": args.max_publication_bytes,
    }
    if args.ca_file:
        req["ca_file"] = args.ca_file
    if args.proxy:
        req["proxy"] = args.proxy
    reserved = call("host.relay_link", req)
    out.write(f"reserved: relay {_cut(reserved.get('origin'))} (the workspace is now permanently relay-linked)\n")
    out.flush()
    _genesis_pin_run(call, entry, state)
    started = call("host.relay_start", {"workspace_id": wid})
    begun = time.monotonic()
    deadline = begun + LINK_WAIT_S
    relay: dict[str, Any] = {}
    while time.monotonic() < deadline:
        relay = call("host.relay_status", {"workspace_id": wid}).get("relay") or {}
        # "off" right after the start can be the starter thread not having registered the worker yet: it ends the
        # wait only when the start itself answered "off" or after OFF_GRACE_S
        off = relay.get("worker") == "off" and (
            started.get("worker") == "off" or time.monotonic() - begun >= OFF_GRACE_S
        )
        if relay.get("link") == "linked" or relay.get("worker") == "failed" or off:
            break
        sleep(POLL_S)
    return {
        "workspace_id": wid, "link": _word(relay.get("link")), "worker": _word(relay.get("worker")),
        "reason": _cut(relay.get("reason")) if relay.get("reason") else None, "start": _word(started.get("worker")),
    }  # fmt: skip


def _genesis_guard(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """The genesis pin's errors as :class:`HostError` (never a traceback), as :func:`_guarded` does for step 9."""
    from orch.custody.base import CustodyError
    from orch.ops.errors import OrchError

    from . import frames
    from .signer import SignerRefused

    try:
        return run()
    except SignerRefused as exc:
        raise HostError("invalid.input", _cut(f"{exc}; nothing was signed")) from None
    except CustodyError as exc:
        raise HostError(str(getattr(exc, "code", "custody")), _cut(str(exc))) from None
    except OrchError as exc:
        raise HostError(str(exc.code), _cut(str(exc))) from None
    except (OSError, ConnectionError, TimeoutError, frames.FrameError) as exc:
        raise HostError("host.unavailable", _cut(f"lost the connection to the orch host ({exc})")) from None


def _inline_driver(
    env: Mapping[str, str], state: Path, entry: Mapping[str, Any], out: TextIO,
    ask_timed: Callable[[str, float | None], str | None],
) -> Callable[[str, str, Mapping[str, Any]], Any]:  # fmt: skip
    """The product's inline primary (B1b) for :func:`attach`: :func:`orch.custody.pairinline.driver` (the owner client
    is the person path's, never imported by the host daemon, N4) with this module's terminal text rules."""
    from orch.custody import pairinline

    def show(text: str) -> None:
        out.write(safe(text, 300) + "\n")
        out.flush()

    return pairinline.driver(
        state, entry, show=show, ask_timed=ask_timed, link_check=lambda: _link_origin_check(state, entry),
        fail=lambda code, message, hint=None: HostError(code, message, hint=hint),
    )  # fmt: skip


def _guarded(step9: Callable[[str], None]) -> Callable[[str], None]:
    """Step 9's errors as :class:`HostError` (Opus review of #589, M1, as ``hostclient._human`` maps them), so every
    one ends in ``main``'s cleaned refusal, never a traceback: a declined review (``SignerRefused``, "not
    confirmed"), the key custody (``CustodyError``), the host connection (``OrchError``/``HostFailure``,
    ``FrameError``, ``OSError``, ``TimeoutError``), anything else as ``internal``. The hint says the offer stays."""

    def run(offer_id: str) -> None:
        from orch.custody.base import CustodyError
        from orch.ops.errors import OrchError

        from . import frames
        from .signer import SignerRefused

        oid = _word(offer_id)
        try:
            step9(offer_id)
        except HostError as exc:
            raise _Step9Failed(exc.code, _cut(exc.message), offer=oid) from None
        except SignerRefused as exc:
            declined = "not confirmed" in str(exc)
            text = "not confirmed: nothing was signed" if declined else f"{exc}; nothing was signed"
            raise _Step9Failed("invalid.input", _cut(text), offer=oid) from None
        except CustodyError as exc:
            raise _Step9Failed(str(getattr(exc, "code", "custody")), _cut(str(exc)), offer=oid) from None
        except OrchError as exc:
            raise _Step9Failed(str(exc.code), _cut(str(exc)), offer=oid) from None
        except (OSError, ConnectionError, TimeoutError, frames.FrameError) as exc:
            raise _Step9Failed(
                "host.unavailable", _cut(f"lost the connection to the orch host ({exc})"), offer=oid
            ) from None
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - never a traceback: the class name only
            raise _Step9Failed("internal", _cut(f"step 9 failed ({type(exc).__name__})"), offer=oid) from None

    return run


def _json_sink(fd: object, out: TextIO) -> TextIO:
    """Where ``--json`` writes the link (11A.15.9, Opus #589 L4): stdout only when it is a TTY, else ``--json-fd``,
    which must be a pipe or a socket (never a regular file, never fd 0, 1 or 2)."""
    import stat

    if fd is None:
        if out.isatty():
            return out
        raise HostError("invalid.input", "--json writes the link only to a TTY or to --json-fd")
    if type(fd) is not int or fd < 3:
        raise HostError("invalid.input", "--json-fd must be a descriptor above 2 (a pipe or a socket)")
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        raise HostError("invalid.input", "--json-fd is not an open descriptor") from None
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
        raise HostError("invalid.input", "--json-fd must be a pipe or a socket: the link is never written to a file")
    return os.fdopen(fd, "w", closefd=False)


def main(
    args: Any,
    env: Mapping[str, str],
    out: TextIO,
    *,
    state: Path,
    control: Path,
    ask: Callable[[str], str] = _tty_ask,  # type: ignore[assignment]
    step9: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    primary: Callable[[str, str, Mapping[str, Any]], Any] | None = None,
    ask_timed: Callable[[str, float | None], str | None] | None = None,
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
            if args.action == "link":
                data = _genesis_guard(lambda: _link_run(call, entry, state, args, out, sleep))
                out.write(json.dumps({"ok": True, "data": data}, sort_keys=True) + "\n")
                return 0
            if args.action == "genesis-pin":
                data = _genesis_guard(lambda: _genesis_pin_run(call, entry, state))
                out.write(json.dumps({"ok": True, "data": data}, sort_keys=True) + "\n")
                return 0
            if args.action == "primary":
                if sub != "set":
                    raise HostError("invalid.input", "orch host relay primary set --workspace W")
                cert, label = _primary_set_args(state, entry, args)
                data = call("host.relay_primary_set", {"workspace_id": wid, "cert": cert, "label": label})
                out.write(json.dumps({"ok": True, "data": data}, sort_keys=True) + "\n")
                return 0
            if sub == "list":
                data = call("host.relay_pair_list", {"workspace_id": wid})
                if as_json:
                    out.write(json.dumps({"ok": True, "data": data}, sort_keys=True) + "\n")
                else:
                    for o in data["offers"]:
                        label = safe(o.get("label")) or "-"
                        req = _word(o.get("outstanding_request")) if o.get("outstanding_request") else "-"
                        sc = ",".join(_word(x, frozenset({"look", "decide"})) for x in o.get("scopes") or [])
                        st = _word(o.get("state"), STATES)
                        out.write(f"{_word(o.get('offer_id'))}  {st:<12} {sc}  [{label}]  request={req}\n")
                return 0
            if sub in ("cancel", "abandon"):
                if not args.offer:
                    raise HostError("invalid.input", f"orch host relay pair {sub} needs --offer")
                data = call(f"host.relay_pair_{sub}", {"workspace_id": wid, "offer_id": args.offer})
                out.write(json.dumps({"ok": True, "data": data}, sort_keys=True) + "\n")
                return 0
            if sub is not None:
                raise HostError("invalid.input", "orch host relay pair [list|cancel|abandon]")
            inline = (args.primary or "inline") != "external"
            emit = None
            erase = None
            if as_json:
                sink = _json_sink(getattr(args, "json_fd", None), out)

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
                offer_id = _word(offer.get("offer_id"))
                link = {"native": _link(offer["link"]["native"]), "https": _link(offer["link"]["https"])}
                if emit is not None:
                    emit({"offer_id": offer_id, "state": offer["state"], "link": link["https"],
                          "native_link": link["native"], "expires_ms": offer["expires_ms"]})  # fmt: skip
                else:
                    exp = offer.get("expires_ms")
                    qr = None if args.no_qr else _qr(link["native"])
                    text = f"offer {offer_id} expires_ms={exp if type(exp) is int else '?'}\n"
                    if qr:
                        text += qr
                    elif not args.no_qr:
                        text += "no QR (the QR library is missing from this install); use the link below\n"
                    text += f"link: {link['native']}\n"
                    text += f"{FALLBACK}: {link['https']}\n"
                    text += LINK_NOTICE + "\n"
                    out.write(text)
                    out.flush()
                    if _is_tty(out):
                        rows = _rows(text, _width())

                        def erase(out: TextIO = out, rows: int = rows) -> None:
                            _erase(out, rows + 1)  # + the "state: open" line printed after the block
                del link
            driver = _guarded(step9 or _step9(env, state, entry))
            inline_primary = None
            if inline:
                timed = ask_timed or ((lambda p, t: _tty_ask(p, t)) if ask is _tty_ask else (lambda p, _t: ask(p)))
                inline_primary = primary or _inline_driver(env, state, entry, out, timed)
            return attach(call, wid, offer_id, out, ask=ask, step9=driver, emit=emit, sleep=sleep,
                          primary=inline_primary, erase=erase)  # fmt: skip
    except _Refused as exc:  # the host's refusal, JSON-encoded (ASCII: every control character escaped)
        out.write(json.dumps({"ok": False, "error": exc.error, "exit": exc.exit}, sort_keys=True, ensure_ascii=True))
        out.write("\n")
        return exc.exit
    except HostError as exc:
        if isinstance(exc, _Step9Failed):
            out.write(f"nothing was added; the offer stays: sign later with orch host relay pair --resume --offer "
                      f"{exc.offer}\n")  # fmt: skip
        hint = safe(exc.hint, 300) if exc.hint else None
        return write_error(out, as_json, _word(exc.code) if exc.code else "internal", safe(exc.message, 400), hint=hint)
    except (FileNotFoundError, ConnectionRefusedError):
        return write_error(out, as_json, "host.unavailable", "the orch host is not running: orch host start")
    except (OSError, ValueError) as exc:
        return write_error(out, as_json, "invalid.input", safe(f"{type(exc).__name__}: {exc}", 300))
    except KeyboardInterrupt:
        out.write("interrupted: the offer stays; attach again with --resume --offer\n")
        return 130

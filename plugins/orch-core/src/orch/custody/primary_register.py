"""``orch keys primary init --workspace W --relay ORIGIN --invite-file F`` (orch-v2-host 11A.15.23, B1b): the primary's
certificate and the relay account, in one command, resumable.

Steps (S:8390): [primary keys if absent] -> the owner's recovery code (typed once, ``RecoveryCodeSource``, matched
against the workspace owner's pinned key before anything is signed) -> the primary's PK-signed certificate -> WRITE
AHEAD ``<primary>/cert.json`` (public only, ``registered: false``) -> ``POST /v2/accounts`` -> ``registered: true`` and
``relay.json {origin, person_id, device_id, label}``.

* **Resume.** A crash between the POST and the rewrite leaves ``registered: false``: the next run first tries to log
  in as the primary (the account exists then); success marks it registered, no recovery code, no invite. Else it
  repeats the POST with the **stored** certificate bytes (never a second, different certificate for the device).
* **409 ``account_exists``** (the person has an account, e.g. made by an earlier install): ``PUT /v2/devices/{d}``
  with the same stored certificate (``register_device``), then login, then registered (review M4).
* **One relay per install** (owner decision 15 Oct): ``relay.json``/``cert.json`` name the install's relay; another
  origin is ``primary.relay_mismatch``.
* **The invite** is read from a 0600 file (``O_NOFOLLOW``, ``fstat`` on the descriptor), never printed, logged,
  stored or put in an error text (review L5); the file is not deleted here.

Nothing secret is stored: not the recovery code, not PK, not the invite. Pure with injectable seams (``pk_factory``,
``client_factory``); the CLI wires the real ones.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import crypto

from . import primary
from .base import CustodyError

__all__ = [
    "RegisterError",
    "cert_path",
    "check_origin",
    "default_label",
    "read_cert",
    "read_invite_file",
    "read_relay",
    "register",
    "relay_path",
    "stored_origin",
]

INVITE_MAX = 512
#: a login answered with one of these means "the relay does not know this primary" (anything else is not that)
LOGIN_UNKNOWN = (401, 403, 404)
_INVITE = re.compile(r"[\x21-\x7e]{1,256}")


class RegisterError(CustodyError):
    """A refusal with a catalog code (``primary.*``) and a fix text; the message never carries the invite, a relay body
    or a secret."""

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code, self.hint = code, hint


def cert_path(state: str | os.PathLike[str]) -> Path:
    return primary.primary_root(state) / "cert.json"


def relay_path(state: str | os.PathLike[str]) -> Path:
    return primary.primary_root(state) / "relay.json"


# -- small private files ----------


def _read_private(path: Path) -> Any | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise RegisterError("primary.not_registered", f"{path.name} cannot be read (is it a link?)") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise RegisterError("primary.not_registered", f"{path.name} is not a private file of yours")
        raw = os.read(fd, 64 * 1024 + 1)
    finally:
        os.close(fd)
    if len(raw) > 64 * 1024:
        raise RegisterError("primary.not_registered", f"{path.name} is too large")
    try:
        return json.loads(raw)
    except ValueError:
        raise RegisterError("primary.not_registered", f"{path.name} is not readable JSON") from None


def _write_private(path: Path, doc: Any) -> None:
    """Replace ``path`` atomically and durably, mode 0600, in the primary's 0700 directory."""
    data = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def read_cert(state: str | os.PathLike[str]) -> dict[str, Any] | None:
    """``{v, registered, origin, cert}`` or ``None``."""
    doc = _read_private(cert_path(state))
    if doc is None:
        return None
    if type(doc) is not dict or type(doc.get("cert")) is not dict or type(doc.get("registered")) is not bool:
        raise RegisterError("primary.not_registered", "cert.json is not a primary certificate record")
    return doc


def read_relay(state: str | os.PathLike[str]) -> dict[str, Any] | None:
    """``{v, origin, person_id, device_id, label}`` of a registered primary, or ``None``."""
    doc = _read_private(relay_path(state))
    if doc is None:
        return None
    if type(doc) is not dict or type(doc.get("origin")) is not str:
        raise RegisterError("primary.not_registered", "relay.json is not a relay record")
    return doc


def stored_origin(state: str | os.PathLike[str]) -> str | None:
    """The install's relay origin (registered), else the pending record's, else ``None``."""
    rel = read_relay(state)
    if rel is not None:
        return rel["origin"]
    cert = read_cert(state)
    return cert.get("origin") if cert else None


def read_invite_file(path: str | os.PathLike[str]) -> str:
    """The invite: one line of a 0600 regular file of this user, opened without following a link and checked by
    ``fstat`` on the descriptor. The text is never in an error."""

    def bad(why: str) -> RegisterError:
        return RegisterError(
            "primary.invite_file",
            f"--invite-file: {why}",
            "a 0600 file of yours holding the invite on one line (chmod 600 FILE)",
        )

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise bad("cannot be opened (missing, or a symbolic link)") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise bad("is not a regular file")
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise bad("must be a 0600 file of yours")
        raw = os.read(fd, INVITE_MAX + 1)
    finally:
        os.close(fd)
    if len(raw) > INVITE_MAX:
        raise bad("is too large")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise bad("is not an invite") from None
    lines = [x for x in text.splitlines() if x.strip()]
    if len(lines) != 1 or not _INVITE.fullmatch(lines[0].strip()):
        raise bad("must hold exactly one line, the invite")
    return lines[0].strip()


def default_label() -> str:
    from orch.identity.pairing import clean_label

    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = "this Mac"
    return clean_label(f"{host or 'this Mac'} (primary)")[:60]


def check_origin(origin: str) -> str:
    from orch.wsproto import wire

    try:
        return wire.check_origin(origin)
    except wire.WireError:
        raise RegisterError(
            "invalid.input", "--relay: an https origin like https://relay.example (http only for loopback)"
        ) from None


def _ms() -> int:
    return time.time_ns() // 1_000_000


# -- the command ----------


def register(
    state: str | os.PathLike[str],
    *,
    origin: str,
    owner_pk: bytes,
    invite_file: str | os.PathLike[str] | None,
    label: str | None = None,
    pk_factory: Callable[..., Any],
    client_factory: Callable[[str, Any], Any],
    now_ms: Callable[[], int] = _ms,
    say: Callable[[str], None] = lambda _t: None,
) -> dict[str, Any]:
    """Make the primary's certificate and the relay account (module doc). ``owner_pk`` is the workspace owner's key
    from its genesis; ``pk_factory(labels=..., pk_pub=..., action=...)`` is the recovery-code source (a context
    manager); ``client_factory(origin, session)`` the :class:`orch.relay_owner.OwnerClient`. Returns the public
    result ``{origin, person_id, device_id, label, resumed}``."""
    from orch.relay_owner import OwnerError, OwnerRefused, primary_certificate

    origin = check_origin(origin)
    person_id = crypto.person_id(owner_pk).hex()
    stored_rel, stored = read_relay(state), read_cert(state)
    for have in (stored_rel and stored_rel["origin"], stored and stored.get("origin")):
        if have is not None and have != origin:
            raise RegisterError(
                "primary.relay_mismatch",
                "this install already uses another relay (one relay per install)",
                f"use --relay {have}",
            )
    if stored_rel is not None and stored is not None and stored["registered"]:
        return {**_public(stored_rel), "resumed": True, "already": True}

    if not primary.keys_dir(state).is_dir():
        say("making the primary key pair (you choose its passphrase)")
        primary.init(state)
        say("enter the primary passphrase once more to unlock it")

    def failed(exc: OwnerError, what: str) -> RegisterError:
        # never the invite, never a relay body: the status and the documented code only
        if isinstance(exc, OwnerRefused):
            return RegisterError(
                "primary.account_failed", f"the relay refused {what} ({exc.status} {exc.code or 'no code'})"
            )
        return RegisterError(
            "primary.account_failed", f"could not reach the relay {origin}; nothing was lost, run again"
        )

    with primary.unlocked(state, action="register your primary with the relay") as session:
        client = client_factory(origin, session)
        try:
            if stored is not None:
                cert = stored["cert"]
                if cert["o"]["device_id"] != session.device_id.hex():
                    raise RegisterError("primary.not_registered", "the stored certificate is not for this primary")
                if cert["o"].get("person_id") != person_id:
                    raise RegisterError(
                        "primary.not_registered", "the stored certificate is not for this workspace's owner"
                    )
                label = stored.get("label") or label
                try:  # the account may exist already (a crash after the POST)
                    client.login()
                except OwnerRefused as exc:
                    if exc.status not in LOGIN_UNKNOWN:
                        raise failed(exc, "the login") from None
                    # a refusal: the relay does not know this primary yet, so the POST below goes out
                except OwnerError as exc:  # transport: the relay was not reached; says nothing about the account
                    raise failed(exc, "the login") from None
                else:
                    return _mark(state, origin, person_id, cert, label, resumed=True)
            else:
                if not invite_file:
                    raise RegisterError(
                        "primary.invite_file", "--invite-file is required", "ask the relay operator for an invite"
                    )
                read_invite_file(invite_file)  # refuse a bad file before the recovery code is asked
                label = label or default_label()
                with pk_factory(
                    labels={"sig_device_cert"}, pk_pub=owner_pk, action="register the primary key on the relay (once)"
                ) as pk:
                    cert = primary_certificate(pk, session, session.dk_kx_pub, label=label, created_ms=now_ms())
                _write_private(
                    cert_path(state), {"v": 1, "registered": False, "origin": origin, "label": label, "cert": cert}
                )
            got = _post(client, cert, owner_pk, invite_file, failed)
        finally:
            with contextlib.suppress(Exception):
                client.close()
    if got != person_id:
        raise RegisterError("primary.account_failed", "the relay created another person")
    return _mark(state, origin, person_id, cert, label, resumed=stored is not None)


def _post(client: Any, cert: dict[str, Any], owner_pk: bytes, invite_file: Any, failed: Callable[..., Any]) -> str:
    from orch.relay_owner import OwnerError, OwnerRefused

    invite: str | None = read_invite_file(invite_file) if invite_file else None
    if invite is None:
        raise RegisterError(
            "primary.invite_unusable",
            "the relay did not know this primary and no invite was given",
            "run again with --invite-file (ask the relay operator for a new invite)",
        )
    try:
        return client.create_account(invite, owner_pk, cert)
    except OwnerRefused as exc:
        if exc.code == "account_exists":
            return _join_existing(client, cert, owner_pk)
        if exc.code in ("not_eligible", "revoked") and exc.status == 403:
            raise RegisterError(
                "primary.invite_unusable",
                "the relay refused the invite (unknown, used or expired)",
                "ask the relay operator for a new invite",
            ) from None
        raise failed(exc, "the account") from None
    except OwnerError as exc:
        raise failed(exc, "the account") from None
    finally:
        invite = None


def _join_existing(client: Any, cert: dict[str, Any], owner_pk: bytes) -> str:
    """409 ``account_exists`` (review M4): the same stored certificate through ``PUT /v2/devices`` then a login."""
    from orch.relay_owner import OwnerError, OwnerRefused

    try:
        client.register_device(cert, owner_pk)
        client.login()
    except OwnerRefused as exc:
        if exc.code == "cert_conflict":
            raise RegisterError(
                "primary.account_exists",
                "this person already has a different primary registered on the relay",
                "ask the relay operator to remove the old primary device, or use the install that made it",
            ) from None
        if exc.code == "other_person":
            raise RegisterError(
                "primary.account_exists", "the relay says this device belongs to another person"
            ) from None
        raise RegisterError(
            "primary.account_failed", f"the relay refused the primary's device ({exc.status} {exc.code or 'no code'})"
        ) from None
    except OwnerError:
        raise RegisterError(
            "primary.account_failed", "could not reach the relay to register the primary's device; run again"
        ) from None
    return crypto.person_id(owner_pk).hex()


def _public(rel: dict[str, Any]) -> dict[str, Any]:
    return {k: rel.get(k) for k in ("origin", "person_id", "device_id", "label")}


def _mark(
    state: str | os.PathLike[str],
    origin: str,
    person_id: str,
    cert: dict[str, Any],
    label: str | None,
    *,
    resumed: bool,
) -> dict[str, Any]:
    prev = read_cert(state)
    label = (prev or {}).get("label") or label or default_label()
    _write_private(cert_path(state), {"v": 1, "registered": True, "origin": origin, "label": label, "cert": cert})
    rel = {"v": 1, "origin": origin, "person_id": person_id, "device_id": cert["o"]["device_id"], "label": label}
    _write_private(relay_path(state), rel)
    return {**_public(rel), "resumed": resumed}

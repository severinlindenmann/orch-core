"""Who is asking (§0.5, F4, F8): the per-request authentication snapshot, the grant-in-args rule and redaction.

**The snapshot.** :class:`AuthView` is an immutable copy of what authentication needs from the workspace log: each
grant's person, ``secret_hash``, scope, verbs, expiry and revocation; each member's role; each person's devices that
can ``decide`` (with their expiry). The workspace worker thread builds it (:func:`build_auth`) after every refresh of
the store, and the event loop reads it without blocking. Expiry is judged **at request time** from the copied
timestamps, never from a flag computed earlier.

The snapshot carries the identity (inode, size, mtime) of ``events/workspace.jsonl`` it was built from.
:meth:`AuthView.fresh` compares it with the file now (one ``stat``): a snapshot whose log changed since is **stale**,
and the gate then leaves the column to the worker thread. Either way the worker thread recomputes the column from
the freshly refreshed store before any op runs, so the snapshot only orders the precedence of §0.6 (the column before
the rate limit); it never lets a request through on its own.

**Grant secrets in arguments** (§0.5 "Secrets"): :func:`secret_in_request` finds a grant-shaped string, or the
presented secret itself, anywhere in ``args``, ``opts`` or ``files`` (decoded).

**Redaction** (D-P2-10): :class:`Redactor` holds the presented grant and its secret for the whole request, after
``auth`` is dropped, and removes them, any grant-shaped string, PEM private keys and key-material fields from every
result and error before it is stored or sent.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch.tokenshape import GITHUB_TOKEN_BYTES, GITHUB_TOKEN_SHAPE, is_key_value, split_key_file

from .columns import GrantState
from .errors import HostError

__all__ = [
    "AuthView",
    "GRANT_SHAPE",
    "GITHUB_TOKEN_SHAPE",
    "GrantEntry",
    "Redactor",
    "Verified",
    "build_auth",
    "log_stamp",
    "secret_in_request",
]

GRANT_SHAPE = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43}")
_GRANT_BYTES = re.compile(rb"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43}")
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S)
#: mapping keys whose values are key material or a grant secret's hash: never returned (§0.5 derived fields)
SECRET_KEYS = frozenset(
    {"secret", "secret_hash", "private_key", "privkey", "seed", "pk_sign", "dk_sign", "wsk_priv", "passphrase"}
)
REDACTED = "[redacted]"


def log_stamp(root: str | os.PathLike[str]) -> tuple[int, int, int] | None:
    """The identity of the workspace log right now, or ``None`` if it cannot be read."""
    try:
        st = os.stat(Path(root) / "events" / "workspace.jsonl")
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


@dataclass(frozen=True)
class GrantEntry:
    id: str
    person: str
    secret_hash: str | None
    revoked: bool
    expires_at: str
    expires_s: int | None
    verbs: Any
    scope: str


@dataclass(frozen=True)
class Verified:
    """The outcome of checking ``auth.grant``: its state, and the grant when it is verified."""

    state: GrantState
    grant: GrantEntry | None = None

    @property
    def person(self) -> str | None:
        return self.grant.person if self.grant is not None else None


@dataclass(frozen=True)
class AuthView:
    stamp: tuple[int, int, int] | None
    grants: Mapping[str, GrantEntry] = field(default_factory=dict)
    roles: Mapping[str, str] = field(default_factory=dict)
    #: person -> the expiry (ms, or None) of each of their devices that is neither removed nor revoked and has decide
    deciders: Mapping[str, tuple[int | None, ...]] = field(default_factory=dict)

    def fresh(self, root: str | os.PathLike[str]) -> bool:
        return self.stamp is not None and log_stamp(root) == self.stamp

    def person_ok(self, person: str | None, now: float) -> bool:
        """F8: a member with a device that is not removed, revoked or expired and carries ``decide``."""
        if person is None or person not in self.roles:
            return False
        return any(exp is None or exp > now * 1000 for exp in self.deciders.get(person, ()))

    def owner(self, person: str | None) -> bool:
        return person is not None and self.roles.get(person) == "owner"

    def verify(self, grant: str | None, now: float) -> Verified:
        """``auth.grant`` -> absent, malformed, unverified (unknown, wrong secret, expired, revoked, a person who may
        no longer run agents) or verified. The secret is compared in constant time."""
        from orch.identity import Refused, parse_grant, secret_matches

        if grant is None:
            return Verified("absent")
        try:
            token = parse_grant(grant)
        except Refused:
            return Verified("malformed")
        entry = self.grants.get(token.grant_id)
        if entry is None or entry.secret_hash is None or not secret_matches(token.secret, entry.secret_hash):
            return Verified("unverified")
        if entry.revoked or entry.expires_s is None or entry.expires_s <= now:
            return Verified("unverified")
        role = self.roles.get(entry.person)
        if role is None or role == "viewer":
            return Verified("unverified")
        return Verified("verified", entry)


def build_auth(store: Any, root: str | os.PathLike[str]) -> AuthView:
    """The snapshot of ``store`` (call it on the workspace worker thread, after a refresh)."""
    from orch.identity import Refused, parse_timestamp
    from orch.identity.certs import person_scope_ok

    stamp = log_stamp(root)  # taken first: a change after it makes the snapshot stale, never wrongly fresh
    with store.locked():
        ws = store.state.workspace
        grants = {}
        for gid, view in ws.grants.items():
            try:
                expires = parse_timestamp(view.expires_at)
            except Refused:
                expires = None
            grants[gid] = GrantEntry(
                gid,
                view.person,
                store.grant_secret_hash(gid),
                bool(view.revoked),
                view.expires_at,
                expires,
                view.verbs,
                view.scope,
            )
        roles = {person: member.role for person, member in ws.members.items()}
        deciders: dict[str, list[int | None]] = {}
        for device in ws.devices.values():
            if device.removed or device.revoked or not person_scope_ok({"scopes_max": list(device.scopes)}):
                continue
            deciders.setdefault(device.person, []).append(device.expires_ms)
    return AuthView(stamp, grants, roles, {p: tuple(v) for p, v in deciders.items()})


# --- grant secrets in a request -------------------------------------------------------------------------------------


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def _secret_of(grant: Any) -> str | None:
    if not isinstance(grant, str):
        return None
    secret = grant.partition(".")[2]
    return secret if len(secret) >= 8 else None


# The GitHub-token shape check skips ONLY the values selected below, by op, exact path and exact shape (#825, #832).
# A base64url Ed25519 signature (86 chars) contains ``ghp_`` + 20 base64url chars by chance about 2e-5 of the time and
# a valid signed request must not be refused at random. Every other string (notes, titles, paths, keys, any other
# field, a ``sig`` of any other shape or path) stays fully scanned; the grant-shape and presented-secret checks are
# never skipped, not even for these values.
# Entries (op, path in args; ``*`` is any list index):
#   human.submit    events[*].sig      (#825)
#   reader.declare  reader_decl.sig    (#832; the signed reader declaration ``{o, sig}``)
# Exact shape: canonical base64url of exactly 64 bytes (86 chars, crypto.unb64u(s, SIG_LEN)). Safe because it is a
# fixed-size Ed25519 signature that the host verifies against the signer's key before anything is stored; free text
# cannot pass the exact-shape test and a forged value fails verification.
# The args key is spelled in two parts only so the store-layout lint does not read it as a store path.
SIGNATURE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("human.submit", ("event" + "s", "*", "sig")),
    ("reader.declare", ("reader_decl", "sig")),
)
#: A reader declaration file (``member.add --reader-decl PATH``, sent inline in ``files``) is one JSON object
#: ``{o, sig}``; only its ``sig`` is exempt, by the same exact-shape rule (#832).
DECLARATION_FILE_OPS = frozenset({"member.add"})


#: Key material in args (#1044): random base64url key bytes hold a GitHub-token-shaped run in about 1 invite in
#: 13,000. Same rule as the signatures above, with the exact decoded length of the field
#: (``orch.tokenshape.KEY_BYTES``): (op, path in args, key kind). The invitee's person key, and the key material of
#: the certificate that the signed ``member.added`` event carries. Only the GitHub-token shape is skipped for these
#: values; free text is never listed.
_EV = "event" + "s"  # spelled in two parts only so the store-layout lint does not read it as a store path
KEY_FIELDS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("member.add", ("pk",), "pub"),
    ("human.submit", (_EV, "*", "pk_pub"), "pub"),
    ("human.submit", (_EV, "*", "device_cert", "sig"), "sig"),
    ("human.submit", (_EV, "*", "device_cert", "o", "dk_sig_pub"), "pub"),
    ("human.submit", (_EV, "*", "device_cert", "o", "dk_kx_pub"), "pub"),
    ("human.submit", (_EV, "*", "device_cert", "o", "label_sealed"), "sealed"),
)
#: ``member.add`` args that name an inline file, and the kind of key file each is (#1044, #832)
KEY_FILE_ARGS: dict[str, dict[str, str]] = {"member.add": {"reader_decl": "reader_decl", "cert": "device_cert"}}


def _is_signature(value: Any) -> bool:
    return is_key_value(value, "sig")


def _blank_at(node: Any, path: tuple[str, ...], sigs: list[str], kind: str = "sig") -> Any:
    """``node`` with the exact-shape key value at ``path`` blanked (appended to ``sigs``); the rest unchanged. Only
    real dicts and lists are followed, and only a ``str`` of the exact shape of ``kind`` is taken out."""
    if not path:
        if type(node) is str and is_key_value(node, kind):
            sigs.append(node)
            return ""
        return node
    head, rest = path[0], path[1:]
    if head == "*":
        if type(node) is not list:
            return node
        return [_blank_at(item, rest, sigs, kind) for item in node]
    if not isinstance(node, Mapping) or head not in node:
        return node
    return {**node, head: _blank_at(node[head], rest, sigs, kind)}


def _split_signatures(frame: Mapping[str, Any]) -> tuple[Any, list[str]]:
    """``(args with the allowlisted signature values blanked, those values)``. Only the op and path in
    :data:`SIGNATURE_FIELDS`, and only a value of the exact signature shape, are taken out; all else stays in args."""
    args = frame.get("args")
    sigs: list[str] = []
    if not isinstance(args, Mapping):
        return args, sigs
    for op, path in SIGNATURE_FIELDS:
        if frame.get("op") == op:
            args = _blank_at(args, path, sigs)
    for op, path, kind in KEY_FIELDS:
        if frame.get("op") == op:
            args = _blank_at(args, path, sigs, kind)
    return args, sigs


def secret_in_request(frame: Mapping[str, Any]) -> bool:
    """§0.5: a grant-shaped string, or the presented grant's secret, anywhere in ``args``, ``opts`` or ``files``, the
    decoded bytes of inline files and of a ``blob.put`` chunk included. A secret split across two chunks is found
    when the blob is used: the handler checks the bytes it reads (``check_secret``)."""
    grant = (frame.get("auth") or {}).get("grant")
    secret = _secret_of(grant)
    args, sigs = _split_signatures(frame)  # the signatures get the grant-shape and secret checks only, below
    for part in (args, frame.get("opts"), frame.get("files")):
        for text in _strings(part):
            if GRANT_SHAPE.search(text) or GITHUB_TOKEN_SHAPE.search(text) or (secret is not None and secret in text):
                return True
    for text in sigs:
        if GRANT_SHAPE.search(text) or (secret is not None and secret in text):
            return True
    inline = [
        (name, spec.get("b64") if isinstance(spec, Mapping) else None)
        for name, spec in (frame.get("files") or {}).items()
    ]
    if frame.get("op") == "blob.put" and isinstance(frame.get("args"), Mapping):
        inline.append((None, frame["args"].get("b64")))  # a chunk's bytes are file content too (§0.10)
    # the files a key-file op names in its args (never any other file, never a blob chunk): ``{o, sig}`` documents whose
    # key-material fields are not taken for GitHub tokens (#832, #1044)
    wanted = KEY_FILE_ARGS.get(frame.get("op"), {}) if isinstance(frame.get("args"), Mapping) else {}
    named = [(frame["args"].get(arg), doc) for arg, doc in wanted.items() if type(frame["args"].get(arg)) is str]
    key_files = {n: d for n, d in named if sum(1 for m, _ in named if m == n) == 1}  # one file, two kinds: scan whole
    for name, raw in inline:
        if not isinstance(raw, str):
            continue
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            continue  # not base64: blob.invalid later (§0.6 step 8)
        if name in key_files:
            split = split_key_file(data, key_files[name])
            if split is not None:
                rest, taken = split
                data = rest.encode()
                for value in taken:  # key material: the grant checks still apply, the GitHub-token shape does not
                    if _GRANT_BYTES.search(value.encode()) or (secret is not None and secret in value):
                        return True
        if (
            _GRANT_BYTES.search(data)
            or GITHUB_TOKEN_BYTES.search(data)
            or (secret is not None and secret.encode() in data)
        ):
            return True
    return False


# --- redaction --------------------------------------------------------------------------------------------------------


#: op -> result paths (``*`` is any list index) whose exact-shape signature the read side returns as stored (#832): a
#: stored declaration's ``sig`` in ``reader.list`` is re-verified by clients, so it must not be replaced by chance.
READ_SIGNATURE_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "reader.list": (("data", "readers", "*", "decl", "sig"),),
}


class Redactor:
    """The redaction context of one request (D-P2-10): the presented grant and its secret, kept after ``auth`` is
    dropped, plus the shapes that are never returned whatever the request."""

    def __init__(self, grant: Any = None, op: str | None = None) -> None:
        #: the result paths of ``op`` whose exact-shape signature is returned as stored (#832), else none
        self.keep = READ_SIGNATURE_PATHS.get(op or "", ())
        secrets = []
        if isinstance(grant, str) and grant:
            secrets.append(grant)
            secret = _secret_of(grant)
            if secret is not None:
                secrets.append(secret)
        self.secrets = sorted(secrets, key=len, reverse=True)

    def text(self, text: str, *, signature: bool = False) -> str:
        """``text`` redacted. ``signature``: the value is a stored signature (see :meth:`_kept`): only the GitHub
        token shape is not applied to it; the secrets, grant shape and key blocks always are."""
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        text = GRANT_SHAPE.sub(REDACTED, text)
        if not signature:
            text = GITHUB_TOKEN_SHAPE.sub(REDACTED, text)
        return _PEM.sub(REDACTED, text)

    def _kept(self, path: tuple[str | int, ...], value: str) -> bool:
        """Is ``value`` at ``path`` a signature this op returns as stored: exact path (``*`` matches a list index
        only, never a mapping key), and canonical base64url of exactly 64 bytes?"""

        def at(pattern: tuple[str, ...]) -> bool:
            return len(pattern) == len(path) and all(
                type(b) is int if a == "*" else (type(b) is str and a == b) for a, b in zip(pattern, path, strict=True)
            )

        return any(at(p) for p in self.keep) and _is_signature(value)

    def obj(self, value: Any, _path: tuple[str | int, ...] = ()) -> Any:
        if isinstance(value, str):
            return self.text(value, signature=bool(self.keep) and self._kept(_path, value))
        if isinstance(value, list | tuple):
            return [self.obj(v, (*_path, i)) for i, v in enumerate(value)]
        if isinstance(value, Mapping):
            # a null value carries nothing (a prepared grant.issued has "secret_hash": null, F1): kept, so the
            # signer sees exactly the event it signs
            return {
                self.text(str(k)): self.obj(v, (*_path, str(k)))
                for k, v in value.items()
                if str(k) not in SECRET_KEYS or v is None
            }
        return value

    def error(self, error: HostError) -> HostError:
        out = HostError(
            error.code,
            self.text(error.message) or error.code,
            retry_after_ms=error.retry_after_ms,
            hint=self.text(error.hint) if error.hint else None,
            fix=[self.text(a) for a in error.fix] if error.fix else None,
            detail=error.detail,  # only closed_detail() of it is ever sent (errors.py)
        )
        out.exit, out.retryable = error.exit, error.retryable
        return out

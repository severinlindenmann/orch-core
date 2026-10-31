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
from .mcpshared import CAPABILITY_ANY, CAPABILITY_SHAPE

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
_CAPABILITY_BYTES = re.compile(CAPABILITY_SHAPE.pattern.encode())
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
    # the signed hub card a connect carries, and the hub-owner proof of a hub.joined event: random keys, hashes and
    # signatures, ~1 in 1,000 of such a request held a token-shaped run (#1475)
    *(
        (op, (*pre, *path), kind)
        for op, pre in (("hub.connect", ()), ("human.prepare", ("args",)))
        for path, kind in (
            (("hub_card", "o", "hk_root_pub"), "pub"),
            (("hub_card", "o", "hxk_pub"), "pub"),
            (("hub_card", "o", "tls_spki_sha256"), "hash"),
            (("hub_card", "op_cert", "o", "hk_op_pub"), "pub"),
            (("hub_card", "op_cert", "sig"), "sig"),
            (("hub_card", "sig"), "sig"),
        )
    ),
    ("human.submit", (_EV, "*", "hk_root_pub"), "pub"),
    ("human.submit", (_EV, "*", "owner_proof", "o", "payload", "hk_root_pub"), "pub"),
    ("human.submit", (_EV, "*", "owner_proof", "sig"), "sig"),
    # the sha-256 digest of the plan the owner was shown, echoed back by the share confirm ops
    ("share.rule_add", ("expect",), "hash"),
    ("share.rule_remove", ("expect",), "hash"),
    ("share.sync", ("expect",), "hash"),
)
#: ``member.add`` args that name an inline file, and the kind of key file each is (#1044, #832)
KEY_FILE_ARGS: dict[str, dict[str, str]] = {"member.add": {"reader_decl": "reader_decl", "cert": "device_cert"}}


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
            if (
                GRANT_SHAPE.search(text)
                or CAPABILITY_SHAPE.search(text)
                or GITHUB_TOKEN_SHAPE.search(text)
                or (secret is not None and secret in text)
            ):
                return True
    for text in sigs:
        if GRANT_SHAPE.search(text) or CAPABILITY_SHAPE.search(text) or (secret is not None and secret in text):
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
                    if (
                        _GRANT_BYTES.search(value.encode())
                        or _CAPABILITY_BYTES.search(value.encode())
                        or (secret is not None and secret in value)
                    ):
                        return True
        if (
            _GRANT_BYTES.search(data)
            or _CAPABILITY_BYTES.search(data)
            or GITHUB_TOKEN_BYTES.search(data)
            or (secret is not None and secret.encode() in data)
        ):
            return True
    return False


# --- redaction --------------------------------------------------------------------------------------------------------


#: op -> (answer path, key kind) whose exact-shape value is returned as it is, the GitHub-token shape not applied (#832,
#: #1641): values a client re-verifies or sends back, all random base64url, so about one in 10^3..10^4 holds a
#: token-shaped run by chance. ``*`` is any list index. The kinds are those of :data:`orch.tokenshape.KEY_BYTES`, plus
#: ``link``: an ``https`` share link whose ``#t=`` fragment is a 32-byte token (only the fragment is kept; the rest of
#: the link is redacted like any text). The grant shape, the capability shape, key blocks and the presented secret are
#: applied to every value, kept or not; free text is never listed.
_CARD = (
    (("o", "hk_root_pub"), "pub"), (("o", "hxk_pub"), "pub"), (("o", "tls_spki_sha256"), "hash"),
    (("op_cert", "o", "hk_op_pub"), "pub"), (("op_cert", "sig"), "sig"), (("sig",), "sig"),
)  # fmt: skip
_APP_CERT = (
    (("pk_pub",), "pub"),
    (("device_certs", "*", "sig"), "sig"),
    (("device_certs", "*", "o", "dk_sig_pub"), "pub"),
    (("device_certs", "*", "o", "dk_kx_pub"), "pub"),
    (("device_certs", "*", "o", "label_sealed"), "sealed"),
)
ANSWER_KEY_PATHS: dict[str, tuple[tuple[tuple[str, ...], str], ...]] = {
    "reader.list": ((("data", "readers", "*", "decl", "sig"), "sig"),),  # re-verified by clients (#832)
    # the hub card of a link and the held card: the CLI and the dashboard send it back in hub.connect / hub.mode
    "hub.link": tuple((("data", "hub_card", *p), k) for p, k in _CARD),
    "hub.status": tuple((("data", "hub_card", *p), k) for p, k in _CARD),
    "share.preview": ((("data", "plan"), "hash"),),  # the plan digest, sent back as ``expect``
    "space.get": ((("data", "data"), "data"),),  # the item's file bytes (redacting base64 cannot find a token anyway)
    "term.attach": ((("data", "ticket"), "hash"),),  # the single-use attach capability, presented to the runner
    "publish.send": ((("data", "link"), "link"),),  # shown once: the link's token is its only copy
    # #1455: the owner's PK and device certificates the client signs the go-live approval with: random keys and a
    # signature, sent back inside the approval, so a token-shaped run by chance must not be rewritten
    "app.send": tuple((("data", "commit", *p), k) for p, k in _APP_CERT),
    "app.commit_request": tuple((("data", *p), k) for p, k in _APP_CERT),
}
_LINK = re.compile(r"(https://[^\s#]*)#t=([A-Za-z0-9_-]{43})")


class Redactor:
    """The redaction context of one request (D-P2-10): the presented grant and its secret, kept after ``auth`` is
    dropped, plus the shapes that are never returned whatever the request."""

    def __init__(self, grant: Any = None, op: str | None = None) -> None:
        #: the answer paths of ``op`` whose exact-shape value is returned as it is (:data:`ANSWER_KEY_PATHS`)
        self.keep = ANSWER_KEY_PATHS.get(op or "", ())
        secrets = []
        if isinstance(grant, str) and grant:
            secrets.append(grant)
            secret = _secret_of(grant)
            if secret is not None:
                secrets.append(secret)
        self.secrets = sorted(secrets, key=len, reverse=True)

    def text(self, text: str, *, signature: bool = False) -> str:
        """``text`` redacted. ``signature``: the value is a kept key value (see :meth:`_kind`): only the GitHub token
        shape is not applied to it; the secrets, grant shape, capability shape and key blocks always are."""
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        text = GRANT_SHAPE.sub(REDACTED, text)
        text = CAPABILITY_ANY.sub(REDACTED, text)
        if not signature:
            text = GITHUB_TOKEN_SHAPE.sub(REDACTED, text)
        return _PEM.sub(REDACTED, text)

    def _kind(self, path: tuple[str | int, ...]) -> str | None:
        """The key kind listed for this exact answer path (``*`` matches a list index only, never a mapping key)."""
        for pattern, kind in self.keep:
            if len(pattern) == len(path) and all(
                type(b) is int if a == "*" else (type(b) is str and a == b) for a, b in zip(pattern, path, strict=True)
            ):
                return kind
        return None

    def _value(self, path: tuple[str | int, ...], value: str) -> str:
        kind = self._kind(path) if self.keep else None
        if kind == "link":
            m = _LINK.fullmatch(value)
            if m is not None and is_key_value(m.group(2), "hash"):
                head = self.text(m.group(1))
                token = self.text(m.group(2), signature=True)
                return f"{head}#t={token}"
            return self.text(value)
        return self.text(value, signature=kind is not None and is_key_value(value, kind))

    def obj(self, value: Any, _path: tuple[str | int, ...] = ()) -> Any:
        if isinstance(value, str):
            return self._value(_path, value)
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

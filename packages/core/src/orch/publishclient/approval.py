"""The Publish add-on's rules as pure functions: what may be shared, and the exact words a person signs (P5).

No I/O, no key, no host state. The host (:mod:`orch.host.publishing`) and the signer's plain view
(:mod:`orch.host.signcontent`) both import this, so the rule is written once.

**What the person signs.** Sharing is approved by a person's signature over a ``log.added`` note on the ticket whose
text :func:`approval_text` builds from the staged copy: the ticket, who prepared it, the kind of link, how long it
lives, how many views, **which server the files go to**, **where the link opens**, and every file with its size and
hash. The staged copy's ``manifest`` hash is in the text too. At send time the host builds the text again from the
staged copy it is about to upload and requires a person's signed note with exactly these words: any change of file,
server, kind, days or views makes the text differ, so the old signature covers nothing else (WYSIWYS).

A file name is plain on purpose (:data:`NAME`): the same alphabet orch-publish accepts on the server (design I8), so
what is shown is what is stored and no name can fake a second line in the review.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "APP_FILES_SHOWN",
    "APP_MARK",
    "APP_OPS",
    "DAYS",
    "AppReleaseFacts",
    "ApprovalFacts",
    "KINDS",
    "LIMITS",
    "MARK",
    "NAME",
    "VIEWS",
    "RESERVED_LINES",
    "app_release_text",
    "approval_text",
    "check_name",
    "reserved_note_op",
    "human_size",
    "manifest_hash",
    "password_record",
]

#: the first words of every approval note; the signer's plain view keys on them
MARK = "Publish approval: share files outside this computer"
KINDS = ("secret", "once")
DAYS = (1, 3, 7, 30)
VIEWS = (1, 3, 10)
#: the server accepts these per share (design I14); the host refuses earlier, with plain words
LIMITS = {"files": 20, "file_bytes": 25 * 1024 * 1024, "share_bytes": 50 * 1024 * 1024}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")
_STAGED = re.compile(r"ps_[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_ORIGIN = re.compile(r"https?://[a-z0-9.:\-\[\]]{1,300}")


@dataclass(frozen=True)
class ApprovalFacts:
    """What the host hands ``human.prepare`` for ``publish.share``: built from the staged copy, never from the request
    (which names only ``staged_id``)."""

    staged_id: str
    ticket_key: str
    text: str


def check_name(name: object) -> str:
    """A file name the server will take (its own alphabet, no leading dot, no ``..``), else ``ValueError``."""
    if type(name) is not str or NAME.fullmatch(name) is None or ".." in name or name != name.strip():
        raise ValueError("file names use letters, digits, space, dot, dash and underscore only (up to 100 characters)")
    return name


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def manifest_hash(
    workspace_id: str,
    ticket_uid: str,
    kind: str,
    days: int,
    views: int,
    origin: str,
    files: Sequence[Mapping[str, Any]],
    server: str,
    share: str,
) -> str:
    """``sha256:`` + hex over the canonical JSON of everything the staged copy consists of, the server included."""
    doc = {
        "ws": workspace_id,
        "ticket": ticket_uid,
        "kind": kind,
        "days": days,
        "views": views,
        "origin": origin,
        "server": server,
        "share": share,
        "files": [{"name": f["name"], "size": f["size"], "sha256": f["sha256"]} for f in files],
    }
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def approval_text(
    *,
    staged_id: str,
    ticket_key: str,
    origin: str,
    kind: str,
    days: int,
    views: int,
    files: Sequence[Mapping[str, Any]],
    server: str,
    share: str,
    manifest: str,
) -> str:
    """The note a person signs to let one staged copy leave this computer. Deterministic; every value is checked first
    (anything outside its alphabet raises ``ValueError``, so the text can never hold a line break from outside)."""
    if _STAGED.fullmatch(staged_id or "") is None or origin not in ("agent", "person"):
        raise ValueError("staged id or origin")
    if kind not in KINDS or days not in DAYS or (kind == "once") != (views in VIEWS) or (kind == "secret" and views):
        raise ValueError("kind, days or views")
    if not 1 <= len(files) <= LIMITS["files"] or _HEX64.fullmatch(manifest.removeprefix("sha256:")) is None:
        raise ValueError("files or manifest")
    if _ORIGIN.fullmatch(server) is None or _ORIGIN.fullmatch(share) is None:
        raise ValueError("server")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", ticket_key) is None:
        raise ValueError("ticket key")
    total = 0
    rows = []
    for f in files:
        name = check_name(f["name"])
        size, digest = f["size"], f["sha256"]
        if type(size) is not int or size < 0 or _HEX64.fullmatch(digest) is None:
            raise ValueError("file size or hash")
        total += size
        rows.append(f"- {name}  ({human_size(size)}, sha256 {digest[:16]})")
    if kind == "secret":
        link = "a secret link: anyone who has the link can open the files"
    else:
        link = f"a show-once link: it opens at most {views} time{'' if views == 1 else 's'}"
    who = "an agent" if origin == "agent" else "you"
    lines = [
        MARK,
        f"Ticket: {ticket_key}",
        f"Prepared by: {who}",
        f"Link: {link}",
        f"Works for: {days} day{'' if days == 1 else 's'} from the moment the link is made",
        f"The files are sent to: {server}",
        f"The link opens at: {share}",
        f"Files ({len(files)}, {human_size(total)}):",
        *rows,
        f"Staged copy: {staged_id}",
        f"Manifest: {manifest}",
    ]
    return "\n".join(lines)


#: the password record (contracts/hub/protocol.md 11.4.1, C5): exactly these scrypt parameters, 16-byte salt, 32-byte
#: key
PW_N, PW_R, PW_P, PW_SALT, PW_DK = 32768, 8, 1, 16, 32
PW_INPUT_MAX = 256


def password_record(password: str, salt: bytes) -> dict[str, Any]:
    """The C5 record of ``password`` (the host draws ``salt``: 16 random bytes). Pure; orch-publish never sees the
    password, only this record, and it refuses any other parameters."""
    import base64

    if type(password) is not str or not password or len(password.encode("utf-8")) > PW_INPUT_MAX:
        raise ValueError("password")
    if type(salt) is not bytes or len(salt) != PW_SALT:
        raise ValueError("salt")
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=PW_N, r=PW_R, p=PW_P, dklen=PW_DK, maxmem=128 * PW_N * PW_R * 2
    )

    def enc(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")

    return {"alg": "scrypt", "n": PW_N, "r": PW_R, "p": PW_P, "salt": enc(salt), "dk": enc(dk)}


# -- app releases (#1457, HUB-PU-07): publishing or updating an app needs a PERSON's one-click signature ---------------
# An app runs code in other people's browsers, so an agent can prepare a release but never make one live. The person
# signs a ``log.added`` note whose words :func:`app_release_text` builds from the staged release (never from the
# request): the action, the name, who may open it, what the app may contact, the files, what changed since the live
# version, and the hashes that bind all of it (``meta_hash`` covers the whole stored meta, ``reach_hash`` the reach).
# The host builds the words again at send time and sends only when a person's note with exactly these words exists
# (:func:`orch.host.appops.signed_release`), so a changed file, access or reach makes the old signature cover nothing.

APP_MARK = "App approval: let an app go live"
#: the three requests that make code live at orch-publish (``commit``, ``rollback``, ``start``)
APP_OPS = ("commit", "rollback", "start")
_APP_ID = re.compile(r"[0-9a-f]{32}")
_APP_STAGED = re.compile(r"pa_[0-9a-f]{32}")
_TICKET_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: the review lists this many files; the rest are counted, and every one is bound by ``meta_hash``
APP_FILES_SHOWN = 25
_ACCESS_WORDS = {
    "public": "anyone with the link, no sign-in",
    "password": "anyone with the link who knows the password you set",
}
_ACTION_WORDS = {
    "commit": "make this version live",
    "rollback": "go back to this older version",
    "start": "start this app again",
}


@dataclass(frozen=True)
class AppReleaseFacts:
    """What the host hands ``human.prepare`` for ``app.release``: built from the staged release, never from the request
    (which names only ``staged_id``)."""

    staged_id: str
    ticket_key: str
    text: str


def _who_can_open(access: str, audience: str | None, origin: str | None = None) -> str:
    if access == "password":
        by = "set by an agent" if origin == "agent" else "set by you"
        return f"anyone with the link who knows a password ({by})"
    if access in _ACCESS_WORDS:
        return _ACCESS_WORDS[access]
    if access == "company":
        return "everyone signed in to your company hub"
    assert audience is not None
    if audience.startswith("group:"):
        return f"the hub group {audience.removeprefix('group:')}"
    return "the people of one workspace (" + audience.removeprefix("ws:")[:8] + ")"


def _reach_words(reach: Sequence[str]) -> str:
    return ", ".join(reach) if reach else "nothing outside the app itself"


#: files that run in the browser come first in the review and are always all listed (an agent cannot hide a service
#: worker or a wasm module behind padding); images and the rest are counted, and only they can be cut
_CODE_EXT = ("html", "js", "mjs", "cjs", "wasm", "css", "svg", "htm")
_IMAGE_EXT = ("png", "jpg", "jpeg", "gif", "webp", "avif", "ico", "bmp")
_QUOTES = '"\u201c\u201d'


def _ext(path: str) -> str:
    return path.rpartition(".")[2].lower() if "." in path else ""


def _file_order(f: Mapping[str, Any]) -> tuple[int, str]:
    ext = _ext(f["path"])
    rank = {e: i for i, e in enumerate(("html", "htm", "js", "mjs", "cjs", "wasm", "css", "svg"))}
    return (rank.get(ext, len(rank)), f["path"])


def _file_kind(path: str) -> str:
    ext = _ext(path)
    return "code" if ext in _CODE_EXT else "image" if ext in _IMAGE_EXT else "other"


def _counts(files: Sequence[Mapping[str, Any]]) -> str:
    n = {"code": 0, "image": 0, "other": 0}
    for f in files:
        n[_file_kind(f["path"])] += 1
    words = {"code": "code file", "image": "image", "other": "other file"}
    return ", ".join(f"{n[k]} {words[k]}{'' if n[k] == 1 else 's'}" for k in ("code", "image", "other") if n[k])


def _mixed_letters(name: str) -> bool:
    """True when the letters of ``name`` come from more than one script (a Cyrillic ``a`` in a Latin word)."""
    import unicodedata

    scripts = set()
    for c in name:
        if c.isalpha():
            scripts.add(unicodedata.name(c, "UNKNOWN").split(" ")[0])
    return len(scripts) > 1


def _changes(prev: Mapping[str, Any], meta: Mapping[str, Any], origin: str | None = None) -> list[str]:
    """Plain lines for what changes against the live version; ``[]`` when nothing differs."""
    out: list[str] = []
    if (prev["access"], prev["audience"]) != (meta["access"], meta["audience"]):
        out.append(
            f"- Who can open it: was {_who_can_open(prev['access'], prev['audience'], origin)}; "
            f"now {_who_can_open(meta['access'], meta['audience'], origin)}"
        )
    elif meta["access"] == "password" and prev["pw"] != meta["pw"]:
        out.append("- Password: changed")
    added = [o for o in meta["reach"] if o not in prev["reach"]]
    dropped = [o for o in prev["reach"] if o not in meta["reach"]]
    if added:
        out.append("- The app may now also contact: " + ", ".join(added))
    if dropped:
        out.append("- The app may no longer contact: " + ", ".join(dropped))
    old = {f["path"]: f["sha256"] for f in prev["files"]}
    new = {f["path"]: f["sha256"] for f in meta["files"]}
    n_add = sum(p not in old for p in new)
    n_gone = sum(p not in new for p in old)
    n_chg = sum(p in old and old[p] != h for p, h in new.items())
    if n_add or n_gone or n_chg:
        out.append(f"- Files: {n_add} added, {n_chg} changed, {n_gone} removed")
    return out


def app_release_text(
    *,
    staged_id: str,
    ticket_key: str,
    origin: str,
    op: str,
    app_id: str,
    version: int,
    meta: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    server: str,
    share: str,
) -> str:
    """The note a person signs to let one app version go live. Deterministic, built from the staged release only.
    ``meta`` is the stored meta of the version (the shape orch-publish checks, ``hubclient.published.meta_problem``);
    ``previous`` is ``{"version": int, "meta": <meta of the live version>}``: None for a first release, required for a
    rollback (to a version below the live one) and a start (the live version itself). Every value is checked first:
    anything outside its alphabet raises ``ValueError``, so no line break from outside can reach the text and what is
    shown is what is stored."""
    from orch.hubclient import published as pub

    if _APP_STAGED.fullmatch(staged_id or "") is None or origin not in ("agent", "person"):
        raise ValueError("staged id or origin")
    if op not in APP_OPS or _APP_ID.fullmatch(app_id or "") is None:
        raise ValueError("op or app id")
    if type(version) is not int or not 1 <= version <= 2**31 - 1:
        raise ValueError("version")
    if _TICKET_KEY.fullmatch(ticket_key or "") is None:
        raise ValueError("ticket key")
    if _ORIGIN.fullmatch(server) is None or _ORIGIN.fullmatch(share) is None:
        raise ValueError("server")
    if (bad := pub.meta_problem(dict(meta))) is not None:
        raise ValueError(f"app meta: {bad}")
    if any(q in meta["name"] for q in _QUOTES):
        raise ValueError("app name")
    live_version = None
    if previous is not None:
        live_version = previous["version"]
        if pub.meta_problem(dict(previous["meta"])) is not None or type(live_version) is not int or live_version < 1:
            raise ValueError("live version")
    # which version a request may name against the live one (what orch-publish also enforces)
    if op == "commit" and live_version is not None and live_version >= version:
        raise ValueError("live version")
    if op == "rollback" and (live_version is None or live_version <= version):
        raise ValueError("live version")
    if op == "start" and (live_version is None or live_version != version):
        raise ValueError("live version")
    files = sorted(meta["files"], key=_file_order)
    total = sum(f["size"] for f in files)
    code = [f for f in files if _file_kind(f["path"]) == "code"]
    rest = [f for f in files if _file_kind(f["path"]) != "code"]

    def row(f: Mapping[str, Any]) -> str:
        return f"- {f['path']}  ({human_size(f['size'])}, sha256 {f['sha256'][:16]})"

    rows = [row(f) for f in code] + [row(f) for f in rest[:APP_FILES_SHOWN]]
    if len(rest) > APP_FILES_SHOWN:
        rows.append(f"- and {len(rest) - APP_FILES_SHOWN} more files that are not code (covered by the hash below)")
    who = "an agent" if origin == "agent" else "you"
    mixed = " (mixed letters: check the name carefully)" if _mixed_letters(meta["name"]) else ""
    lines = [
        APP_MARK,
        f"Ticket: {ticket_key}",
        f"Prepared by: {who}",
        f"Action: {_ACTION_WORDS[op]}",
        f"App name: \u201c{meta['name']}\u201d{mixed}",
        f"Kind: {meta['kind']}",
        f"Address: {share}/a/{meta['slug']}",
        f"Version: {version}",
        f"Who can open it: {_who_can_open(meta['access'], meta['audience'], origin)}",
        "Code in the app runs in the browser of everyone who opens it",
        f"The app may contact: {_reach_words(meta['reach'])}",
        f"The files are sent to: {server}",
        f"Files ({len(files)}, {human_size(total)}): {_counts(files)}",
        *rows,
    ]
    if previous is not None:
        changes = _changes(previous["meta"], meta, origin)
        lines.append(f"Compared with the live version {live_version}:")
        lines.extend(changes or ["- no change to who can open it, what it may contact or the files"])
    else:
        lines.append("Compared with the live version: this is the first version")
    lines += [
        f"Staged release: {staged_id}",
        f"App id: {app_id}",
        f"App hash: {pub.meta_hash(meta)}",
        f"Reach hash: {pub.reach_hash(meta['reach'])}",
    ]
    return "\n".join(lines)


#: Reserved approval lines are written only by their own click op. A note that starts with one of these first
#: lines is an approval a sender will trust (``publish.send``, ``app.send``), so no other op may write such a note:
#: the signer (``host.signer._person_op``) refuses it for every other person op, and the ordinary note op
#: (``ops.plans.log``) refuses it for everyone, an agent included. One table, so a new approval kind is one row,
#: not one more patched path.
RESERVED_LINES: dict[str, str] = {MARK: "publish.share", APP_MARK: "app.release"}


def reserved_note_op(text: object) -> str | None:
    """The op that alone may write a note with this text (it starts with a reserved approval line), else None."""
    if type(text) is not str:
        return None
    for line, op in RESERVED_LINES.items():
        if text.startswith(line):
            return op
    return None

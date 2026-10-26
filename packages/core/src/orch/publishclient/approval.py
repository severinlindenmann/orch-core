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
    "DAYS",
    "ApprovalFacts",
    "KINDS",
    "LIMITS",
    "MARK",
    "NAME",
    "VIEWS",
    "approval_text",
    "check_name",
    "human_size",
    "manifest_hash",
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

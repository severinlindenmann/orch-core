"""The one place that decides what CI may sign for a channel: ``trusted_current``.

INVARIANT: CI signs a channel manifest only if
 (a) its source envelope verifies at NOW (never at the payload's own issued_at): the certificate is valid now and not
     revoked by the repository's current list. An expired manifest is the one thing accepted, because expiry is the
     reason to refresh; its signature, certificate and revocation status are still checked at now;
 (b) its seq is >= the seq of the verified current channel pointer (refresh: exactly equal; it re-signs the current
     pointer's version, seq and commit and nothing else);
 (c) every failure except "this channel has never been published" is a ``Refused`` (exit 1). Only a channel with
     neither a channel.json nor any version release is ``NothingToRefresh`` (exit 3).
Used by refresh.py (weekly re-sign) and channel_seq.py (publish: may the pointer move?). The published channel.json
is writable by anyone with repository access, so it only counts after it verifies; the immutable per-version release
of exactly the version it names must verify too and agree with it.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
TAG = re.compile(r"v(\d+\.\d+\.\d+)(?:\.dev(\d+))?")  # 2.0.0 (main) or 2.0.0.dev<seq> (test channels)
TIMES = ("issued_at", "expires_at")

# verify(envelope_bytes, channel, now, allow_expired) -> the verified manifest; raises Refused.
Verifier = Callable[[bytes, str, datetime, bool], dict[str, Any]]


class Refused(Exception):
    """Anything that must stop the job (exit 1)."""


class NothingToRefresh(Exception):
    """The channel has never been published (exit 3)."""


def jcs(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def parse_time(value: object) -> datetime:
    if not isinstance(value, str) or TIME.fullmatch(value) is None:
        raise Refused("bad time value")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def core_verifier() -> Verifier:
    """orch.update.verify (U2) against the embedded roots. Fails closed if it is not importable."""
    try:
        from orch.update.verify import UpdateRefused, verify_envelope
    except ImportError as exc:  # pragma: no cover - depends on the installed package
        raise Refused(f"the core verifier is not available: {exc}") from None

    def verify(envelope: bytes, channel: str, now: datetime, allow_expired: bool) -> dict[str, Any]:
        # allow_expired skips ONLY the manifest expires_at rule inside the verifier; every other rule still runs and
        # the returned document is the verifier's own. A refusal is never turned into a payload.
        try:
            return dict(
                verify_envelope(
                    envelope,
                    purpose="core",
                    channel=channel,
                    now=now,
                    seq_seen=None,
                    revocations_issued_at=None,
                    allow_expired_manifest=allow_expired,
                ).document
            )
        except UpdateRefused as exc:
            raise Refused(f"verification refused: {exc.reason}") from None

    return verify


def verify_published(
    raw: bytes,
    channel: str,
    revocations: dict[str, Any],
    verify: Verifier,
    now: datetime,
    *,
    allow_expired: bool,
) -> dict[str, Any]:
    """Verify a published envelope at ``now`` against the repository's CURRENT revocation list."""
    try:
        envelope = json.loads(raw)
        doc = verify(jcs({**envelope, "revocations": revocations}), channel, now, allow_expired)
        if parse_time(doc["issued_at"]) > now:
            raise Refused("issued_at is in the future")
        return doc
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise Refused(f"unreadable envelope: {type(exc).__name__}") from None


@dataclass(frozen=True)
class Trusted:
    doc: dict[str, Any]  # the verified manifest of the current pointer
    tag: str

    @property
    def seq(self) -> int:
        return int(self.doc["seq"])


def _same_but_times(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return {k: v for k, v in a.items() if k not in TIMES} == {k: v for k, v in b.items() if k not in TIMES}


def trusted_current(
    channel: str,
    revocations: dict[str, Any],
    verify: Verifier,
    now: datetime,
    *,
    get_pointer: Callable[[], bytes | None],
    get_release: Callable[[str], bytes | None],
    tag_commit: Callable[[str], str],
    releases_for_channel: Callable[[], list[str]],
) -> Trusted:
    try:
        return _trusted_current(
            channel, revocations, verify, now, get_pointer, get_release, tag_commit, releases_for_channel
        )
    except (KeyError, TypeError, IndexError, ValueError, OSError) as exc:
        raise Refused(f"unexpected {type(exc).__name__}") from None


def _claimed_channel(raw: bytes) -> str:
    """The channel an envelope CLAIMS (unverified); it is only used to pick the verifier's channel argument, and the
    verified document's own channel is what counts afterwards."""
    try:
        c = json.loads(base64.b64decode(json.loads(raw)["payload"]))["channel"]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise Refused(f"unreadable release: {type(exc).__name__}") from None
    if not isinstance(c, str):
        raise Refused("unreadable release channel")
    return c


def _anchors(channel, tags, extra, revocations, verify, now, get_release):  # type: ignore[no-untyped-def]
    """``{tag: verified doc}`` of every release that belongs to ``channel``. Each tag is verified under the channel its
    release CLAIMS; the signature proves where it belongs, and a verified release of another channel is skipped. A tag
    whose release is missing, or does not verify, is a Refused (fail closed). One exception, documented: for ``main``
    a ``.dev`` tag that claims a ``test-*`` channel is skipped unverified, because the production verifier refuses
    test channels by design, so such a release could never verify there (and never anchors main)."""
    out: dict[str, dict[str, Any]] = {}
    for t in sorted({*tags, *extra}):
        raw = get_release(t)
        if raw is None:
            raise Refused(f"{t}: a git tag exists but its immutable release is missing or was retitled/deleted")
        claimed = _claimed_channel(raw)
        if channel == "main" and ".dev" in t and claimed.startswith("test-"):
            continue
        rdoc = verify_published(raw, claimed, revocations, verify, now, allow_expired=True)
        if f"v{rdoc['version']}" != t:
            raise Refused(f"{t}: the release names another version")
        if rdoc["channel"] == channel:
            out[t] = rdoc
    return out


def _trusted_current(channel, revocations, verify, now, get_pointer, get_release, tag_commit, releases_for_channel):  # type: ignore[no-untyped-def]
    pointer = get_pointer()
    if pointer is None:
        if _anchors(channel, releases_for_channel(), (), revocations, verify, now, get_release):
            raise Refused(f"channel-{channel} has version releases but no channel.json")
        raise NothingToRefresh(channel)
    doc = verify_published(pointer, channel, revocations, verify, now, allow_expired=True)
    tag = f"v{doc['version']}"
    m = TAG.fullmatch(tag)
    if m is None or (m.group(2) is not None and doc["seq"] != int(m.group(2))):
        raise Refused(f"{tag}: version and seq do not agree")
    if doc["commit"] != tag_commit(tag):
        raise Refused(f"{tag}: commit does not match the git tag")
    # Anchor in immutable data: the pointer is mutable and never picks the release. Every immutable version release of
    # the channel must verify (fail closed), and the pointer must name the one with the highest seq.
    anchors = _anchors(channel, releases_for_channel(), (tag,), revocations, verify, now, get_release)
    if tag not in anchors:
        raise Refused(f"{tag}: the pointer's release does not belong to channel {channel}")
    best_tag, best = tag, anchors[tag]
    for t, rdoc in anchors.items():
        if rdoc["seq"] > best["seq"]:
            best_tag, best = t, rdoc
    if best_tag != tag or best["seq"] != doc["seq"]:
        raise Refused(
            f"channel pointer is {tag} (seq {doc['seq']}) but the highest verified release is {best_tag}: rollback?"
        )
    if not _same_but_times(best, doc):
        raise Refused(f"{tag}: the release and the channel pointer disagree")
    return Trusted(doc=best, tag=best_tag)


def refresh_payload(t: Trusted, issued_at: str, expires_at: str) -> bytes:
    """Refresh re-signs exactly the current pointer's version, seq and commit: only the times change."""
    if not parse_time(issued_at) < parse_time(expires_at):
        raise Refused("expires_at must be after issued_at")
    return jcs({**t.doc, "issued_at": issued_at, "expires_at": expires_at})


def check_new_seq(t: Trusted | None, new_seq: int) -> None:
    if t is not None and new_seq < t.seq:
        raise Refused(f"seq {new_seq} is lower than the verified current seq {t.seq}: the pointer is not moved")


# --- real sources (used by the scripts' main; tests inject fakes) -----------------------------------------------


def http_pointer(url: str) -> Callable[[], bytes | None]:
    def get() -> bytes | None:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - https URL built by the workflow
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise Refused(f"cannot fetch channel.json (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise Refused(f"cannot fetch channel.json: {type(exc).__name__}") from None

    return get


def gh_release(tag: str) -> bytes | None:
    res = subprocess.run(
        ["gh", "release", "download", tag, "--pattern", "release.json", "--output", "-"],
        capture_output=True,
        check=False,
    )
    return res.stdout if res.returncode == 0 and res.stdout else None


def git_release_tags(repo: Path, exclude: str | None) -> Callable[[], list[str]]:
    """Every ``v*`` release tag of the checked-out repository (``fetch-depth: 0``). The ANCHOR list is git tags, not
    release titles: a tag cannot be hidden by retitling or deleting a release. Tag protection and immutable releases
    (REQUIRED, see release.yml.draft) make the tags themselves unmovable."""

    def get() -> list[str]:
        res = subprocess.run(
            ["git", "-C", str(repo), "tag", "-l", "v*"], capture_output=True, text=True, check=False
        )
        if res.returncode != 0:
            raise Refused("cannot list git tags")
        return [t for t in res.stdout.split() if TAG.fullmatch(t) and t != exclude]

    return get


def git_tag_commit(repo: Path) -> Callable[[str], str]:
    def get(tag: str) -> str:
        res = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise Refused(f"git tag {tag} not found")
        return res.stdout.strip()

    return get

#!/usr/bin/env python3
"""Rebuild the weekly-refresh payload from VERIFIED immutable releases, never from the mutable channel pointer.

    refresh.py --candidates DIR --channel develop --revocations release/keys/revocations.json \
        --issued-at T --expires-at T --out manifest.json [--repo .]

DIR holds ``<tag>/release.json`` (the envelope of each per-version release, downloaded by the workflow). Each
candidate for the channel must verify with the core verifier (embedded roots, the envelope's own certificate, and the
CURRENT root-signed revocation list from the repository, so a since-revoked key is refused). The newest verified
manifest is cross-checked against its git tag (version, seq from the dev number, commit). Only issued_at and
expires_at change; the output is signed again by sign.py. ``channel-<ch>/channel.json`` is never read: anyone with
write access could replace it, and the release key must not sign what they put there.

Exit codes: 0 payload written; 3 no release exists for the channel yet (nothing to refresh); 1 any refusal
(verify failure, mismatch with the tag, unreadable input). Every refusal fails the job.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
TAG = re.compile(r"v(\d+\.\d+\.\d+)\.dev(\d+)")
NOTHING = 3

# verify(envelope_bytes, channel, now) -> the verified manifest document; raises Refused.
Verifier = Callable[[bytes, str, datetime], dict[str, Any]]


class Refused(Exception):
    pass


def jcs(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def parse_time(value: object) -> datetime:
    if not isinstance(value, str) or TIME.fullmatch(value) is None:
        raise Refused("bad time value")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def core_verifier() -> Verifier:
    """The real verifier (orch.update.verify, U2) against the embedded roots. Fails closed if it is not importable."""
    try:
        from orch.update.verify import UpdateRefused, verify_envelope
    except ImportError as exc:  # pragma: no cover - depends on the installed package
        raise Refused(f"the core verifier is not available: {exc}") from None

    def verify(envelope: bytes, channel: str, now: datetime) -> dict[str, Any]:
        try:
            return dict(
                verify_envelope(
                    envelope, purpose="core", channel=channel, now=now, seq_seen=None, revocations_issued_at=None
                ).document
            )
        except UpdateRefused as exc:
            raise Refused(f"verification refused: {exc.reason}") from None

    return verify


def with_current_revocations(envelope: dict[str, Any], revocations: dict[str, Any]) -> bytes:
    """The envelope with the repository's current revocation list in place of the one it carried."""
    return jcs({**envelope, "revocations": revocations})


def verify_published(
    raw: bytes, channel: str, revocations: dict[str, Any], verify: Verifier, *, now: datetime | None = None
) -> dict[str, Any]:
    """Verify a published envelope as of its signing time, against the current revocations; returns the manifest."""
    try:
        envelope = json.loads(raw)
        payload = json.loads(base64.b64decode(envelope["payload"], validate=True))
        signed_at = parse_time(payload["issued_at"])
    except (ValueError, KeyError, TypeError) as exc:
        raise Refused(f"unreadable envelope: {type(exc).__name__}") from None
    if signed_at > (now or datetime.now(UTC)):
        raise Refused("issued_at is in the future")
    return verify(with_current_revocations(envelope, revocations), channel, signed_at)


def git_tag_commit(repo: Path, tag: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise Refused(f"git tag {tag} not found")
    return res.stdout.strip()


def rebuild_payload(
    candidates: Path,
    channel: str,
    revocations: dict[str, Any],
    issued_at: str,
    expires_at: str,
    verify: Verifier,
    tag_commit: Callable[[str], str],
) -> bytes:
    parse_time(issued_at)
    parse_time(expires_at)
    if not issued_at < expires_at:
        raise Refused("expires_at must be after issued_at")
    best: tuple[int, str, dict[str, Any]] | None = None
    for path in sorted(candidates.glob("*/release.json")):
        tag = path.parent.name
        raw = path.read_bytes()
        try:  # only to skip the other channel's releases; nothing from here is trusted
            peek = json.loads(base64.b64decode(json.loads(raw)["payload"], validate=True))
            if peek["channel"] != channel:
                continue
        except (ValueError, KeyError, TypeError):
            raise Refused(f"{tag}: unreadable release.json") from None
        doc = verify_published(raw, channel, revocations, verify)
        m = TAG.fullmatch(tag)
        if m is None or doc["version"] != tag[1:] or doc["seq"] != int(m.group(2)):
            raise Refused(f"{tag}: version or seq does not match the tag")
        if doc["commit"] != tag_commit(tag):
            raise Refused(f"{tag}: commit does not match the git tag")
        if best is None or doc["seq"] > best[0]:
            best = (doc["seq"], tag, doc)
    if best is None:
        raise LookupError(channel)
    return jcs({**best[2], "issued_at": issued_at, "expires_at": expires_at})


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candidates", type=Path, required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--revocations", type=Path, required=True)
    ap.add_argument("--issued-at", required=True)
    ap.add_argument("--expires-at", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--repo", type=Path, default=Path("."))
    a = ap.parse_args(argv)
    try:
        out = rebuild_payload(
            a.candidates,
            a.channel,
            json.loads(a.revocations.read_text(encoding="utf-8")),
            a.issued_at,
            a.expires_at,
            core_verifier(),
            lambda tag: git_tag_commit(a.repo, tag),
        )
    except LookupError:
        print(f"refresh: no release for channel {a.channel} yet; nothing to refresh", file=sys.stderr)
        return NOTHING
    except (Refused, OSError, ValueError) as exc:
        print(f"refresh: REFUSED: {exc}", file=sys.stderr)
        return 1
    a.out.write_bytes(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

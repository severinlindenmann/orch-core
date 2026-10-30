#!/usr/bin/env python3
"""Weekly re-sign payload: ``refresh.py --channel C --pointer-url U --revocations R --issued-at T --expires-at T
--out manifest.json [--repo .]``.

All trust decisions live in trusted.trusted_current (see its docstring). Refresh re-signs exactly the verified current
pointer's version, seq and commit; only issued_at and expires_at change. Exit 0 payload written; 3 the channel has
never been published; 1 every other failure (nothing is written, so nothing is signed).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trusted import (  # noqa: E402
    NothingToRefresh,
    Refused,
    core_verifier,
    gh_release,
    git_release_tags,
    git_tag_commit,
    http_pointer,
    refresh_payload,
    trusted_current,
)

NOTHING = 3


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--channel", required=True)
    ap.add_argument("--pointer-url", required=True)
    ap.add_argument("--revocations", type=Path, required=True)
    ap.add_argument("--issued-at", required=True)
    ap.add_argument("--expires-at", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--repo", type=Path, default=Path("."))
    a = ap.parse_args(argv)
    try:
        t = trusted_current(
            a.channel,
            json.loads(a.revocations.read_text(encoding="utf-8")),
            core_verifier(),
            datetime.now(UTC),
            get_pointer=http_pointer(a.pointer_url),
            get_release=gh_release,
            tag_commit=git_tag_commit(a.repo),
            releases_for_channel=git_release_tags(a.repo, None),
        )
        out = refresh_payload(t, a.issued_at, a.expires_at)
    except NothingToRefresh:
        print(f"refresh: channel {a.channel} has never been published; nothing to refresh", file=sys.stderr)
        return NOTHING
    except (Refused, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f"refresh: REFUSED: {exc}", file=sys.stderr)
        return 1
    a.out.write_bytes(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

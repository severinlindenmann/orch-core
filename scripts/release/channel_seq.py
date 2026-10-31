#!/usr/bin/env python3
"""Publish guard: may the channel pointer move to ``--new-seq``?  ``channel_seq.py --channel C --pointer-url U
--revocations R --new-seq N [--exclude-tag vX]``.

Uses trusted.trusted_current (the one invariant). Exit 0 the verified current seq is <= N (prints it); 3 the channel
has never been published (the version release being published is ``--exclude-tag``); 1 anything else, including a
lower seq or an unverifiable pointer. Never exits 0 without having verified.
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
    check_new_seq,
    core_verifier,
    gh_release,
    git_release_tags,
    git_tag_commit,
    http_pointer,
    trusted_current,
)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--channel", required=True)
    ap.add_argument("--pointer-url", required=True)
    ap.add_argument("--revocations", type=Path, required=True)
    ap.add_argument("--new-seq", type=int, required=True)
    ap.add_argument("--exclude-tag", default=None)
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
            releases_for_channel=git_release_tags(a.repo, a.exclude_tag, channel=a.channel),
        )
        check_new_seq(t, a.new_seq)
    except NothingToRefresh:
        print("channel_seq: first publish for this channel", file=sys.stderr)
        return 3
    except (Refused, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f"channel_seq: REFUSED: {exc}", file=sys.stderr)
        return 1
    print(t.seq)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Days left on a release-key certificate: ``cert_expiry.py KEY_CERT.json [--now ISO]``.

Prints ``days_left=N not_after=T`` (key=value lines for $GITHUB_OUTPUT). Exit 1 when the certificate is expired or
unreadable (fail closed). The certificate is public (it travels in every envelope); no key is involved.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import sys
from pathlib import Path

FMT = "%Y-%m-%dT%H:%M:%SZ"


def days_left(key_cert: dict, now: dt.datetime) -> tuple[int, str]:
    cert = json.loads(base64.b64decode(key_cert["cert"], validate=True))
    not_after = cert["not_after"]
    end = dt.datetime.strptime(not_after, FMT).replace(tzinfo=dt.UTC)
    return (end - now).days if end > now else -1, not_after


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("key_cert", type=Path)
    ap.add_argument("--now", default=None, help="UTC time, for tests")
    a = ap.parse_args(argv)
    now = dt.datetime.strptime(a.now, FMT).replace(tzinfo=dt.UTC) if a.now else dt.datetime.now(dt.UTC)
    try:
        left, not_after = days_left(json.loads(a.key_cert.read_text(encoding="utf-8")), now)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"cert_expiry: cannot read the certificate: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"days_left={left}")
    print(f"not_after={not_after}")
    return 1 if left < 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

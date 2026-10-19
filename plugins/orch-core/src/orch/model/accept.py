"""Pure manual-acceptance labels, bound to the criterion's exact current wording."""

from __future__ import annotations

import hashlib
import re

_LABEL = re.compile(r"accepted (AC[1-9][0-9]*) ([0-9a-f]{16})")


def criterion_hash(ac_id: str, text: str) -> str:
    return hashlib.sha256(b"orch.accept.v1\n" + ac_id.encode("ascii") + b"\n" + text.encode("utf-8")).hexdigest()[:16]


def label(ac_id: str, text: str) -> str:
    return f"accepted {ac_id} {criterion_hash(ac_id, text)}"


def parse_label(value: str | None) -> tuple[str, str] | None:
    match = _LABEL.fullmatch(value) if isinstance(value, str) else None
    return (match[1], match[2]) if match else None


def withdraw_label(ac_id: str) -> str:
    return f"withdrawn {ac_id}"

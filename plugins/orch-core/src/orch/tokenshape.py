"""Keep GitHub token detection in one place so refusal and redaction cannot drift.

host.auth, cli.render, ops.runtime and runner.grants share these shapes. Relay framing
cannot import orch.host and is outside this boundary: phones/relay carry no GitHub ops in v1.
"""

from __future__ import annotations

import re

GITHUB_TOKEN_SHAPE = re.compile(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}")
GITHUB_TOKEN_BYTES = re.compile(rb"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}")


# --- key material (#1044) -------------------------------------------------------------------------------------------
#
# Random base64url key bytes contain a ``ghp_`` + 20 name-chars run in about 1 invite in 13,000, so the GitHub-token
# shape
# must not be applied to the fields that ARE key material. They are named here by schema, never by content: a value is
# exempt only when it sits at a listed path of a listed document AND is canonical base64url of the decoded length
# of that field. Free text is never on this list. Only the GitHub-token shape is skipped for these values: callers still
# apply the grant shape and the presented secret to them, and scan everything else in full.

#: kind -> (min, max) decoded bytes. A signature, a P-256 public key and a dk_hash have one exact length; a sealed
#: device
#: label is ``salt(16) || AES-GCM(label)``, so it is bounded, not exact.
KEY_BYTES: dict[str, tuple[int, int]] = {
    "sig": (64, 64),
    "pub": (65, 65),
    "hash": (32, 32),
    "sealed": (32, 512),
}

#: file kind -> (path inside the ``{o, sig}`` document, key kind)
KEY_FILE_FIELDS: dict[str, tuple[tuple[tuple[str, ...], str], ...]] = {
    "device_cert": (
        (("sig",), "sig"),
        (("o", "dk_sig_pub"), "pub"),
        (("o", "dk_kx_pub"), "pub"),
        (("o", "label_sealed"), "sealed"),
    ),
    "reader_decl": (
        (("sig",), "sig"),
        (("o", "dk_hash"), "hash"),
    ),
}


def is_key_value(value: object, kind: str) -> bool:
    """Is ``value`` canonical base64url of a length that ``kind`` allows (signature, public key, hash, sealed label)?"""
    from orch import crypto

    lo, hi = KEY_BYTES[kind]
    if type(value) is not str or len(value) > (hi * 4) // 3 + 2:
        return False
    try:
        raw = crypto.unb64u(value)
    except crypto.EncodingError:
        return False
    return lo <= len(raw) <= hi


def split_key_file(data: bytes | str, doc: str) -> tuple[str, list[str]] | None:
    """A certificate or declaration file with its key-material values blanked, and those values.

    ``doc`` is the file kind the call site expects (``KEY_FILE_FIELDS``). ``None`` unless the text is strict JSON (no
    duplicate key), exactly one object ``{o, sig}`` with ``o`` an object: anything else is scanned whole by the
    caller. Only values of the exact shape at the listed paths are taken out; the rest, key names included, stays in
    the returned text."""
    import json

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        if len({k for k, _ in pairs}) != len(pairs):
            raise ValueError("duplicate key")  # last-wins parsing would hide the earlier text
        return dict(pairs)

    try:
        node = json.loads(data, object_pairs_hook=unique)
    except (ValueError, RecursionError):
        return None
    if type(node) is not dict or set(node) != {"o", "sig"} or type(node["o"]) is not dict:
        return None
    taken: list[str] = []
    for path, kind in KEY_FILE_FIELDS[doc]:
        parent = node
        for step in path[:-1]:
            parent = parent[step]
        if is_key_value(parent.get(path[-1]), kind):
            taken.append(parent[path[-1]])
            parent[path[-1]] = ""
    return json.dumps(node, ensure_ascii=False), taken

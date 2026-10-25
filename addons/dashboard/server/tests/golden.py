"""Golden JSON of the real adapters' output: the browser tests render the same files (src/test/fixtures/server-m2)."""

import json
import os
import re
from pathlib import Path

DIRECTORY = Path(__file__).parents[2] / "src/test/fixtures/server-m2"
ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
PERSON = re.compile(r"^p_[0-9a-f]{32}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")  # a question hash: it binds the (random) ticket uid, so its value is per run
HASH12 = re.compile(r"^[0-9a-f]{12}$")
GRANT = re.compile(r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
DEVICE = re.compile(r"^d_[0-9a-f]{32}$")
SESSION = re.compile(r"^s_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def normalize(value, seen=None):
    seen = {} if seen is None else seen

    def name(prefix, raw):
        return seen.setdefault(raw, f"{prefix}{sum(1 for v in seen.values() if v.startswith(prefix)) + 1}")

    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in ("now", "as_of"):
                out[k] = "2026-10-11T09:00:00Z"
            elif k in ("sub_cursor", "cursor") and isinstance(v, str):
                out[k] = "CURSOR"
            elif k == "next_page" and isinstance(v, str):
                out[k] = "PAGE." + v.split(".")[1]
            else:
                out[k] = normalize(v, seen)
        return out
    if isinstance(value, list):
        return [normalize(v, seen) for v in value]
    if isinstance(value, str):
        if GRANT.match(value):
            return name("gr_", value)
        if DEVICE.match(value):
            return name("d_", value)
        if SESSION.match(value):
            return name("s_", value)
        if ULID.match(value):
            return name("UID", value)
        if PERSON.match(value):
            return name("p_user", value)
        if HASH12.match(value):
            return "HASH12"
        if DIGEST.match(value):
            return name("sha256:Q", value)
        if SHA.match(value):
            return name("sha", value)
    return value


def check(name, value):
    """Compare with the committed golden; UPDATE_GOLDEN=1 rewrites it."""
    text = json.dumps(normalize(value), indent=1, sort_keys=True) + "\n"
    path = DIRECTORY / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert path.read_text() == text, f"{path} differs; run with UPDATE_GOLDEN=1 to update"

"""Scrub a real host's answers into fixtures: every identifier and time is replaced by a stable placeholder.

One mapping is shared by everything recorded in one session, so the references between answers still line up (a
person, a ticket uid, a digest). Nothing in a fixture is a real credential: the workspace is disposable and holds only
test data, and the scrub removes the ids anyway.
"""

import json
import re
from datetime import UTC, datetime, timedelta

PATTERNS = [
    ("p_", re.compile(r"\bp_[0-9a-f]{32}\b")),
    ("d_", re.compile(r"\bd_[0-9a-f]{32}\b")),
    ("gr_", re.compile(r"\bgr_[0-7][0-9A-HJKMNP-TV-Z]{25}\b")),
    ("s_", re.compile(r"\bs_[0-7][0-9A-HJKMNP-TV-Z]{25}(?:\.\d+)*\b")),
    ("sha256:", re.compile(r"\bsha256:[0-9a-f]{64}\b")),
    ("sha256:", re.compile(r"\bsha256:[0-9a-f]{32}\b")),
    ("g", re.compile(r"\b[0-9a-f]{40}\b")),
    ("w", re.compile(r"\b[0-9a-f]{32}\b")),
    ("h", re.compile(r"\b[0-9a-f]{12}\b")),
    ("u", re.compile(r"\b[0-7][0-9A-HJKMNP-TV-Z]{25}\b")),
]
TIME = re.compile(r"\b20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ\b")
BASE = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)


class Scrubber:
    def __init__(self):
        self.names: dict[str, str] = {}
        self.times: dict[str, str] = {}

    SHAPE = {
        "p_": lambda n: f"p_{n:032x}",
        "d_": lambda n: f"d_{n:032x}",
        "gr_": lambda n: f"gr_{n:026d}",
        "s_": lambda n: f"s_{n:026d}",
        "sha256:": lambda n: f"sha256:{n:064x}",
        "g": lambda n: f"{n:040x}",
        "w": lambda n: f"{n:032x}",
        "h": lambda n: f"{n:012x}",
        "u": lambda n: f"{n:026d}",
    }

    def name(self, prefix: str, raw: str) -> str:
        """The same shape as the original (so every adapter's own checks still apply), a stable counter inside it."""
        if raw not in self.names:
            kind = "sha256:" if prefix.startswith("sha256") else prefix
            if kind == "sha256:" and len(raw) == len("sha256:") + 32:
                self.names[raw] = f"sha256:{len(self.names) + 1:032x}"
            else:
                self.names[raw] = self.SHAPE[kind](len(self.names) + 1)
        return self.names[raw]

    def __call__(self, value):
        text = json.dumps(value, sort_keys=True)
        for prefix, rx in PATTERNS:
            text = rx.sub(lambda m, p=prefix: self.name(p, m.group(0)), text)
        stamps = sorted(set(TIME.findall(text)))
        for i, stamp in enumerate(stamps):
            self.times.setdefault(stamp, (BASE + timedelta(seconds=7 * len(self.times))).strftime("%Y-%m-%dT%H:%M:%SZ"))
        text = TIME.sub(lambda m: self.times[m.group(0)], text)
        text = re.sub(r'("next_page": ")[^"]+', r"\1PAGE.0", text)
        text = re.sub(r'("(?:cursor|sub_cursor|as_of_cursor)": ")[^"]+', r"\1CURSOR", text)
        return json.loads(text)

"""Person-owned directory checks go through privdir.others_can_write. A raw 0o022 / S_IWGRP test elsewhere is a
second, different answer to the same question (stock Ubuntu's private group), so it fails here unless the site is
root-owned or a file check and listed below."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "orch"
RAW = re.compile(r"0o0?22\b|0o0?20\b|0o0?2\b|0o0?02\b|S_IWGRP|S_IWOTH")
ROOT_OWNED = re.compile(r"st_uid != 0|st_uid == 0|\.uid != 0")  # a root-owned path keeps the strict rule
# Lines that are not a private-directory check: "anyone can write here" walk stop, and an access-reach probe.
OTHER_OK = ("S_ISVTX | stat.S_IWOTH", "self._reach(uid, path, 0o1, 0o2, 0o2)")
# Sites that stay strict on purpose: root-owned paths, files (keys, pins, tokens), the runner account's own dirs, and
# the passkey store. A new site needs a reason here.
STRICT = {
    "privdir.py",
    "land/key.py",
    "host/pins.py",
    "setup/osapply.py",
    "setup/osfront.py",
    "setup/osinstall.py",
    "setup/oslauncher.py",
    "click/stepup.py",
    "click/passkey_store.py",
    "runner/term.py",
    "runner/termclient.py",
    "runner/devmode.py",
    "runner/supervise.py",
}


def test_no_raw_group_write_mask_on_person_owned_paths():
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in STRICT:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if (
                RAW.search(line)
                and not line.lstrip().startswith("#")
                and not ROOT_OWNED.search(line)
                and not any(o in line for o in OTHER_OK)
            ):
                bad.append(f"{rel}:{n}: {line.strip()}")
    assert not bad, "use orch.privdir.others_can_write for person-owned paths:\n" + "\n".join(bad)

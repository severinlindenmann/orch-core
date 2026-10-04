"""Out-of-sync differences the human chose to ignore (spec v2 §13.2): one `<ticket>|<key>|<category>` per line in the
addon's state folder. When the issue's category changes, the token changes and the difference shows again."""
from __future__ import annotations

import os
from pathlib import Path

FILE = "ignored.txt"
MAX = 1000


def token(ticket_id: str, key: str, category) -> str:
    return f"{ticket_id}|{key}|{category}"


def ignored(state_dir) -> set[str]:
    try:
        text = (Path(state_dir) / FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def add_ignored(state_dir, value: str) -> None:
    folder = Path(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    lines = sorted(ignored(folder) | {value})[-MAX:]
    tmp = folder / (FILE + ".tmp")
    tmp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    os.replace(tmp, folder / FILE)

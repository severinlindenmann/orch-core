"""No file in the plugin may carry a git conflict marker: a merge once committed them into app.css."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.M)
SKIP = {".venv", "node_modules", ".git", "__pycache__", "vendor"}


def test_no_file_carries_a_conflict_marker():
    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or SKIP & set(path.parts) or path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in MARKER.finditer(text):
            hits.append(f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, m.start()) + 1}")
    assert not hits, "conflict markers found: " + ", ".join(hits)

"""``python -m orch.instructions write-plugin DIR``: regenerate the plugin layout (skills, hooks) below ``DIR``; the
repository copy under ``plugins/orch-core/`` is checked against it by a test."""

from __future__ import annotations

import sys
from pathlib import Path

from .harness import plugin_files


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "write-plugin":
        sys.stderr.write("usage: python -m orch.instructions write-plugin DIR\n")
        return 2
    root = Path(argv[1])
    for rel, text in plugin_files().items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

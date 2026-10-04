"""Write a built-in widget template, filled with its example data, as one standalone frame document.

    uv run python scripts/widget_preview.py mermaid out.html [--version 1] [--theme dark] [--nonce N]
    uv run python scripts/widget_preview.py --all outdir/

The document is what a dashboard frame loads (docs/widgets.md, "The frame"), so it can be opened in a sandboxed
iframe to check a template without the rest of orch.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orch.widgets.assemble import BUILTIN, assemble, read_libs, read_static


def build(name: str, version: str = "1", theme: str = "system", nonce: str = "preview-nonce") -> str:
    folder = BUILTIN / name
    meta = json.loads((folder / "widget.json").read_text(encoding="utf-8"))
    data = json.loads((folder / "example.json").read_text(encoding="utf-8"))[version]
    body = (folder / f"v{version}.html").read_text(encoding="utf-8")
    kit, tokens = read_static()
    return assemble(body, data, nonce=nonce, kit_js=kit, tokens_css=tokens, libs=read_libs(meta.get("libs", [])),
                    theme=theme, title=meta["title"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("name", nargs="?")
    p.add_argument("out")
    p.add_argument("--all", action="store_true", help="every built-in into the folder OUT as <name>.html")
    p.add_argument("--version", default="1")
    p.add_argument("--theme", default="system", choices=["light", "dark", "system"])
    p.add_argument("--nonce", default="preview-nonce")
    a = p.parse_args()
    if a.all:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        for folder in sorted(BUILTIN.iterdir()):
            if (folder / "widget.json").is_file():
                (out / f"{folder.name}.html").write_text(build(folder.name, a.version, a.theme, a.nonce), encoding="utf-8")
                print(out / f"{folder.name}.html")
    else:
        if not a.name:
            p.error("name a template, or pass --all")
        Path(a.out).write_text(build(a.name, a.version, a.theme, a.nonce), encoding="utf-8")
        print(a.out)


if __name__ == "__main__":
    main()

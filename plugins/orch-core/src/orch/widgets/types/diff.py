"""`diff`: a few changed lines of a unified diff, at most 200. Data: {"file", "lines": "- old\\n+ new"}. A line is
added if it starts with "+", removed with "-", a hunk header with "@@", else context; the +/- stays in the text so
colour is never the only cue."""
from __future__ import annotations

from orch.widgets.render import esc

NAME = "diff"
MOMENT = "review"
MAX_LINES = 200
SCHEMA = {"properties": {"file": {"type": "string", "minLength": 1, "maxLength": 500},
                         "lines": {"type": "string", "minLength": 1, "maxLength": 20000,
                                   "pattern": "^(?:[^\\n]*\\n){0,%d}[^\\n]*(?![\\s\\S])" % (MAX_LINES - 1)}}, "required": ["file", "lines"]}
EXAMPLE = {"type": "diff", "file": "src/board.py", "lines": "@@ -10,2 +10,2 @@\n def title(t):\n-    return t.name\n+    return t.name.strip()"}


def _kind(line: str) -> str:
    return "hunk" if line.startswith("@@") else "add" if line.startswith("+") else "del" if line.startswith("-") else "ctx"


def render_html(block, ctx) -> str:
    d = block.data
    rows = "".join(f'<span class="w-dl w-dl-{_kind(l)}">{esc(l) or "&nbsp;"}</span>' for l in d["lines"].split("\n"))
    return (f'<p class="w-diff-f"><code>{esc(d["file"])}</code></p>'
            f'<div class="w-scroll"><pre class="w-diff" tabindex="0">{rows}</pre></div>')


def render_text(block, ctx) -> str:
    return f'--- {block.data["file"]}\n{block.data["lines"]}'

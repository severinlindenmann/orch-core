"""`diffstat`: lines added and removed per file as small bars, with totals. Data: {"files": [{"path", "add", "del"}]}
(1-500, counts are integers >= 0). Each bar is as long as the file's changed lines relative to the biggest file,
split into an added part and a removed part; "+n" and "-n" are printed, so the colours repeat the text."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL
from orch.widgets.types._chartkit import p

NAME = "diffstat"
MOMENT = "review"
ALT = "numbers"
_N = {"type": "integer", "minimum": 0, "maximum": 10000000}
SCHEMA = {"properties": {"files": {"type": "array", "minItems": 1, "maxItems": 500, "items": {
    "type": "object", "additionalProperties": False, "required": ["path", "add", "del"],
    "properties": {"path": LABEL, "add": _N, "del": _N}}}}, "required": ["files"]}
EXAMPLE = {"type": "diffstat", "title": "Changed files", "files": [
    {"path": "src/orch/cli.py", "add": 42, "del": 7}, {"path": "tests/test_cli.py", "add": 120, "del": 0},
    {"path": "docs/widgets.md", "add": 3, "del": 3}]}


def _totals(files):
    return len(files), sum(f["add"] for f in files), sum(f["del"] for f in files)


def render_html(block, ctx) -> str:
    files = block.data["files"]
    top = max(f["add"] + f["del"] for f in files)
    rows = []
    for f in files:
        tot = f["add"] + f["del"]
        w = tot / top * 100 if top else 0
        a = f["add"] / tot * w if tot else 0
        rows.append(f'<li class="w-df"><code class="w-df-p">{esc(f["path"])}</code>'
                    f'<span class="w-df-n"><b class="w-add">+{f["add"]}</b> <b class="w-del">−{f["del"]}</b></span>'
                    f'<span class="w-df-b"><i class="w-add" style="width:{p(a)}%"></i><i class="w-del" style="width:{p(w - a)}%"></i></span></li>')
    n, a, d = _totals(files)
    return (f'<ul class="w-diffstat">{"".join(rows)}</ul>'
            f'<p class="w-meta">{n} file{"" if n == 1 else "s"} changed · <b class="w-add">+{a}</b> <b class="w-del">−{d}</b></p>')


def render_text(block, ctx) -> str:
    files = block.data["files"]
    n, a, d = _totals(files)
    return "\n".join([f'{f["path"]}: +{f["add"]} -{f["del"]}' for f in files] + [f"{n} file{'' if n == 1 else 's'} changed, +{a} -{d}"])

"""`checks`: a verdict per acceptance criterion (AC<n>, the positional ids of evidence.py and `ref: ac:<n>`). The
dashboard projects the latest verdict of each criterion next to it in Acceptance criteria (`projection`)."""
from __future__ import annotations

import re

from orch.widgets.render import esc

NAME = "checks"
MOMENT = "verify"
VERDICTS = {"met": ("ok", "✓", "Met"), "not_met": ("err", "✕", "Not met"), "unproven": ("warn", "○", "Unproven")}
SCHEMA = {"properties": {"rows": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
    "type": "object", "additionalProperties": False, "required": ["ac", "verdict", "evidence"],
    "properties": {"ac": {"type": "string", "pattern": "^AC[1-9][0-9]{0,2}$"}, "verdict": {"enum": list(VERDICTS)},
                   "evidence": {"type": "string", "minLength": 1, "maxLength": 2000},
                   "ref": {"type": "string", "maxLength": 500}}}}}, "required": ["rows"]}
EXAMPLE = {"type": "checks", "source": "pytest -q", "rows": [
    {"ac": "AC1", "verdict": "met", "evidence": "returns 404 for an unknown id", "ref": "tests/test_api.py::test_404"},
    {"ac": "AC2", "verdict": "unproven", "evidence": "needs a phone to try"}]}


def verdict_chip(verdict: str) -> str:
    role, glyph, word = VERDICTS[verdict]
    return f'<span class="w-verdict w-r-{role}"><span class="w-mark" aria-hidden="true">{glyph}</span>{word}</span>'


def _row(r: dict) -> str:
    ref = f' <code>{esc(r["ref"])}</code>' if r.get("ref") else ""
    return (f'<tr><th scope="row">{esc(r["ac"])}</th><td>{verdict_chip(r["verdict"])}</td>'
            f'<td>{esc(r["evidence"])}{ref}</td></tr>')


def render_html(block, ctx) -> str:
    return ('<div class="w-scroll"><table class="w-table w-checks"><thead><tr><th scope="col">Criterion</th>'
            '<th scope="col">Verdict</th><th scope="col">Evidence</th></tr></thead>'
            f'<tbody>{"".join(_row(r) for r in block.data["rows"])}</tbody></table></div>')


def render_text(block, ctx) -> str:
    return "\n".join(f'{r["ac"]}: {VERDICTS[r["verdict"]][2]} — {r["evidence"]}' + (f' ({r["ref"]})' if r.get("ref") else "")
                     for r in block.data["rows"])


def projection(ticket) -> dict[int, dict]:
    """{n: row + "word", "role"}: the latest verdict per criterion from the valid `checks` blocks in Verification
    (later blocks and later rows win). Read-only: the approved criteria are never touched."""
    from orch.widgets.blocks import parse_blocks
    from orch.widgets.validate import validate
    out: dict[int, dict] = {}
    for b in parse_blocks(ticket.section("Verification"), "Verification"):
        if (b.data or {}).get("type") != NAME or validate(b, ticket):
            continue
        for r in b.data["rows"]:
            role, _, word = VERDICTS[r["verdict"]]
            out[int(re.fullmatch(r"AC(\d+)", r["ac"]).group(1))] = {**r, "word": word, "role": role}
    return out

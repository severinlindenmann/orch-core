"""`deploy`: environment, commit, health, time. Data: {"env", "sha" (7-40 hex, shown as 7), "healthy": bool,
"at"?: time as written, "url"?: a web link or dashboard path; any other scheme is shown as text}."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import role_class, safe_url

NAME = "deploy"
MOMENT = "report"
SCHEMA = {"properties": {
    "env": {"type": "string", "minLength": 1, "maxLength": 60}, "sha": {"type": "string", "pattern": "^[0-9a-f]{7,40}$"},
    "healthy": {"type": "boolean"}, "at": {"type": "string", "maxLength": 60},
    "url": {"type": "string", "minLength": 1, "maxLength": 2000}}, "required": ["env", "sha", "healthy"]}
EXAMPLE = {"type": "deploy", "env": "staging", "sha": "9fceb02d0ae598e95dc970b74767f19372d61af8", "healthy": True,
           "at": "2026-10-04 12:10", "url": "https://staging.example.test/health"}


def _url(d, ctx=None) -> str:
    if not d.get("url"):
        return ""
    safe = safe_url(d["url"], ctx)
    if safe and safe.lower().startswith("http"):
        return f'<a href="{esc(safe)}" target="_blank" rel="noopener">{esc(d["url"])} ↗</a>'
    return f'<a href="{esc(safe)}">{esc(d["url"])}</a>' if safe else f'<code>{esc(d["url"])}</code>'


def render_html(block, ctx) -> str:
    d = block.data
    role, glyph, word = ("ok", "✓", "Healthy") if d["healthy"] else ("err", "✕", "Unhealthy")
    at = f'<span class="w-dep-at">{esc(d["at"])}</span>' if d.get("at") else ""
    url = f'<span class="w-dep-u">{_url(d, ctx)}</span>' if d.get("url") else ""
    return (f'<div class="w-dep"><span class="w-dep-env">{esc(d["env"])}</span><code class="w-dep-sha">{esc(d["sha"][:7])}</code>'
            f'<span class="w-verdict{role_class(role)}"><span class="w-mark" aria-hidden="true">{glyph}</span>{word}</span>'
            f'{at}{url}</div>')


def render_text(block, ctx) -> str:
    d = block.data
    return (f'{d["env"]} @ {d["sha"][:7]}: {"healthy" if d["healthy"] else "unhealthy"}'
            + (f' ({d["at"]})' if d.get("at") else "") + (f' {d["url"]}' if d.get("url") else ""))

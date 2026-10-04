"""`deps`: a small dependency / wave graph as inline SVG, laid out in columns by depth (the longest chain of
prerequisites; an edge [a, b] reads "a before b"). Data: {"nodes": [{"id", "label"?, "status"?}], "edges": [[from,
to]]}. Edges naming an unknown node are dropped; a cycle stops the depth count (nodes stay in their column). The
status is a word on the node plus a role colour: done/closed -> ok, blocked/failed -> err, waiting/testing/review
-> warn, in-progress/running/open -> info, any other word -> neutral."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import MARK

NAME = "deps"
MOMENT = "plan"
_ID = {"type": "string", "minLength": 1, "maxLength": 40}
SCHEMA = {"properties": {
    "nodes": {"type": "array", "minItems": 1, "maxItems": 40, "items": {
        "type": "object", "additionalProperties": False, "required": ["id"],
        "properties": {"id": _ID, "label": {"type": "string", "maxLength": 200}, "status": {"type": "string", "maxLength": 40}}}},
    "edges": {"type": "array", "maxItems": 120, "items": {"type": "array", "minItems": 2, "maxItems": 2, "items": _ID}}},
    "required": ["nodes", "edges"]}
EXAMPLE = {"type": "deps", "nodes": [{"id": "B-1", "label": "Schema", "status": "done"}, {"id": "B-2", "label": "API", "status": "in-progress"},
                                     {"id": "B-3", "label": "UI", "status": "blocked"}, {"id": "B-4", "label": "Docs"}],
           "edges": [["B-1", "B-2"], ["B-1", "B-3"], ["B-2", "B-3"]]}
ROLE_OF = {"done": "ok", "closed": "ok", "completed": "ok", "blocked": "err", "failed": "err", "waiting": "warn",
           "testing": "warn", "review": "warn", "in-progress": "info", "running": "info", "open": "info"}
W, H, GX, GY, PAD = 150, 52, 56, 16, 8  # node box, gaps and padding in svg user units
CHARS = 20


def layout(d: dict):
    """(column per node id, row per node id, edges kept)."""
    ids = [n["id"] for n in d["nodes"]]
    edges = [(a, b) for a, b in d["edges"] if a in ids and b in ids and a != b]
    depth = dict.fromkeys(ids, 0)
    for _ in ids:  # relax at most len(ids) times: a cycle just stops growing
        moved = False
        for a, b in edges:
            if depth[b] < depth[a] + 1 and depth[a] + 1 < len(ids):
                depth[b], moved = depth[a] + 1, True
        if not moved:
            break
    rows: dict[int, int] = {}
    row = {}
    for i in ids:
        row[i] = rows.get(depth[i], 0)
        rows[depth[i]] = row[i] + 1
    return depth, row, edges


def _cut(s: str) -> str:
    return s if len(s) <= CHARS else s[:CHARS - 1] + "…"


def render_html(block, ctx) -> str:
    d = block.data
    col, row, edges = layout(d)
    cols, maxrow = max(col.values()) + 1, max(row.values()) + 1
    width, height = PAD * 2 + cols * W + (cols - 1) * GX, PAD * 2 + maxrow * H + (maxrow - 1) * GY
    pos = {i: (PAD + col[i] * (W + GX), PAD + row[i] * (H + GY)) for i in col}
    lines = []
    for a, b in edges:
        (x1, y1), (x2, y2) = pos[a], pos[b]
        x1, y1, x2, y2 = x1 + W, y1 + H / 2, x2, y2 + H / 2
        mid = (x1 + x2) / 2
        lines.append(f'<path class="w-dg-e" d="M{x1:g} {y1:g} C{mid:g} {y1:g} {mid:g} {y2:g} {x2 - 2:g} {y2:g}" marker-end="url(#w-dg-arrow)"/>')
    nodes = []
    for n in d["nodes"]:
        x, y = pos[n["id"]]
        status = n.get("status")
        role = ROLE_OF.get(status.lower(), "neu") if status else "neu"
        glyph = MARK[role][0] if role != "neu" else "·"
        label = n.get("label") or n["id"]
        sub = f'{glyph} {_cut(status)}' if status else ""
        nodes.append(f'<g class="w-dg-n w-r-{role}"><title>{esc(n["id"])}: {esc(label)}{" (" + esc(status) + ")" if status else ""}</title>'
                     f'<rect x="{x}" y="{y}" width="{W}" height="{H}" rx="6"/>'
                     f'<text class="w-dg-l" x="{x + 8}" y="{y + 21}">{esc(_cut(label))}</text>'
                     f'<text class="w-dg-s" x="{x + 8}" y="{y + 40}">{esc(sub)}</text></g>')
    return ('<div class="w-scroll"><svg class="w-dg" role="img" aria-label="Dependency graph, text alternative below" '
            f'viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
            '<defs><marker id="w-dg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
            '<path d="M0 0L10 5L0 10z" class="w-dg-a"/></marker></defs>'
            f'{"".join(lines)}{"".join(nodes)}</svg></div>')


def render_text(block, ctx) -> str:
    d = block.data
    _, _, edges = layout(d)
    out = [f'{n["id"]}' + (f' {n["label"]}' if n.get("label") else "") + (f' [{n["status"]}]' if n.get("status") else "")
           for n in d["nodes"]]
    return "\n".join(out + [f"{a} -> {b}" for a, b in edges])

"""Checking a block against the contract: one layer key, the common keys, the type's or template's JSON Schema, where
it stands, and the files it pins. Codes: widget-parse, widget-schema, widget-place, widget-digest, widget-drift."""
from __future__ import annotations

from orch.widgets import artifacts, registry
from orch.widgets.blocks import LAYERS, MAX_BLOCKS, Block, Problem, placement, ticket_blocks


def _schema_problems(data, schema: dict) -> list[Problem]:
    import jsonschema  # lazy: heavy, and only blocks need it
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=lambda e: list(map(str, e.path)))
    return [Problem("widget-schema", f"{'/'.join(map(str, e.path)) or 'block'}: {e.message}") for e in errors[:5]]


def _as_uris(data):
    """`data` as the frame sees it, for the schema: each {path, sha256} object a 1-pixel image `data:` URI."""
    if isinstance(data, dict):
        if isinstance(data.get("path"), str) and "sha256" in data:
            return "data:image/png;base64,iVBORw0KGgo="
        return {k: _as_uris(v) for k, v in data.items()}
    return [_as_uris(v) for v in data] if isinstance(data, list) else data


def _layer_problems(data: dict, home) -> list[Problem]:
    present = [k for k in LAYERS if k in data]
    if len(present) != 1:
        return [Problem("widget-schema", "a block names exactly one of type, widget or html"
                        + (f" (it names {', '.join(present)})" if present else ""))]
    if "type" in data:
        name = data["type"]
        if name not in registry.core_types():
            return [Problem("widget-schema", f"unknown core type {name!r} (see `orch widget types`)")]
        return _schema_problems(data, registry.block_schema(name))
    if "widget" in data:
        found = _schema_problems(data, registry.WIDGET_SCHEMA)
        if found:
            return found
        spec, version = registry.template_version(home, data["widget"])
        if spec is None:
            return [Problem("widget-schema", f"no template {data['widget'].partition('@')[0]!r} "
                                             "(see `orch widget list`)")]
        if version is None:
            return [Problem("widget-schema", f"template {spec['name']} has no version "
                                             f"{data['widget'].partition('@')[2]}")]
        schema, payload = version.get("schema") or {"type": "object"}, data.get("data", {})
        found = _schema_problems(payload, schema)
        if found and artifacts.refs(payload):  # an image pinned as {path, sha256} reaches the frame as a data: URI
            found = _schema_problems(_as_uris(payload), schema)
        return found
    return _schema_problems(data, registry.HTML_SCHEMA)


def _digest_problems(data: dict, ticket, ws) -> list[Problem]:
    out = []
    if isinstance(data.get("widget"), str):
        state, current = registry.template_state(ws.home, data["widget"], data.get("sha256"))
        if state == "drift":
            out.append(Problem("widget-drift", f"template {data['widget']} changed since this widget was written; the "
                                               f"new version is not shown (it now has sha256 {current}: re-pin it "
                                               "only after checking what it draws)"))
    for r in artifacts.refs(data):
        state = artifacts.state(ws, ticket.id, r["ref"], r["sha256"])
        if state == "missing":
            out.append(Problem("widget-digest", f"{r['ref']} is missing (only files under artifacts/{ticket.id}/ "
                                                "or artifact:<name> can be shown)"))
        elif state == "changed" and r["node"] is data and "html" in data:  # the one-off page itself: it never runs
            out.append(Problem("widget-digest", f"{r['ref']} changed since this widget was written; it does not run"))
        elif state == "changed":
            out.append(Problem("widget-digest", f"{r['ref']} changed since this widget was written; it is not shown",
                               "warning"))
    return out


def validate(block: Block, ticket, section: str | None = None, *, ws=None) -> list[Problem]:
    """The problems of one block (also stored on `block.problems`). `section` defaults to the block's own; digests
    are checked only with a workspace (`ws`), whose home also holds the workspace's templates."""
    section = block.section if section is None else section
    if block.error:
        found = [Problem("widget-parse", block.error)]
    elif not placement(section):
        found = [Problem("widget-place", f"widgets cannot stand in {section or 'the text before the first section'}"
                                         " (hashed by a gate, own grammar or append-only); shown as code")]
    else:
        found = _layer_problems(block.data, ws.home if ws is not None else None)
        if not found and ws is not None and ticket is not None:
            found = _digest_problems(block.data, ticket, ws)
    block.problems = found
    return found


def check_ticket(ticket, *, raw: str | None = None, ws=None) -> list[Block]:
    """Every block of the ticket, validated, plus the ticket-wide rules: at most MAX_BLOCKS, ids unique."""
    blocks = ticket_blocks(ticket, raw)
    seen: set[str] = set()
    for b in blocks:
        validate(b, ticket, ws=ws)
        wid = (b.data or {}).get("id")
        if isinstance(wid, str) and not b.error:
            if wid in seen:
                b.problems.append(Problem("widget-schema", f"id {wid!r} is used twice in this ticket"))
            seen.add(wid)
        if b.index >= MAX_BLOCKS:
            b.problems.append(Problem("widget-parse", f"more than {MAX_BLOCKS} widgets in one ticket"))
    return blocks


def tickets(ws, ref: str | None = None):
    """(path, ticket, raw text) of one ticket or of every ticket that parses."""
    from orch.core import store
    from orch.errors import OrchError
    entries = [store.resolve(ws, ref)] if ref else [e for e in store.scan(ws) if e.meta is not None]
    for e in entries:
        try:
            raw = e.path.read_text(encoding="utf-8")
            yield e.path, store.read_ticket(e.path), raw
        except (OrchError, OSError, UnicodeDecodeError):
            if ref:
                raise


def findings(ws, ref: str | None = None) -> list[dict]:
    """Every widget problem of one ticket or the workspace, as rows for `orch widget check` and `orch check`."""
    from orch.widgets.blocks import has_blocks
    rows = []
    for _, ticket, raw in tickets(ws, ref):
        if not has_blocks(raw):
            continue
        for b in check_ticket(ticket, raw=raw, ws=ws):
            rows += [{"ticket": ticket.id, "index": b.index, "section": b.section, "line": b.line, **p.to_dict()}
                     for p in b.problems]
    return rows


def usage(ws) -> dict[str, dict[str, dict]]:
    """Which blocks of this workspace name each template and core type: {"widget": {name: row}, "type": {name: row}},
    row = {"uses", "versions": {v: n}, "tickets": [id, …], "last": the latest `updated` of those tickets}."""
    from orch.widgets.blocks import has_blocks
    out: dict[str, dict[str, dict]] = {"widget": {}, "type": {}}
    for _, ticket, raw in tickets(ws):
        if not has_blocks(raw):
            continue
        for b in ticket_blocks(ticket, raw):
            data = b.data or {}
            layer = b.layer if b.layer in ("widget", "type") else None
            if layer is None or not isinstance(data.get(layer), str):
                continue
            name, _, version = data[layer].partition("@")
            row = out[layer].setdefault(name, {"uses": 0, "versions": {}, "tickets": [], "last": ""})
            row["uses"] += 1
            if version:
                row["versions"][version] = row["versions"].get(version, 0) + 1
            if ticket.id not in row["tickets"]:
                row["tickets"].append(ticket.id)
            row["last"] = max(row["last"], str(ticket.meta.get("updated") or ""))
    return out

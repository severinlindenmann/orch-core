"""orch search: search tickets and their text"""

from collections.abc import Iterator
from typing import Any

from orch.ops import views
from orch.ops._dsl import INT, KEY, STR, I, S, arr, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call
from orch.ops.views import fence
from orch.store import StoreError


def _hits(v: Any, texts: dict[str, str], needle: str) -> Iterator[tuple[str, str]]:
    if needle in v.title.lower():
        yield "title", v.title
    for sid, text in texts.items():
        for line in text.split("\n"):
            if needle in line.lower():
                yield f"section {sid}", line
    for a in v.fields["acceptance"]:
        if needle in a["text"].lower():
            yield f"ac {a['id']}", a["text"]
    for t in v.fields["tasks"]:
        if needle in t["text"].lower():
            yield f"task {t['id']}", t["text"]


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """Case-insensitive substring search over the titles, sections, criteria and tasks of the tickets the actor may
    see, newest first; one hit per place."""
    c = Call.of(ctx, "search")
    needle = c.text(args["query"], one_line=True, what="query").lower()
    if not needle.strip():
        raise OrchError("invalid.input", "query is empty")
    c.store.load_all()
    hits: list[dict[str, str]] = []
    limit = args.get("limit", 10)
    for v in sorted(c.store.state.tickets.values(), key=lambda v: views.key_number(v.key), reverse=True):
        if not c.sees(v) or len(hits) >= limit:
            continue
        try:
            texts = c.store.body_sections(v.uid)
        except StoreError:
            texts = {}  # a body that does not match the log is not searched
        for where, line in _hits(v, texts, needle):
            hits.append({"key": v.key, "where": where, "line": views.short(line, 120)})
            if len(hits) >= limit:
                break
    body = "\n".join(f"{h['key']} {h['where']}: {h['line']}" for h in hits)
    return Result(
        data={"count": len(hits), "hits": hits},
        hints=[f"orch show {hits[0]['key']}" if hits else "orch list"],
        lines=fence(body, "search hits") if hits else ["no hits"],
    )


OP = operation(
    "search",
    "Read",
    "Search ticket titles and sections.",
    who="read",
    props={
        "query": S("what to look for", **{"x-metavar": "QUERY"}),
        "limit": I("at most this many", minimum=1, maximum=200, default=10, **{"x-metavar": "N"}),
    },
    required=("query",),
    positional=("query",),
    pre=("workspace_exists",),
    text="ok search {count}\nnext: {next}",
    data=obj({"count": INT, "hits": arr(obj({"key": KEY, "where": STR, "line": STR}))}),
    handler=handle,
)

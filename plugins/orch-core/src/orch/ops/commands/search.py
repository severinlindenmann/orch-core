"""orch search: search tickets and their text"""

from orch.ops._dsl import INT, KEY, STR, I, S, arr, obj, operation

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
)

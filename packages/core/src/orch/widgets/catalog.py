"""What an agent may write: each core type with a one-line shape and a working example (``orch widgets types``).

Names, shapes and examples follow the dashboard catalog (``src/api/widgetCatalog.ts``); ``timeline``, ``progress`` and
the multi-series form of ``series`` are accepted because the owner-reviewed mock accepts them. A test parses every
example with the same validator the write path uses.
"""

from __future__ import annotations

import json
from typing import Any

# name -> (what it shows, shape, example block)
TYPES: dict[str, tuple[str, str, dict[str, Any]]] = {
    "stats": (
        "Headline numbers with an optional change and a role.",
        '{"type":"stats","items":[{"label":"<=200","value":number|"<=200 chars","delta"?:number|string,'
        '"role"?:"ok|info|warn|err|neu"}]}  1 to 12 items',
        {
            "type": "stats",
            "id": "ex-stats",
            "title": "Seed load",
            "items": [
                {"label": "Tables loaded", "value": "31 of 40", "delta": "+9 since 09:00", "role": "ok"},
                {"label": "Seed time, s", "value": 48, "delta": -12},
                {"label": "Blocked tables", "value": 9, "role": "warn"},
            ],
        },
    ),
    "series": (
        "A line over x with markers, or up to four named lines over shared x.",
        '{"type":"series","unit"?:"<=20","points":[[x,y],...2-500]} or {"type":"series","x":[2-500 numbers or labels],'
        '"series":[{"name":"unique","values":[number|null per x]}] 1-4,"markers"?:[{"x","label"}] <=20}',
        {
            "type": "series",
            "id": "ex-series",
            "title": "Seed time per night",
            "unit": "s",
            "x": ["10-02", "10-03", "10-04", "10-05"],
            "series": [{"name": "seed", "values": [61, 64, None, 72]}, {"name": "test", "values": [22, 21, 25, 24]}],
            "markers": [{"x": "10-04", "label": "typed columns"}],
        },
    ),
    "spark": (
        "A word-sized trend inside a sentence, at {spark}.",
        '{"type":"spark","values":[2-500 numbers],"text":"<=500 chars containing {spark} once"}',
        {
            "type": "spark",
            "id": "ex-spark",
            "values": [9.1, 8.7, 8.9, 8.2, 7.9, 6.8],
            "text": "CI time over the last runs {spark} now 6.8 min.",
        },
    ),
    "checks": (
        "A verdict per acceptance criterion (the agent's check, not the human verdict).",
        '{"type":"checks","rows":[{"ac":"AC1","verdict":"met|not_met|unproven","evidence":"text","ref"?:"text"}]}',
        {
            "type": "checks",
            "id": "ex-checks",
            "title": "Acceptance criteria",
            "rows": [
                {"ac": "AC1", "verdict": "met", "evidence": "dbt seed loads 40 tables."},
                {"ac": "AC2", "verdict": "unproven", "evidence": "Waits on the valid_from type."},
            ],
        },
    ),
    "gates": (
        "Build, test and lint gates with durations.",
        '{"type":"gates","items":[{"name":"<=200","status":"pass|fail|skip|running","seconds"?:0..1e12}]}  1-500',
        {
            "type": "gates",
            "id": "ex-gates",
            "title": "Checks on the branch",
            "items": [
                {"name": "dbt build", "status": "pass", "seconds": 252},
                {"name": "dbt test", "status": "fail", "seconds": 97},
                {"name": "docs", "status": "skip"},
            ],
        },
    ),
    "diff": (
        "A few changed lines of one file as a unified diff, at most 200.",
        '{"type":"diff","file":"path <=500","lines":"unified diff, <=200 lines"}',
        {
            "type": "diff",
            "id": "ex-diff",
            "file": "models/fct.sql",
            "lines": "@@ -1,2 +1,2 @@\n-select day\n+select hour",
        },
    ),
    "callout": (
        "One sentence the reader must not miss.",
        '{"type":"callout","role":"ok|info|warn|err|neu","text":"1-2000 chars"}',
        {"type": "callout", "id": "ex-callout", "role": "info", "text": "Mask by hour, not by day."},
    ),
    "bars": (
        "Horizontal labelled bars, one may be highlighted.",
        '{"type":"bars","unit"?:"<=20","data":{"label":number>=0}|[["label",number]],"highlight"?:"a label"}  1-500',
        {
            "type": "bars",
            "id": "ex-bars",
            "title": "Bundle size, kB",
            "unit": "kB",
            "data": {"main": 412, "branch": 286},
            "highlight": "branch",
        },
    ),
    "table": (
        "A table (at most 50 columns, 500 rows); cells are string, number, boolean or null.",
        '{"type":"table","columns":["<=200",...1-50],"rows":[[cell per column],...0-500]}',
        {"type": "table", "id": "ex-table", "columns": ["meter_id", "rows"], "rows": [["M-88213", 2], ["M-90102", 2]]},
    ),
    "kv": (
        "Facts as label and value.",
        '{"type":"kv","items":{"label":string|number|boolean}}  1-50',
        {"type": "kv", "id": "ex-kv", "items": {"Seen in": "October build", "Duplicate ids": 112}},
    ),
    "timeline": (
        "Dated steps: what happened, what is happening, what is next.",
        '{"type":"timeline","items":[{"at":"YYYY-MM-DD[THH:MM[:SS]][Z]","label":"<=200","status"?:"done|current|next|failed",'
        '"note"?:"<=500"}]}  1-50',
        {
            "type": "timeline",
            "id": "ex-timeline",
            "title": "So far",
            "items": [
                {"at": "2026-10-07", "label": "Plan approved", "status": "done"},
                {"at": "2026-10-09T11:30Z", "label": "Fix the mask", "status": "current"},
            ],
        },
    ),
    "progress": (
        "A value against a maximum, or segments that add up to at most the maximum.",
        '{"type":"progress","max":>0,"unit"?:"<=20","value":0..max} or {...,"segments":[{"label","value">=0,'
        '"status"?:"ok|warn|err|neu"}]} 1-12',
        {
            "type": "progress",
            "id": "ex-progress",
            "max": 40,
            "unit": "tables",
            "segments": [
                {"label": "Loaded", "value": 31, "status": "ok"},
                {"label": "Blocked", "value": 9, "status": "warn"},
            ],
        },
    ),
}

COMMON = (
    'Every block may also carry "id" (a-z, 0-9, -; unique in the ticket), "title" (<=200), "source", "caption" (<=500).'
)
PLACEMENT = "Allowed in Context, Current state and Verification only (a gate signs the other sections)."


def types_lines() -> list[str]:
    out = [COMMON, PLACEMENT, "Write a block as a fenced code block whose info string is exactly: orch", ""]
    for name, (shows, shape, example) in TYPES.items():
        out += [f"{name}: {shows}", f"  shape: {shape}", "  example: " + json.dumps(example, ensure_ascii=False), ""]
    return out[:-1]

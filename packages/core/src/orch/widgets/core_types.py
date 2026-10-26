"""Structural validators, in the same refusal order as coreTypes.ts and parse.ts."""

import calendar
import math
import re

from .limits import MAX_MAGNITUDE
from .strict import MISSING, js_string, js_trim

CORE_TYPES = (
    "bars",
    "table",
    "checks",
    "kv",
    "stats",
    "series",
    "spark",
    "gates",
    "diff",
    "callout",
    "timeline",
    "progress",
)
PROPOSED_TYPES = ("timeline", "progress")
KEYS = {
    "bars": ["unit", "data", "highlight"],
    "table": ["columns", "rows"],
    "checks": ["rows"],
    "kv": ["items"],
    "stats": ["items"],
    "series": ["unit", "points", "x", "series", "markers"],
    "spark": ["values", "text"],
    "gates": ["items"],
    "diff": ["file", "lines"],
    "callout": ["role", "text"],
    "timeline": ["items"],
    "progress": ["value", "max", "unit", "segments"],
}
ROLES = ("ok", "info", "warn", "err", "neu")


def num(v):
    return type(v) is float and math.isfinite(v)


def short(v, maximum):
    return isinstance(v, str) and len(v) <= maximum


def one_of(v, choices):
    return isinstance(v, str) and v in choices


def unknown(o, allowed, where=""):
    return next((f'unknown key "{k}"' + (f" in {where}" if where else "") for k in o if k not in allowed), None)


def label(o, key, what):
    if not short(o.get(key), 200) or not o[key]:
        return f"{what} must be 1 to 200 characters"


def unit_ok(v):
    return "unit" not in v or short(v["unit"], 20)


def items(v, name, maximum, size, allowed, where, each, minimum=1):
    if not isinstance(v, list) or not minimum <= len(v) <= maximum:
        return f"{name} needs {size}"
    for o in v:
        if not isinstance(o, dict):
            return f"each entry of {name} must be an object"
        r = unknown(o, allowed, where) or each(o)
        if r:
            return r


def valid_date(s):
    if not isinstance(s, str):
        return False
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(T([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?Z?)?", s, re.ASCII)
    if not m:
        return False
    y, mo, d = map(int, m.group(1, 2, 3))
    return y >= 100 and 1 <= mo <= 12 and 1 <= d <= calendar.monthrange(y, mo)[1]


def plottable(values, what):
    if any(abs(v) > MAX_MAGNITUDE for v in values):
        return f"{what} must be between -1e15 and 1e15"


def check_series(v):
    if not unit_ok(v):
        return "unit must be a short string"
    points_form = "points" in v
    if points_form == ("series" in v):
        return "series needs exactly one of points or x with series"
    ys = []
    if points_form:
        if "x" in v:
            return "x goes with series, not with points"
        p = v["points"]
        if not isinstance(p, list) or not 2 <= len(p) <= 500:
            return "points needs 2 to 500 [x, y] pairs"
        if not all(isinstance(q, list) and len(q) == 2 and all(num(n) for n in q) for q in p):
            return "each point must be [x, y] with two numbers"
        xs = [q[0] for q in p]
        ys = [q[1] for q in p]
    else:
        xs = v.get("x")
        if not isinstance(xs, list) or not 2 <= len(xs) <= 500:
            return "x needs 2 to 500 values"
        if not all(num(x) for x in xs) and not all(short(x, 200) for x in xs):
            return "x must be all numbers or all labels"
        names = set()

        def each(o):
            r = label(o, "name", "a series name")
            if r:
                return r
            n = o["name"]
            if n in names:
                return f'series name "{n}" is used twice'
            names.add(n)
            vals = o.get("values")
            if not isinstance(vals, list):
                return f'values of "{n}" must be a list'
            if len(vals) != len(xs):
                return f'series "{n}" has {len(vals)} values, expected {len(xs)}'
            if not all(y is None or num(y) for y in vals):
                return f'each value of "{n}" must be a number or null'
            if not any(num(y) for y in vals):
                return f'series "{n}" needs at least one number'
            ys.extend(y for y in vals if num(y))

        r = items(v["series"], "series", 4, "1 to 4 lines", ["name", "values"], "a series", each)
        if r:
            return r
    numeric = all(num(x) for x in xs)
    r = plottable(ys, "values") or (plottable(xs, "x values") if numeric else None)
    if r:
        return r
    if "markers" in v:

        def marker(o):
            x = o.get("x", MISSING)
            ok = num(x) if numeric else isinstance(x, str) and x in xs
            if not ok:
                return (
                    "a marker x must be a number"
                    if numeric
                    else f'marker at "{js_string(x)}" is not one of the x labels'
                )
            return label(o, "label", "a marker label")

        return items(v["markers"], "markers", 20, "at most 20 markers", ["x", "label"], "a marker", marker, 0)


def check_core(t, v):
    if t == "series":
        return check_series(v)
    if t == "bars":
        if "data" not in v:
            return "bars needs data"
        data = v["data"]
        if isinstance(data, list):
            if not all(isinstance(p, list) and len(p) == 2 and isinstance(p[0], str) for p in data):
                return "data pairs must be [label, number]"
            pairs = data
        elif isinstance(data, dict):
            pairs = list(data.items())
        else:
            return "data must be an object of label: number or a list of [label, number]"
        if not pairs:
            return "data needs at least one bar"
        if len(pairs) > 500:
            return "more than 500 bars"
        for k, x in pairs:
            if len(k) > 200:
                return "a label is longer than 200 characters"
            if not num(x):
                return f'value of "{k}" must be a number'
            if x < 0:
                return f'value of "{k}" is negative; bars show sizes, so negative values are not drawn'
        if not unit_ok(v):
            return "unit must be a short string"
        if "highlight" in v and not any(isinstance(v["highlight"], str) and k == v["highlight"] for k, _ in pairs):
            return f'highlight "{js_string(v["highlight"])}" is not a label in data'
    elif t == "table":
        cols, rows = v.get("columns"), v.get("rows")
        if not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
            return "columns must be a list of strings"
        if not cols:
            return "table needs at least one column"
        if len(cols) > 50:
            return "more than 50 columns"
        if any(len(c) > 200 for c in cols):
            return "a column name is longer than 200 characters"
        if not isinstance(rows, list):
            return "rows must be a list of rows"
        if len(rows) > 500:
            return "more than 500 rows"
        for i, row in enumerate(rows, 1):
            if not isinstance(row, list):
                return f"row {i} must be a list"
            if len(row) != len(cols):
                return f"row {i} has {len(row)} cells, expected {len(cols)}"
            if not all(c is None or isinstance(c, (str, float, bool)) for c in row):
                return f"a cell in row {i} must be a string, number, boolean or null"
    elif t == "checks":
        rows = v.get("rows")
        if not isinstance(rows, list) or not rows:
            return "checks needs at least one row"
        if len(rows) > 500:
            return "more than 500 rows"
        for r in rows:
            if not isinstance(r, dict):
                return "each row must be an object"
            err = unknown(r, ["ac", "verdict", "evidence", "ref"], "a row")
            if err:
                return err
            if not isinstance(r.get("ac"), str) or not re.fullmatch(r"AC[1-9][0-9]*", r["ac"]):
                return "ac must be AC<n>, for example AC1"
            if not one_of(r.get("verdict"), ["met", "not_met", "unproven"]):
                return "verdict must be met, not_met or unproven"
            if not isinstance(r.get("evidence"), str):
                return "evidence must be a string"
            if "ref" in r and not isinstance(r["ref"], str):
                return "ref must be a string"
    elif t == "kv":
        data = v.get("items")
        if not isinstance(data, dict):
            return "items must be an object of label: value"
        if not data:
            return "kv needs at least one item"
        if len(data) > 50:
            return "more than 50 items"
        for k, x in data.items():
            if len(k) > 200:
                return "a label is longer than 200 characters"
            if not isinstance(x, (str, float, bool)):
                return f'value of "{k}" must be a string, number or boolean'
    elif t == "spark":
        vals = v.get("values")
        if not isinstance(vals, list) or not 2 <= len(vals) <= 500 or not all(num(x) for x in vals):
            return "values needs 2 to 500 numbers"
        r = plottable(vals, "values")
        if r:
            return r
        if not short(v.get("text"), 500):
            return "text must be a string of at most 500 characters"
        if v["text"].count("{spark}") != 1:
            return "text must contain {spark} exactly once"
    elif t == "diff":
        if not isinstance(v.get("file"), str) or not v["file"]:
            return "diff needs file (the path the lines come from)"
        if len(v["file"]) > 500:
            return "file is longer than 500 characters"
        if not isinstance(v.get("lines"), str) or not js_trim(v["lines"]):
            return "lines must be a non-empty unified diff"
        if len(v["lines"].split("\n")) > 200:
            return "a diff shows at most 200 lines"
    elif t == "callout":
        if not one_of(v.get("role"), ROLES):
            return "role must be ok, info, warn, err or neu"
        if not short(v.get("text"), 2000) or not v["text"]:
            return "text must be 1 to 2000 characters"
    elif t in ("stats", "gates", "timeline"):

        def each(o):
            if t == "timeline" and not valid_date(o.get("at")):
                return "at must be a real date (YYYY-MM-DD, optionally with THH:MM)"
            r = label(
                o,
                "name" if t == "gates" else "label",
                {"stats": "a stat label", "gates": "a gate name", "timeline": "a step label"}[t],
            )
            if r:
                return r
            if t == "stats":
                for k in ("value", "delta"):
                    if (k == "value" or k in o) and not num(o.get(k)) and not short(o.get(k), 200):
                        return f"{k} must be a number or a string of at most 200 characters"
                if "role" in o and not one_of(o["role"], ROLES):
                    return "role must be ok, info, warn, err or neu"
            elif t == "gates":
                if not one_of(o.get("status"), ["pass", "fail", "skip", "running"]):
                    return "status must be pass, fail, skip or running"
                if "seconds" in o and not (num(o["seconds"]) and 0 <= o["seconds"] <= 1e12):
                    return "seconds must be a number from 0 to 1e12"
            else:
                if "status" in o and not one_of(o["status"], ["done", "current", "next", "failed"]):
                    return "status must be done, current, next or failed"
                if "note" in o and not short(o["note"], 500):
                    return "note must be a string of at most 500 characters"

        maximum, size, allowed, where = {
            "stats": (12, "1 to 12 numbers", ["label", "value", "delta", "role"], "a stat"),
            "gates": (500, "at least one gate", ["name", "status", "seconds"], "a gate"),
            "timeline": (50, "1 to 50 steps", ["at", "label", "status", "note"], "a step"),
        }[t]
        return items(v.get("items"), "items", maximum, size, allowed, where, each)
    elif t == "progress":
        maximum = v.get("max")
        if not num(maximum) or not 0 < maximum <= MAX_MAGNITUDE:
            return "max must be a positive number up to 1e15"
        if ("value" in v) == ("segments" in v):
            return "progress needs exactly one of value or segments"
        if not unit_ok(v):
            return "unit must be a short string"
        if "value" in v:
            return None if num(v["value"]) and 0 <= v["value"] <= maximum else "value must be from 0 to max"
        total = 0.0

        def each(o):
            nonlocal total
            r = label(o, "label", "a segment label")
            if r:
                return r
            if not num(o.get("value")) or o["value"] < 0:
                return "a segment value must be a number of 0 or more"
            total += o["value"]
            if "status" in o and not one_of(o["status"], ["ok", "warn", "err", "neu"]):
                return "status must be ok, warn, err or neu"

        r = items(v["segments"], "segments", 12, "1 to 12 segments", ["label", "value", "status"], "a segment", each)
        if r:
            return r
        if total > maximum:
            return "segments add up to more than max"

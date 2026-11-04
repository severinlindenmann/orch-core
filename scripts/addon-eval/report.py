#!/usr/bin/env python3
"""Markdown tables from a results JSONL. Per failure cell first (the unit the design judges), then per task, then totals.

Usage: report.py RESULTS.jsonl [--tag T] [--model M] [--out FILE]
"""
from __future__ import annotations

import argparse
import collections
import json
import sys

GRADES = ["correct", "partial", "wrong", "used-off-addon", "used-addon-when-none-needed"]
SHORT = {"used-off-addon": "off-addon", "used-addon-when-none-needed": "addon-when-none"}


def load(path, tag=None, model=None):
    rows = [json.loads(l) for l in open(path) if l.strip()]
    return [r for r in rows if (tag is None or r["tag"] == tag) and (model is None or r["model"] == model)]


def table(rows, keyf, title, keyname):
    g = collections.defaultdict(list)
    for r in rows:
        g[keyf(r)].append(r)
    head = [keyname, "runs", "correct", *[SHORT.get(x, x) for x in GRADES[1:]], "skill read", "turns", "cost $"]
    out = [f"\n### {title}\n", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for k in sorted(g):
        v = g[k]
        c = collections.Counter(x["grade"] for x in v)
        sr = sum(1 for x in v if x["skills_read"])
        out.append(f"| {k} | {len(v)} | {c['correct']} ({100 * c['correct'] // len(v)}%) | " + " | ".join(str(c[x]) for x in GRADES[1:])
                   + f" | {sr} | {sum(x['turns'] for x in v) / len(v):.1f} | {sum(x['cost'] for x in v):.3f} |")
    return "\n".join(out)


def compare(rows, keyf, a, b):
    """Side by side correct-rates of two renderers per cell: `a` vs `b`."""
    cells = sorted({keyf(r) for r in rows})
    out = [f"\n### {a} vs {b} per cell (correct / runs)\n", f"| cell | {a} | {b} |", "|---|---|---|"]
    for k in cells:
        col = []
        for rd in (a, b):
            v = [r for r in rows if keyf(r) == k and r["renderer"] == rd]
            col.append(f"{sum(1 for x in v if x['grade'] == 'correct')}/{len(v)}" if v else "-")
        out.append(f"| {k} | {col[0]} | {col[1]} |")
    return "\n".join(out)


def render(rows):
    out = []
    for key in sorted({(r["set"], r["renderer"], r["n"]) for r in rows}):
        sub = [r for r in rows if (r["set"], r["renderer"], r["n"]) == key]
        out.append(table(sub, lambda r: r["cell"], f"{key[0]} / {key[1]} / N={key[2]}: per failure cell", "cell"))
    out.append(table(rows, lambda r: f"{r['set']} {r['renderer']} N={r['n']}", "totals", "set renderer N"))
    out.append(table(rows, lambda r: f"{r['set']} {r['renderer']} N={r['n']} {r['task']} ({r['cell']})", "per task", "task"))
    rds = [x for x in ("today", "today-oneliners", "proposed") if x in {r["renderer"] for r in rows}]
    if len(rds) >= 2:
        for st in sorted({r["set"] for r in rows}):
            for n in sorted({r["n"] for r in rows}):
                sub = [r for r in rows if r["set"] == st and r["n"] == n]
                out.append(compare(sub, lambda r: r["cell"], rds[0], rds[-1]).replace("per cell", f"per cell, {st} N={n}"))
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--tag")
    ap.add_argument("--model")
    ap.add_argument("--out")
    a = ap.parse_args()
    text = render(load(a.results, a.tag, a.model))
    if a.out:
        open(a.out, "w").write(text)
    sys.stdout.write(text)

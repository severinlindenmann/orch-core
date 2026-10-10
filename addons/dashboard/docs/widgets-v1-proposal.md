# Proposal: three additions to orch.widgets.v1

Status: proposal from the Mission Control mockup (owner decision 2026-10-10, item 2). Nothing here changes
`plugins/orch-core/docs/widgets.md` yet; this file is the text to take there when the owner accepts it.

The mockup draws two core types that v1 does not define (`timeline`, `progress`) and a second form of `series`. The
gallery (Widgets addon) marks all three "Proposed — not in orch.widgets.v1 yet". Everything else the mockup draws is
v1 as written, with one name per thing: the aliases `ok` (for the `gates` status `pass`) and `note` (for the role
`info`) were removed from the mockup, so a block that is valid there is valid in v1.

## 1. `timeline` (new core type)

**Shows:** dated steps of a ticket's story: what happened, what happens now, what comes next.

**Why:** agents write this today as a Markdown list in Current state. A list cannot say which step is current or which
one failed except in words, and readers skim past it. A timeline is one glance: done, failed, current, next. `gantt`
draws intervals per lane, not a sequence of events; `trail` is for breadcrumbs before a failure (nav, click, request).

**Schema:**

```json
{"type": "timeline", "items": [{"at": "YYYY-MM-DD[THH:MM[:SS]][Z]", "label": "≤ 200 chars", "status"?: "done|current|next|failed", "note"?: "≤ 500 chars"}]}
```

- `items`: 1 to 50 steps, in the order given (the renderer does not sort).
- `at` must name a day that exists (no 2026-02-31).
- Unknown keys are refused at every level, as for every core type.
- Text alternative: one line per step, `<at> <label> (<status word>): <note>`.

**Example:**

```widget
{"type": "timeline", "id": "so-far", "title": "So far", "items": [
  {"at": "2026-10-07", "label": "Plan approved", "status": "done"},
  {"at": "2026-10-09T09:55Z", "label": "T2 check fails", "status": "failed", "note": "Windows across midnight drop the whole day."},
  {"at": "2026-10-09T11:30Z", "label": "Fix the mask", "status": "current"},
  {"at": "2026-10-10", "label": "Verdict", "status": "next"}]}
```

## 2. `progress` (new core type)

**Shows:** one value against a maximum, or labelled segments that add up to at most the maximum.

**Why:** v1 says "`progress` is not a type: the Tasks section is already drawn by core." That stays true for tasks.
The mockup's agents need progress for things core does not count: tables loaded of 40, rows backfilled, files
migrated. Today they use `stats` with "31 of 40", which hides the proportion, or `bullet`, which needs a target and
bands that make no sense for a count. Segments show where the rest is (blocked vs not started).

**Schema:**

```json
{"type": "progress", "max": "> 0, ≤ 1e15", "unit"?: "≤ 20 chars", "value": "0..max"}
{"type": "progress", "max": "> 0, ≤ 1e15", "unit"?: "≤ 20 chars", "segments": [{"label": "≤ 200 chars", "value": ">= 0", "status"?: "ok|warn|err|neu"}]}
```

- Exactly one of `value` or `segments`; `segments` 1 to 12, adding up to at most `max`.
- Colours from the role tokens; the word of each segment is in its label (meaning never by hue alone).
- Text alternative: `31 of 40 tables (78%)`, or `Done: 1, Blocked on Q2: 2, Not started: 1; 4 of 4`.
- Guidance for widgets.md: "Do not draw the ticket's own tasks with it: core draws Tasks."

**Example:**

```widget
{"type": "progress", "id": "seed-load", "title": "Seed load", "max": 40, "unit": "tables", "segments": [
  {"label": "Loaded", "value": 31, "status": "ok"},
  {"label": "Blocked on Q2", "value": 9, "status": "warn"}]}
```

## 3. `series`: document the multi-series form

**Shows:** up to four named lines over shared x values (numbers or labels), with the same markers.

**Why:** the most common chart an agent wants is "before vs after" or "seed vs test" over the same nights. With v1's
`points` form that is two blocks with two y-axes that cannot be compared. The renderer already needs one axis per
block; drawing up to four lines on it costs nothing and keeps the comparison honest (one scale).

**Schema** (the `points` form stays as it is; a block has exactly one of the two forms):

```json
{"type": "series", "unit"?: "≤ 20 chars", "x": [2..500 numbers, or 2..500 labels ≤ 200 chars], "series": [{"name": "≤ 200 chars, unique", "values": [number or null, one per x]}], "markers"?: [{"x", "label"}]}
```

- `series`: 1 to 4 lines; each `values` has exactly as many entries as `x`; `null` is a gap; each line has at least one number.
- With label x, a marker's `x` must be one of the labels; with numeric x, any finite number (outside the range: not drawn).
- Numbers are bounded as in v1 (the Python schema: ±1e12).
- Text alternative: one line per series, `<name>: <x> <y><unit>, …`, then the markers.

**Example:**

```widget
{"type": "series", "id": "seed-time", "title": "Seed time per night", "unit": "s",
 "x": ["10-05", "10-06", "10-07", "10-08"],
 "series": [{"name": "seed", "values": [72, null, 58, 51]}, {"name": "test", "values": [24, 23, 20, 19]}],
 "markers": [{"x": "10-07", "label": "typed columns"}]}
```

## What changes in widgets.md

1. Core types table: add rows for `timeline` and `progress` (the schemas above, abridged), and extend the `series` row
   with `or {"x": [..], "series": [{"name","values"}] (1–4)}`.
2. Replace "`progress` is not a type: …" with "`progress` is for counts core does not draw; the ticket's tasks are
   drawn by core, never with `progress`."
3. Python: `orch/widgets/types/timeline.py`, `progress.py`, and the second form in `series.py` (SCHEMA, EXAMPLE,
   `render_html`, `render_text`), with the strict checks above.

The mockup's checks live in `src/app/pages/ticket/widgets/coreTypes.ts` and its gallery examples in
`src/api/widgetCatalog.ts`; both are tested (`catalog-types.test.ts`, `widgetCatalog.test.ts`).

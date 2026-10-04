# Designing an addon's UI

Read this before you return a single widget. `ADDONS.md` is the API; this is how it should look and read. Addons never write HTML or CSS: they return widgets from `orch.addons.widgets`, and core draws them with the design system's tokens (`static/tokens.json`, generated into `tokens.css`). Core owns the health line, the primary button, the pink "needs you" colour and every dialog.

Open `/design` in Mission Control to see every widget, variant and state as core draws it, in light and dark, at page (960 px), aside (320 px) and phone (375 px) widths. Check your addon with `orch addon check <folder> --strict`.

## Principles

- **Calm.** No alarm colours for routine states, no emoji, no spinners. Stale data stays readable; core says how old it is.
- **The human decides.** Your addon proposes; a `PendingDecision` is how you ask. You never draw a decision as "needs you".
- **Icon and text on every status.** Colour is never the only carrier: a Badge always has words, a role card always has a Badge or the role word in its title.
- **One page-level message.** At most one Callout per page, at the top.

## Pick the widget from the shape of the data

| You have | Use | Limits |
|---|---|---|
| A page-level situation (simulated data, login needed, partial access) | One `Callout` at the top of the page | 1 per page; title ≤ 8 words |
| Records (PRs, runs, issues) | `Table`: the key column first as a `Link` (`key=` if it is not column 0), status as a `Badge` column, times as `Time` | ≤ 5 columns on `page.*` and `board.external`, ≤ 3 in `ticket.*` and `today.from_addons`; ≤ 100 rows without a filter |
| A few facts about one thing | `KV` | 2–8 rows; short values |
| 2–4 counts | `KV(layout="stats")` | numbers only in values (a Badge value is drawn as a number in its role) |
| Several similar things (repos, environments) | `Card(layout="grid")` of compact cards, each with ≤ 1 `KV(stats)` | prefer **one table with the things as rows** when the facts are the same per thing |
| Filters | `Chips` of `Link(current=…)` with counts, or `Search` (page only) | ≤ 8 chips; give each Chips row a `label` when a card has two (core then shows it as visible text before the row); Search always gets a visible "Search" label |
| Same-page views | `Tabs` of `Link(current=…)` (page only) | ≤ 5 |
| A command or ID to paste | `Copy` | never secrets |
| Something outside orch | `Link` (core adds ↗ and opens a new tab) | |
| A change outside orch | `Action` in the row it affects; `quiet=True` for the lesser one | ≤ 1 per table row, ≤ 3 per card; verb + object |
| A timestamp | `Time(at, style="ago"|"at")` with an ISO 8601 time and offset | never a formatted string or UTC text |
| Today's number | One `Tile` with a noun label, a number and a `sub` line | `None` = unknown, not 0 |
| A human decision | `PendingDecision` (core draws the card) | ≤ 6 choices |
| One to three sentences | `Text` | no glyphs, no emoji |
| A link or code to scan | `QR`, always next to a `Copy` or `Link` | ≤ 1000 bytes |

What fits in each slot:

| Slot | Width it gets | Allowed | Budget |
|---|---|---|---|
| `page.<name>` | main column (560–1100 px) | everything except Tile | 1 callout; tables ≤ 5 columns |
| `board.external` | main column | everything except Tile, Search and Tabs | as page |
| `today.summary` | one tile (180–260 px) | Tile only | 1 tile |
| `today.from_addons` | half-width card column | Card, Text, Badge, Link, Callout, Table ≤ 3 columns, Action | ≤ 1 card |
| `ticket.code`, `ticket.sync`, `ticket.external`, `ticket.pages` | 320 px aside, or a phone column | Card, KV, Table ≤ 3 columns, Link, Badge, Action, Copy | ≤ 1 card, ≤ 3 actions |
| `workspace.settings` | text measure (720 px) | Text, KV, Callout, Link, Copy | no tables |

## How core draws your widgets

- **Containers, not viewports.** A widget sizes to the box it is in. Below 400 px KV rows stack, stats go two-up, action rows and Copy go full width.
- **Tables turn into stacked rows** by column count: ≤ 3 columns below 400 px, 4–5 below 560, 6–8 below 800. More than 8 never stack; they scroll with a sticky key column (and the lint warns). A stacked row shows the key cell as its title, then "label value" pairs; a Badge or Link drops its label; empty cells are hidden. Table semantics stay for screen readers.
- **A table inside a card** loses its own border and runs to the card's edges. Do not wrap one table in two cards, and do not put a table in a nested card.
- **Ticket keys** of this workspace (`DEMO-0004`) in Text, Callout text and table cells become links to the ticket. Keys of other systems stay text.
- **Consecutive Actions** share one row.
- **Role cards:** `Card(role="warn"|"err")` gets a 4 px rail; `ok`, `info` and `neu` cards are plain.
- **Core-only primitives.** The Board's Your move card, rail lanes, the ticket journey bar, evidence tiles and the Artifacts panel (see `/design` → Core primitives) are drawn by core around your slots. Addons cannot use them: show progress with a `KV` stat or a `Badge`, and link to the ticket for its proof.

## Colour and roles

Roles mean one thing each: `ok` done, passed, healthy · `info` running, in progress · `warn` stale, needs attention, login · `err` failed · `neu` draft, simulated, unknown. `you` (pink, "needs you") is core's and refused for addons. Fresh data is not `ok`: say nothing, core's health line covers it. No raw colours, HTML or CSS, ever.

## Copy

- **Sentence case** everywhere: titles, columns, buttons, chips. No ALL CAPS (PR, CI, ID, URL, API, SQL and DBR are fine) and no trailing colons.
- **Status text is ≤ 3 words** with no glyphs (core adds the icon): "failed", "1 running 3 h", "login needed".
- **Buttons say what happens:** "Rerun checks", "Close local". Never "OK", "Submit", "Yes" or "Click".
- **Link text says where it goes:** "Open run", "PR #22"; never "here", "link", "more" or a raw URL.
- **Empty text answers why and what next:** "No pull request needs your review. 5 are yours · 1 has failing checks." The default "Nothing here." fails the lint.
- **No emoji, no exclamation marks, no "Error:" prefixes, no jokes.** Personality comes from specific facts.
- Say "simulated" or "partial" **once** (a callout or a column header), never in every row.

## Health and staleness

- Always return a `Snapshot` with the right `health`; never raise for expected failures.
- Never draw your own "updated 2 min ago" or freshness badge.
- `auth_required`: put the exact login command in `message`. `rate_limited`: set `retry_after`. Partial data: `complete=False`.
- On failure, return no items with a failing health; core keeps showing the last good items as stale.

## `orch addon check`

Errors fail the check:

| ID | Rule |
|---|---|
| E1 | Badge text, Callout title, Card title and Tile label are not empty |
| E2 | `role="you"` is refused |
| E3 | Search and Tabs outside `page.*`; Tile outside `today.summary`; anything but Tiles in `today.summary` |

Besides these, the structural checks fail on undeclared actions, unsafe URLs, nesting deeper than 2, wrong row lengths, field caps and anything that is not a widget.

Design warnings print as `△` and fail only with `--strict` (the default addons and the template pass `--strict`). They run on your real widget output for every slot and health state:

| ID | Warns when |
|---|---|
| W1 | A Table has > 5 columns on `page.*`/`board.external`, or > 3 on `ticket.*`/`today.from_addons` |
| W2 | A Table on a page has > 100 rows and the page has no Search or Chips |
| W3 | A Table's `empty` is "Nothing here." or under 4 words |
| W4 | A Badge is longer than 3 words or 24 characters |
| W5 | Text or a Badge starts with a status glyph (✓✕▲●◐○⚠✔❌) or contains emoji |
| W6 | `!` in a Badge, Callout title, Card title or Action label |
| W7 | An ALL CAPS word, or a trailing ":" in a title or column header |
| W8 | An `ok` Badge says updated, ago, fresh or synced |
| W9 | Role and words disagree: fail/error/broken not `err`; stale/expired/login/warning not `warn` or `err`; running/in progress not `info` |
| W10 | More than one Callout at the top of a page, or a Callout after a Table |
| W11 | Chips with > 8 items, or two Chips rows in one card without labels |
| W12 | Text or a cell that looks like a timestamp (`2026-10-03T…`, `UTC`): use `Time` |
| W13 | Link text is here, click here, link or more, or the URL itself |
| W14 | A Card with > 3 Actions, or a table row with > 1 Action |
| W15 | A Table inside a nested Card |
| W16 | `Card(role="warn"|"err")` with no Badge and no role word in its title |
| W17 | A Tile without `sub`, or with a non-numeric string value |
| W18 | The manifest `title` or `menu.title` is longer than 24 characters or not sentence case |

`orch addon check --json --format v2` prints `{"errors": [...], "warnings": [...]}`; plain `--json` keeps the flat problem list (with `--strict` and no errors, the warnings go into that flat list and the exit code is 5).

The warnings are heuristics, so know their limits:

- They see only what your addon renders in the contract (every slot and health state with sample data). A table that only appears for some filter or data is not linted unless the contract's sample reaches it; check those views yourself.
- W9 skips negated or zero wording ("0 failed", "no errors", "nothing stale", "not running"), so an `ok` Badge may say them.
- W7 and W18 allow the acronyms listed under W7, words with an inner capital (GitHub, DevOps) and a short list of product names (Databricks, Confluence, Jira, Slack, Azure, …; `PROPER_NOUNS` in `orch/addons/design_lint.py`). The addon's own name parts may also be written in capitals: an addon named `acme-foo` may say FOO in its titles and widgets. Another product name in Title Case is flagged; rephrase or ignore that warning without `--strict`.
- W4 counts only words that contain a letter ("1 running 3 h" is two words).

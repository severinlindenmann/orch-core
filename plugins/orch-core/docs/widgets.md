# Ticket widgets — format orch.widgets.v1

Agents put small visual blocks into ticket sections so a person reads less: a chart of measured runs, a check
table, a before/after slider, a clickable prototype of an option. This document is the contract.

## One fence, three layers

A widget is a fenced code block whose info string is exactly `orch`, holding **one strict JSON object**:

````
```orch
{"type": "bars", "title": "Bundle size, kB", "source": "size.txt",
 "data": {"main": 412, "branch": 286}}
```
````

The object has exactly one of these keys, which selects the layer:

| Key | Layer | Drawn by | Script |
|---|---|---|---|
| `type` | core type | orch (Python, server-side HTML/CSS/inline SVG) | none |
| `widget` | template, `name@version` | the template's HTML/JS in a sandboxed frame | agent-written, reused |
| `html` | one-off page, a ticket artifact path + `sha256` | the artifact's HTML/JS in a sandboxed frame | agent-written, once |

Common optional keys on every block: `title` (≤200 chars), `source` (≤500: where the numbers came from — a file,
a command, "agent-measured"), `caption` (≤500), `id` (`[a-z][a-z0-9-]{0,39}`: a letter first, so it never
reads as an index; the anchor `#w-<id>`). An id must be unique in the ticket: blocks that share one are refused
everywhere (drawn as code, never served to a frame, an error in `orch widget check`). Unknown top-level keys are an error.

## Parsing rules (strict)

- The fence follows CommonMark: an opening run of 3+ backticks or tildes; it closes on a run of the **same
  character, at least as long**, with nothing but whitespace after it. orch's section splitter, `neutral_text`,
  the body parser and evidence parser all use the one shared rule in `orch/core/fences.py` (previously
  ```` ```` ```` opened and ```` ``` ```` closed in orch but not in CommonMark).
- Body: JSON only (`json.loads` with duplicate-key rejection, `NaN`/`Infinity` rejected, nesting depth ≤ 8).
- Size caps: 64 KiB per block, 40 blocks per ticket, 500 rows / 50 columns in any table-like array, strings
  ≤ 20 000 chars, labels ≤ 200.
- Each core type and each template carries a JSON Schema (draft 2020-12, validated with `jsonschema`);
  `additionalProperties: false` everywhere.
- An invalid block never breaks the page: it renders as an error callout naming the section, the line inside
  the ticket file, and the problem, followed by the raw block as code.

## Where widgets may stand

Allowed: **Context, Current state, Verification, Findings**, and any
extra `## Heading` that is not a known section.

Refused: Ask, Summary, Requirements, Acceptance criteria, Out of scope, Plan (all hashed by a gate — an edit
would void approval), Tasks (own grammar), Log (append-only). A block there is not rendered as a widget; it
shows as code with a warning, and `orch widget check` reports `widget-place`.

## Results live in Verification

The `checks` core type records a verdict per acceptance criterion. Criterion ids are `AC<n>`, the same
positional ids `ref: ac:<n>` and the evidence parser already use. The dashboard projects the latest verdict of
each criterion as a read-only chip next to that criterion in Proven, labelled "the agent's check": it is the agent's
claim, not the human's verdict, and it proves nothing by itself. A criterion counts as proven only by the evidence
rule of `orch.core.evidence` (a top-level Verification line that cites `AC<n>`); a widget fenced under such a line is
part of that criterion's evidence and is drawn inside its evidence on the ticket page, one fenced before any citing
line is "Other evidence". The approved text is never touched.

## Files by digest

Any artifact a block names (images, data files, one-off HTML) is a file in the ticket's own artifact folder, named
as `artifacts/<ID>/<name>` or, like ticket Markdown, `artifact:<name>` (orch.core.artifacts), plus its `sha256`.
The file need not be linked in the ticket's frontmatter: the block's own digest pins it. On render orch hashes the
file (`orch.core.artifacts.file_sha256`, cached per file version): missing → "missing" state; mismatch → "changed
since this widget was written; it is not shown" warning, and the file is not shown (no image, no data: URI, a
one-off page does not run): a widget never shows other bytes than the digest its text pins. On a page the /a/ URL
carries `?v=<digest>`, so a file swapped after the page was drawn is refused by the route as well. `orch widget add` fills in the digest.
The text of a block is part of the section a gate or verdict hash binds, so the digest makes the text pin the file;
`orch.core.artifacts.binding` reads only Markdown images and links, never the inside of a ```orch fence, and the
widget rule is a render rule on the same parser, so drawing widgets changes nothing a gate binds.

Links and inline Markdown inside a block follow R24 like every link in ticket Markdown: a `url` (`links`, `deploy`)
goes through `orch.dashboard.markdown.canonical_link`, and inline text (`text`, `callout`, `options` notes, `health`)
is rendered with the ticket's id and no linked files, so an `artifact:` image or link there stays inert (it is not in
the token stream the hashes bind) and a link to the ticket's own `/a/` files is inert. Files are shown only through
the media types, by digest.

## Provenance and text alternative

Every widget is drawn inside chrome orch owns: title, a layer chip (`core` / `agent HTML · name@v` /
`agent HTML · one-off`), the `source` line, and a **Show numbers / Show text** toggle with the text alternative.
- Core types generate their text alternative (`render_text`), also used by `orch widget render --text`,
  `orch show --widgets`, screen readers and TIX.
- Agent HTML provides it through `orch.text(...)`; until it does, and when agent HTML is off, the chrome shows
  `caption` or "No text alternative given".
Chrome is always outside any frame, so a widget cannot fake its own label or provenance.

## Core types

Defined one per module in `orch/widgets/types/<name>.py` (auto-discovered): `NAME`, `SCHEMA`, `MOMENT`
(understand|decide|plan|verify|review|debug|report), `EXAMPLE`, `render_html(block, ctx) -> Markup`,
`render_text(block, ctx) -> str`. CSS in `static/widgets/<name>.css` or a shared group file. Colours only from
the dashboard role tokens (`--ok-*`, `--warn-*`, `--err-*`, `--info-*`, `--neu-*`, `--you-*`); meaning never by
hue alone (icon or word as well). Geometry is derived from the numbers. Every type works at 390 px and in dark.

| Type | Shows | Data (abridged) |
|---|---|---|
| `text` | a paragraph (Markdown inline) | `{"text"}` |
| `callout` | one sentence the reader must not miss | `{"role": ok\|info\|warn\|err\|neu, "text"}` |
| `stats` | headline numbers, optional delta | `{"items": [{"label","value","delta"?,"role"?}]}` |
| `chips` | tags / scope (files, routes) | `{"items": [str or {"text","role"}]}` |
| `table` | a table | `{"columns": [..], "rows": [[..]]}` |
| `links` | "look here" links with expectation | `{"items": [{"label","url","as"?,"expect"?}]}` |
| `health` | on track / at risk / blocked + why | `{"health": on_track\|at_risk\|blocked, "why"}` |
| `checks` | verdict per acceptance criterion | `{"rows": [{"ac": "AC1","verdict": met\|not_met\|unproven,"evidence","ref"?}]}` |
| `gates` | build/test/lint gates with durations | `{"items": [{"name","status": pass\|fail\|skip\|running,"seconds"?}]}` |
| `tests` | test delta, not totals | `{"added","fixed","broke","flaky"?,"total"?,"names"?: {added\|fixed\|broke\|flaky: [name]}}` (integers >= 0) |
| `bars` | horizontal labelled bars | `{"unit"?,"data": {label: number} or [[label, number]], "highlight"?}` |
| `runs` | repeated measurements as columns, marks | `{"unit"?,"values": [..],"marks"?: {"<1-based>": note}}` |
| `spark` | word-sized sparkline inside a sentence | `{"values": [..], "text": "CI {spark} now 6m"}` (`{spark}` exactly once) |
| `series` | line over time with event markers | `{"unit"?,"points": [[x, y]] (numbers),"markers"?: [{"x","label"}]}` |
| `bullet` | value vs target with bands | `{"value","target","bands": [good, ok, bad],"unit"?, "lower_is_better"?}`; `bands` are zone limits, best first, `bad` the far end of the scale: `[300, 350, 450]` with `lower_is_better`, `[90, 70, 0]` without |
| `scores` | 0–100 scores with Lighthouse bands | `{"items": {"perf": 91, "a11y": 100}}` |
| `gantt` | lanes with intervals | `{"unit"?,"lanes": {name: [[start, end], ..]}}` |
| `diffstat` | +/− per file as small bars | `{"files": [{"path","add","del"}]}` |
| `diff` | a few changed lines, at most 200 | `{"file", "lines": "@@ …\n- old\n+ new"}` |
| `options` | options with cost/risk, recommended | `{"pick"?: id, "items": [{"id","title","cost"?,"risk"?,"notes"?}]}` (unique ids, 1–8) |
| `matrix` | options × criteria scores or ticks | `{"criteria": [..], "rows": {option: [0–5 or "✓"/"✗"]}, "weights"?: [..], "pick"?}`; totals computed (✓ = 1, ✗ = 0, × weight) |
| `risk` | flags (auth, data, money, …) | `{"items": {flag: true\|false\|"note"}}` (true or a note = flagged) |
| `deps` | ticket dependency / wave graph (inline SVG, columns by depth) | `{"nodes": [{"id","label"?,"status"?}], "edges": [[from, to]]}` |
| `flow` | a linear flow with one highlighted step | `{"steps": [str, 2–20 unique], "highlight"?: step, "notes"?: {step: note}}` |
| `trail` | breadcrumb events before a failure | `{"items": [{"t","kind": nav\|click\|request\|log\|error\|…,"text"}]}` |
| `deploy` | env, commit, health, time | `{"env","sha" (7–40 hex, shown as 7),"healthy": bool,"at"?,"url"? (web link or path only)}` |
| `compare` | two images side by side (static) | `{"before": {path, sha256}, "after": {..}, "labels"?: [before, after]}` |
| `screens` | screenshot grid (viewport × theme), mockups | `{"items": [{"label","path","sha256"}], "columns"?: 1–6}` |
| `video` | a recording, native `<video>` | `{"path","sha256","poster"?: {path, sha256}}` |

`progress` is not a type: the Tasks section is already drawn by core.

## Templates (agent HTML/JS, reused)

`orchestrator/widgets/<name>/` in the workspace (plus built-ins shipped in `orch/widgets/builtin/`):

```
widget.json   {"name","title","description","moment","libs": ["uplot"],"min_height": 160,
               "versions": {"1": {"schema": {...}, "notes": "..."}, "2": {...}}}
v1.html v2.html   the widget body (markup + <script>), uses only orch-kit and the listed libs
example.json   {"1": {...data...}, "2": {...}}
```

A fence names `"widget": "name@2"` and its pin `"sha256"`; both are required. `orch widget add` validates the data
against that version's schema and writes the pin. A changed template is a new version; old tickets keep theirs.

### Pinned templates

The pin is `registry.template_digest`: the sha256 of the version's page bytes (`v<n>.html`), a NUL byte and the
sorted `libs` of widget.json as compact JSON, so neither the page nor what it loads can change under a block. A block
whose template no longer matches its pin is **drift** (`widget-drift`, an error): the chrome shows a "Drift" callout
with the digest the template has now, no frame is drawn, and `GET /w/…` refuses too (it checks the digest on the
very bytes it would serve). The new version is never shown under the old pin. Re-pinning means writing the new digest
into the block, which changes the section text; in Verification that changes the verdict hash, so the human reads
the widget again.

#### Upgrades: drift and re-pinning

A built-in template that changes in an upgrade makes every existing block that pins its old digest show "Drift" until
that block is re-pinned. This is by design: the verdict hash binds the pin, so a template must never change what a
reader or a verdict covers without somebody looking at it. Nothing is repaired automatically.

To re-pin, run `orch widget check <ID>`, read what the new version draws (`orch widget render <ID> --html`, or
`orch widget show <name@v>`), then run `orch widget repin <ID> --block <id|index>` (or `--all`). It replaces each
drifted block's `"sha256"` with the template's current digest, prints a diff of what changes, and writes through the
same path, locks and event as `orch section set`. `--dry-run` shows the diff and writes nothing. It never re-pins a
block whose template is unknown or whose file pins are broken (missing or changed): `--block` refuses it, `--all`
lists it as skipped. It edits ticket text, so it is not a human-only verb, and it is not a decision.

Re-pinning edits the section text, so where the section is Verification or Acceptance criteria and a verdict hash was
already bound to the ticket or to its epic (whose verdict binds every open child's criteria and Verification), the old verdict is stale: `repin` says so whatever the ticket's status, the human has to read the
widget again and give a fresh verdict, and an agent cannot give or carry it over. The verdict hash already reads
"drift" right after the upgrade, so an earlier verdict is refused even before any re-pin.

What the pin leaves out is display-only: widget.json's `title`, `description`, `moment`, `min_height` (the frame's
starting height before the document reports its own) and each version's `schema` and `notes` (the schema only checks
the block's data, which is in the block's own text). None of them reaches the frame document.

Bytes are checked as they are sent: a frame's page, an inlined image, an embedded file and an `/a/…?v=<digest>`
response are each read once and hashed from that read (`orch.core.artifacts.read_pinned`); a cached digest is used
only to decide what to list or warn about, never to vouch for bytes served.

A Verification widget is part of what the human reads before the verdict, so the verdict hash binds its pins like
gate hash v3 binds inline artifacts (`orch.core.artifacts.binding(..., widgets=True)`, see docs/ticket-schema.md,
"Verdict hash"): a template edited in place, or a pinned file swapped, after the page was drawn changes the hash and
the verdict is refused as stale. The decision cards (Today, Groom, the board's flow) and the epic verdict draw widgets
through the same `section_md` path and checks as the ticket page (`markdown.section_widgets`), never as raw JSON; a
page that draws widgets links their CSS and host script once, before the first one. Workspace templates shadow
built-ins of the same name. A widget.json that does not have this shape (`registry.TEMPLATE_SCHEMA`: `versions` an
object of `"<n>": {schema?: a JSON Schema, notes?}`, `min_height` an integer 40-2000, …) is left out, so a block
naming it gets "no template"; `orch widget list`, `orch widget check` and `/widgets` say which file and why.

Built-ins: `before-after`, `option-prototype`, `agent-waterfall`, `decision-matrix`, `eval-grid`, `ci-timeline`,
`mermaid`, `bundle-treemap`.

## One-off pages

`{"html": "artifacts/B-0042/replay.html", "sha256": "…", "data"?: {...}, "libs"?: [...], "height"?: 480}`.
Same frame and kit as templates. `orch widget promote <ID> <artifact> --name <name>` turns one into a template
v1 in `orchestrator/widgets/<name>/` (copies the HTML, writes widget.json with a strict schema inferred from the
block's `data` when a block names the page, else `{"type": "object"}`, and example.json). An existing name is
refused; `--version-bump` adds the next version to a workspace template of that name. A digest mismatch draws with
the warning; a missing file is an error state and loads nothing.

Images in a template's or one-off's `data` are pinned like any file, as `{"path": "artifacts/<ID>/x.png", "sha256":
"…"}`; the frame receives each one as a `data:` URI string (≤ 5 MB), and the template's schema is checked against
that form (a `data:image/…` string). Each file is read once per document however often the data names it, and all
inlined images together, repeats included, stay under 8 MB (`artifacts.MAX_INLINED`); beyond that the frame shows
the error "too many or too large images" instead. A core `screens`/`compare` document is held to the same budget.
Digests are hashed once per file version (path, size, mtime).

## The frame (agent HTML)

Agent HTML is on when the workspace config says `widgets.html: true` (default **false**) **and** the approval ledger
on this machine holds a current signed human decision to turn it on for this workspace and this checkout.
`orch widget html on` is that decision. It is a human verb like approve or verdict, refused for any process with an
agent harness in its ancestry and confirmed by typing; the signed entry is bound to the workspace, to the checkout
(its git common dir plus the workspace's path inside the repository, so a nested workspace has its own and the
worktrees of one clone share it) and to the value, and chained to the previous entry for the setting.
`orch widget html off` takes power away, so anyone may run it, and every off is signed too: the newest entry decides,
so after any off only a new human on turns it back on. Wherever the setting is read (`Ctx.of`: ticket pages, frames,
previews, decision cards, addons) it counts as off when the config says true without such a decision: a hand edit or
a pull, an entry that does not chain onto the one before it, a ledger cut short (see the head record in
docs/ticket-schema.md), a later off (in the ledger or the event log), another
machine, or a moved, copied or cloned checkout (each needs its own `orch widget html on`). `orch check` reports that
as `unsigned-setting`, and reports `stale-setting` when the config says false but a signed on is still in force (an
off made by hand rather than with `orch widget html off`). The `/widgets` page says how to turn it on, and
`orch widget html` alone prints the state. Off → the chrome shows the text alternative.

A `srcdoc` frame inherits the embedding page's CSP (the dashboard's `script-src 'self'` would block every inline
script), so each frame is a URL of its own: `GET /w/<ID>/<section>/<digest>`, where `digest` is the sha256 of the block's
canonical JSON (`Block.digest`: keys sorted, compact, UTF-8) and `section` the section it stands in. The route serves
only the block of that section with that digest and refuses everything else (404): there is no lookup by id or
position, so a block in another section (one no verdict binds), a reused id or an index shifted by an inserted block
can never fill a frame the page drew for another block. A block whose id another block also uses is refused (409).
Same auth as every dashboard route, answered with `render_document(...)` and the headers
`Content-Security-Policy: sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src
'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:; connect-src 'none'; form-action 'none';
base-uri 'none'; frame-ancestors 'self'`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`.

On the page the chrome holds a placeholder `<div class="w-frame" data-doc-url="/w/<ID>/<section>/<digest>?n=<nonce>"
data-nonce="<nonce>" data-min-height="<px>" data-title="…">` (a fresh nonce per render; the route puts `?n=` into
the document's `orch-frame` meta and answers 400 when it is not 8-64 of `[A-Za-z0-9_-]`; without `?n=` the document
gets a nonce of its own). The fallback inside it shows until then; the host script (`static/widgets/orch-frames.js`,
loaded by the ticket page and the library with `script-src 'self'`) turns it into
`<iframe sandbox="allow-scripts" src="…">` (never `allow-same-origin`, `allow-top-navigation`, `allow-popups`,
`allow-forms`). Core types never use a frame: they are server HTML inline in the page. The document:

1. `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline';
   style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:; connect-src 'none';
   form-action 'none'; base-uri 'none'">`
2. the theme tokens as CSS variables (light/dark, following the host's theme),
3. orch-kit (inline), 4. each vendored library the template lists (inline), 5. the data as
   `<script type="application/json" id="orch-data">`, 6. images the data names, as `data:` URIs (digest-checked),
7. the widget body.

The dashboard's own CSP (`frame-src 'self'`) is unchanged; the `sandbox` directive in the frame's own header gives
the document an opaque origin, so it reaches neither the dashboard's cookies nor its DOM.

The renderer plugs in through `orch.widgets.frames` (read at call time): `INSTALLED: bool`, `fallback(block, ctx) ->
str` (HTML inside the placeholder), `document(block, ctx) -> str` (the whole frame document) and `text(block, ctx)
-> str | None` (a text alternative known server-side: the caption, until the frame posts `orch.text`). The shipped
hook draws: it resolves `name@v` (workspace templates, then built-ins) or the one-off artifact, checks the block,
inlines the libraries the template's widget.json (or the one-off's `libs`) names and the pinned images, and
assembles the document with `orch.widgets.assemble.assemble`. A hook replaced with `INSTALLED = False` makes the
chrome say "agent HTML renderer not installed".

`GET /w/preview/<name>@<v>` serves a template filled with its own example.json, with the same headers and `?n=`
rule; the library page (`/widgets`, in the menu) frames it. The library lists core types (drawn inline from
`EXAMPLE`) and templates grouped by moment, with use counts per version, the tickets that use each, "unused for 30
days", the latest version's schema as a field table and every version's notes.

On the page the frame's posted text fills the chrome's text slot (the **Show text** toggle); when the frame stops,
fails or misses the 3 s ready deadline the toggle opens. A frame that loads a second time navigated itself (a link,
a form, `location = …`): the host removes it and shows the text with "The widget tried to leave the page". A ticket
page draws its first 40 blocks; the rest are shown as code with a note.

### Risk

What the frame does not stop, by design or for lack of a browser control:

- **Navigating itself.** CSP has no `navigate-to`; `sandbox` without `allow-top-navigation` keeps the top page, but
  the frame can still navigate its own document (to any URL, carrying data in it). The host tears the frame down on
  its second load, after the request has gone out.
- **Beacons without fetch.** A navigation, or a prefetch/DNS hint, can carry data out even with `connect-src
  'none'`; so can WebRTC (`RTCPeerConnection` ICE candidates are not governed by CSP). What a frame can read is its
  own document: the block's data and the images it names, nothing of the dashboard.
- **`widgets.html` is the human's setting, signed.** The config value alone never turns agent HTML on: a signed
  ledger entry must back it (see "The frame"). The guard also refuses an agent's edit of the key, as defence in depth
  only. The ledger has the limit stated in `orch.core.ledger`: it is not OS isolation, so same-user code outside the
  guard's view can read the files it uses; it makes turning the setting on a deliberate act against files outside the
  repository rather than an edit of the config, and `orch check` reports a config the ledger does not back.

Turn agent HTML off (`widgets.html: false`) where these matter: tickets then show each widget's text alternative.

### orch-kit (≈3 kB, `static/widgets/orch-kit.js`)

```
orch.data                 // the validated data object
orch.theme.var(name)      // a token value, e.g. '--ok-mark'; orch.theme.dark: boolean; 'orch:theme' event
orch.text(str)            // the text alternative (required); posted to the host
orch.ready()              // host hides the spinner; must be called within 3 s or the host shows the text
orch.resize()             // auto via ResizeObserver; manual call allowed
orch.open(ref)            // ask the host to navigate to a ticket id or #anchor; host validates
orch.error(msg)           // host shows an error state
```

Host ↔ frame messages are `postMessage` with `{orch: 1, kind, frame: <nonce>, ...}`; the host checks
`event.source` is that frame's window and the nonce matches. Stop control: the host removes the frame and shows
the text. Frames load lazily (IntersectionObserver).

### Vendored libraries (`static/vendor/`, pinned, with licences)

`uplot`, `mermaid`, `vega` + `vega-lite` + `vega-embed` (interpreter build, `data.url` stripped), `plot`
(Observable Plot + d3), `diff2html`, `leaflet` (no tile server: shapes only). Each listed in
`static/vendor/MANIFEST.json` with version, licence, sha256 and size. Every one runs under the frame CSP with no
`unsafe-eval` (checked in headless Chrome with no CSP violation in the console; the pages are
`tests/fixtures/widget_libs/`, mermaid is the built-in template): Vega needs `vegaEmbed(el, spec, {ast: true, expr:
vega.expressionInterpreter})`, which also covers `calculate` transforms.

## CLI

```
orch widget types [--json]                      core types + templates, with moment and example
orch widget show <type|name[@v]> [--json]       schema, example, versions
orch widget list [--for <moment>] [--json]      templates: versions, uses (per version, tickets), unused 30 days
orch widget add <ID> --section <S> (--type T | --widget N@v | --html <artifact>) [--file data.json | --data '{..}']
               [--title ..] [--source ..] [--caption ..] [--id ..]   validates, fills digests, appends the block
orch widget check [<ID>] [--json]               widget-parse / widget-schema / widget-place / widget-digest / widget-drift
orch widget render <ID> [--text|--html] [--id <id>] [--out DIR]   text alternative or standalone documents
orch widget promote <ID> <artifact> --name N [--version-bump] [--moment M]   one-off → template v1 (or v<n+1>)
```

`orch widget add` writes Context, Current state, Verification or Findings (the sections `orch section set` writes
and widgets may stand in), as one JSON line, under the ticket's lock. `orch check` (whole workspace) also runs the
widget checks. `orch show --widgets` prints each block's text alternative after its section (`--json`: a `widgets`
list). Every command takes `--json`.

## Rendering API (Python)

```
orch.widgets.parse_blocks(section_text, section="", offset=0) -> list[Block]   # line = 1 + offset based
orch.widgets.ticket_blocks(ticket, raw=None) -> list[Block]             # every block, with section, file line, index
orch.widgets.validate(block, ticket, section=None, *, ws=None) -> list[Problem]  # digests only with ws
orch.widgets.check_ticket(ticket, *, raw=None, ws=None) -> list[Block]  # validate all + unique ids + 40-block cap
orch.widgets.render_html(block, ctx, *, bare=False) -> Markup           # chrome + body (core) or frame placeholder
orch.widgets.render_text(block, ctx) -> str
orch.widgets.render_document(block, ctx, *, chrome=True) -> str         # standalone HTML document (TIX, export, /w/)
```

`Block`: `section`, `line`, `raw`, `data` (or `error`), `index`, `problems`, `layer` ("type" | "widget" | "html"),
`key` (the page anchor: `id`, else the index), `digest` (the frame address). `Problem`: `code`, `message`, `level` ("error" | "warning": a changed file is not shown, the rest of the widget draws).
`ctx` is `orch.widgets.Ctx(ticket, ws, theme, html)`; `Ctx.of(ws, ticket)` reads `widgets.html` and the theme from
the workspace config. A core type module may also set `ALT = "numbers"` (the toggle reads "Show numbers"); its
`render_html` may return a plain string. Widget CSS classes are prefixed `w-`; `static/widgets/core.css` holds the
chrome and the role classes `w-r-<role>` (they set `--w-bg`, `--w-fg`, `--w-mark` from the role tokens).
`render_document` returns, for every layer, one self-contained HTML document with the same CSP as the frame
(core types: their HTML + the needed CSS inline, no script). `chrome=False` leaves a core type's title, chip, text
alternative and source out, for a host that draws them around the frame (`ctx.ticket_widgets` does this); agent
HTML documents never carry chrome. Every document's tokens CSS inlines the dashboard's Figtree and Manrope
(`static/fonts`, about 60 KB as `data:`), so a frame draws in the same type as the dashboard.

## Widgets on a wiki page

The wiki addon's local pages draw ```orch fences like a ticket does (`Markdown(text, here, pages, widgets=True,
files=<wiki folder>)`; `GitHub wiki` pages are not drawn in orch, they open on GitHub, so they are unchanged). The
fence rule, the strict JSON, the schemas, the chrome (title, layer chip, source, **Show numbers / Show text**), the
error callouts and the sandboxed frame are the ticket's: the same code, with a page in place of a ticket
(`orch.widgets.pages`: `PageTicket`, `Ctx.ticket.is_page`). What differs:

- **Placement.** A page has no gate and no hashed section, so no block is refused for where it stands (no
  `widget-place`), also not under a `## Requirements` heading or before the first heading. Fences nested in a list item
  or another block stay code, as on a ticket. 40 blocks per page; `id`s are unique per page (a reused id is drawn as an
  error and never served to a frame).
- **Errors.** An invalid block shows the error callout with the page's file path and the line **in that file**
  (front matter included): `orchestrator/wiki/decisions/X.md, line 12`. The rest of the page draws.
- **Frame address.** `GET /wp/<addon>/<digest>?page=<page id>&n=<nonce>`: the sha256 of the block's canonical JSON,
  as on a ticket, plus the page. Core asks the addon for that page's text (`page_source(page_id) -> (text, folder)`,
  the cache the page was drawn from, never the live file) and serves only the block with that digest, with the same
  headers (`sandbox allow-scripts`, no-network CSP) as `/w/`; 404 for anything else, 409 for a reused id, 400 for a bad
  nonce. The route is exempt from the dashboard's page CSP the same way `/w/` is.
- **Files.** A block names `_files/<name>` in the wiki folder plus its `sha256` (flat: no subfolders). The folder is
  resolved again by core with the addon's rule (inside the workspace, not hidden, not orch's records, no symlink on
  the way); the file must be a regular file directly in `_files/`, never a symlink, and `_files/` itself never a link.
  Missing, outside the folder, a symlink or `artifacts/<ID>/...` all give the "missing" state; a wrong digest the
  "changed since this widget was written; it is not shown" warning, as on a ticket.
  A page has no `/a/` route, so core types (`compare`, `screens`, `video`) show a page's files through
  `GET /wpf/<addon>/<sha256>?page=<page id>`: the route asks the addon for the page text, serves a file only when a
  valid block of that page pins a file of `_files/` with that digest, and only the bytes that hash to it (read once,
  hashed as read, `read_pinned`); images and video only, with `nosniff`, `no-store` and a `sandbox` CSP, and exempt from
  the page CSP like `/w/`. The page CSP's `default-src 'self'` lets `<img>` and `<video>` load them (so video plays and
  seeks); nothing is embedded as a `data:` URI on a page, so a page's size does not depend on its files. A page's
  URLs carry the digest, so a file swapped after the page was drawn is refused as well. The agent-HTML layer is
  unchanged: its frame document inlines the images its data names as `data:` URIs, within the same
  8 MB (`artifacts.MAX_INLINED`) budget per widget as on a ticket ("too many or too large images" beyond it), and a
  one-off `html` file runs only when its bytes match the pin. Standalone documents (`render_document`) embed up to
  5 MB per file, and one of more than about 9 MB is replaced by the "too many or too large images" error, as for tickets.
- **Links.** No link or image on a page is ever live toward a ticket's artifact: a `links`/`deploy` url or inline
  Markdown in a block that would be another ticket's `/a/` file is shown as text (R24 for pages).
- **A `checks` block** points at acceptance criteria (`AC<n>`) of a ticket, but a page has no ticket and no
  criteria. Decision: it is drawn read-only exactly as on a ticket (the rows are the claim the page's author wrote),
  with a note "A claim written on this page: not a gate, and not the ticket's verdict". It never gates, is never
  projected onto a ticket (`projection` reads only a ticket's Verification) and looks nothing up: the block has no
  ticket key, so the ticket's live state is shown on the ticket. `deps` and `links` need no ticket either: they draw
  from their own data (a `deps` node's `status` is a word the author wrote, not a lookup).
- **Search and mentions** read each valid block's text alternative (`render_text`) instead of its JSON, through
  `ctx.page_widget_text(text, page_id, folder)`; a block that does not parse stays as written.
- **Create page from ticket** copies the blocks of Verification and Findings (Summary and Requirements are gated and
  hold none) with their pinned files into `_files/<ID>-<name>`, digest-checked and read once; a block whose file is
  missing or changed is left out and the page says so (`ctx.ticket_widget_copy`). The wiki then **commits** the new
  files (an explicit exception to "orch never commits on its own", see the wiki addon's README).
- **Checks.** `orch widget check` (no ticket argument) and `orch check` also validate the blocks of the local wiki
  pages when the wiki addon is enabled here with provider `local`, reported as `<page path>:<line>`. Core reads the
  addon's saved settings, not its code.

## TIX

orch-tix reads `ctx.ticket_widgets(ref)` (the addon API; addons do not import `orch.widgets`; its documents are
chrome-less, since the PWA card draws title, chip, source and text) and, at redaction
`full` only, adds `widgets` (`[{section, index, key, layer, name, title, source?, text, doc? | file?}]`) and
`widgets_format: "orch.widgets.v1"` to the sealed doc. A core type's document rides inline (up to 128 KiB each,
512 KiB per ticket); agent HTML goes as its text only, unless the owner sets `sync_widget_docs = always`: then
as a 7-day context FILE (none with `sync_artifacts = never`), re-shared when the document (nonce aside) changes and
deleted when its block changes or goes, the ticket drops below full or is unlinked; every
document is dropped, the text kept, when the sealed doc would pass the server's 1 MiB. The PWA turns the k-th
` ```orch ` fence of a section into the k-th widget of that section: a card with the text by default; Show posts
the document to a `/sandbox/widget` frame (`sandbox="allow-scripts"`, its own header CSP — not `srcdoc`, which
would inherit the page's CSP), whose script `document.write`s it. The kit inside then talks to the page directly
(`window.parent` is the page) with the document's nonce; a core document without a kit gets its height and
`ready` from the frame script. No ready within 3 s, an error, or Stop shows the text again. Nothing is rendered on
the server; documents are built on the desktop before encryption.

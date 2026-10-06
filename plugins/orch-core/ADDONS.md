# Writing orch addons

This guide is written so an agent can follow it: "read ADDONS.md and the template, build an addon for X". Start from `addon-template/` (the `hello-status` example) and keep `orch addon check <folder> --strict` green. Before you draw anything, read `DESIGN.md`: which widget fits which data, the copy rules and the design warnings.

## When to write an addon

Write an addon when Mission Control should show data from an outside tool (code reviews, an issue tracker, a status page, a wiki, a data platform) or hand a human decision back to orch from somewhere else. Do not write one to change how tickets work: statuses, gates, questions and human-only rules belong to orch-core.

Addons come in two kinds. **Default addons** ship in the plugin's `addons/` folder, are trusted with the plugin version and stay off until a human enables them per workspace. **Custom addons** live anywhere, are installed with `orch addon install <folder | git URL>` into `~/.config/orch/addons/<name>/`, and run only after a human trusts that exact version.

## Layout

```
my-status/
  orch-addon.json        manifest
  README.md              what it does, settings, binaries (required)
  CHANGELOG.md           one section per version
  my_status/__init__.py  the entry package; relative imports only for its own modules
  my_status/provider.py
  tests/                 your tests, using orch.testing (not scanned by the import check)
  tests/fixtures/*.json  recorded CLI output for FakeRunner
```

No `requirements.txt`, `pyproject.toml` or other dependency files: API 2 allows no third-party Python packages.

## Manifest `orch-addon.json`

| Key | Required | Value |
|---|---|---|
| `name` | yes | `^[a-z][a-z0-9-]*$`, not `changed`, `addons`, `core` or `orch`; also the folder name once installed and the URL `/addons/<name>/` |
| `title` | yes | shown in the menu and on Workspace & addons |
| `version` | yes | semver, e.g. `0.1.0`; bump it for every change you ship |
| `requires_api` | yes | `"2"` |
| `kind` | yes | `"in-process"` |
| `capabilities` | yes | subset of `provider`, `page`, `panel`, `decisions`, `settings`, `events`, `launch` |
| `entry` | yes | `package.module:factory`, e.g. `my_status:create` |
| `description` | no | one sentence |
| `slots` | with `panel` | slots you fill besides your page: `today.summary`, `today.from_addons`, `ticket.code`, `ticket.sync`, `ticket.external`, `ticket.pages`, `board.external`, `workspace.settings`, `guide.section` |
| `binaries` | no | commands `ctx.run` may start, bare names like `"gh"`, or `"setting:<key>"` for an absolute path the human saves in a text setting |
| `env` | no | environment variables passed through to those commands, e.g. `"GH_HOST"`; everything else is scrubbed |
| `menu` | with `page` | `{title, icon}`; icon is one of `box`, `code`, `issues`, `status`, `database`, `book`, `bell` |
| `settings_schema` | with `settings` | list of `{key, label, type: text|select|bool|map, options?, default?}`; `secret` fields are not allowed |
| `actions` | no | `[{id, label, confirm?, tickets?, idempotent?, accepts_file?}]`: buttons that change something outside orch; `tickets: true` lets `act` return `import`, `close` and `reopen` intents; `accepts_file: {max_bytes, types?}` lets the human attach a file (see Actions with files) |
| `ticket_options` | no | up to 3 yes/no choices on every ticket: `[{id, label, help?, default?}]` (see Ticket options) |
| `remote_actions` | no | action ids (each one declared in `actions`, no duplicates, at most 32) this addon allows from a paired remote device; default none, so a remote device cannot run any of your actions (see Actions from a remote device) |
| `remote_humans` | no | `true` only with the `decisions` capability: this addon may hand a paired phone's signed decision to core (see Remote human decisions) |

## The addon object

`entry` names a factory: `create(ctx: AddonContext) -> addon`. `ctx` gives `ctx.name`, `ctx.settings`, `ctx.root` (workspace root), `ctx.state_dir`, `ctx.records_dir`, `ctx.show(ref)` and `ctx.tickets()` (read-only copies: changing them changes nothing; write through `ctx.ops()`), `ctx.ops()` (see below) and `ctx.provider_context()`. The factory runs while no command may run (`ctx.run` refuses), so only store `ctx` and build your providers there. The addon object defines what its capabilities need:

| Capability | Define |
|---|---|
| `provider` | `providers`: a list of providers (next section) |
| `page`, `panel` | `widgets(slot, view) -> list[Widget]`; the page slot is `page.<name>` |
| `page` with a `menu` (optional) | `menu_badge(view) -> Badge | MenuStatus | None` (API 2.5): a status chip and a short status line on your sidebar entry |
| `decisions` | `decisions(view) -> list[PendingDecision]` and `resolve(id, choice, ctx: ProviderContext) -> TicketIntent | str | None`; optional `on_intent_result(id, outcome, message)` |
| `events` | `on_event(event, outbox)` and `drain(ctx, items) -> list[acked ids]` |
| `launch` | `launch(req: LaunchRequest, ctx: AddonContext) -> LaunchPlan | None` (API 2.7); optional `launched(req, routing, ctx, session)` |
| (actions) | `act(action_id, target, ctx: ProviderContext) -> TicketIntent | FileResult | Reveal | str | None` for each action in the manifest |
| (remote, with `remote_humans: true`) | `pairing_target(view) -> PairingTarget | None` |

`ctx.ops()` returns `AddonOps`: `log(ref, text)`, `set_extra(ref, "x-<name>…", value)`, `link_external(ref, key)`, `import_external(key, title)` (idempotent by key), `add_artifact(ref, path, name=None, *, context=False)` (see Artifacts). It has no answer, approve, verdict, move or edit: addons never act as the human.

### Workspace data

- `ctx.repos()` (also `view.repos()`): `RepoRef(name, role, path, default_branch)` for the harness root (role `harness`) and each `git.repos` entry (role `sub-repo`), from config only. Use the names as provider scopes. Run git yourself with `ctx.run(["git", "-C", str(repo.path), ...])`.
- `ctx.trackers()` (also `view.trackers()`): `TrackerRef(prefix, pattern, url)` with `matches(key)`, `key_for(native_id)` (`GH-12` for `GH-(?P<id>\d+)`) and `url_for(key)`.
- `ctx.document(ref)` (also `view.document(ref)`): the ticket as its versioned schema document (`orch schema ticket --json`): sections, gates with their hashes, questions with their `hash` (the question hash), the task list, and the needs-you items with gate hash and verdict `round`. Read-only; use it instead of importing `orch.core`.
- `ctx.ticket_widgets(ref)` (also on the provider context): the ticket's ```` ```orch ```` widget blocks, each with its section, index, key, layer, name, title, source, text alternative and standalone `document` (`render_document`, None for a block with an error), `raw_sha256` and problems (docs/widgets.md). Read-only; for a companion that shows widgets elsewhere, such as orch-tix.
- `ctx.links()` (also `view.links()`): the core's link index. Never match ticket keys with your own regex. `for_review(url=, branch=, title=)` gives the local tickets of a PR. `for_external(key)` gives every local ticket that links an external key, as a list (more than one ticket can link the same key). `sync(key, category)` gives a `(ticket, "close" | "reopen")` pair for each linked ticket that disagrees with `category`, evaluated per ticket. `merge_order(ids)` orders tickets by `blocked_by`. Tickets come back as `LinkedTicket(id, title, status, external, prs, branches, blocked_by)`.
- `ctx.snapshots(provider=None)`: your cached snapshots, the same data the pages show. Check an action's target against them before you act.
- `view.params` (see Widgets) carries a page's GET filters. `view.state_dir` is your addon's state folder, for small files of your own.
- Your state folder is local to one machine: orch's `.gitignore` block keeps everything in it out of git (snapshots, cursors, inbox/outbox spools, locks), except `ctx.records_dir` (`state_dir/records/`). Put a file there only when it is a real record every clone of the workspace needs (for example which remote decisions were linked to which ticket). It is committed to the workspace's repository with the tickets, so never put secrets, tokens or anything you can fetch again there. `*.lock` files stay ignored even in `records/`.
- `COMMIT_HOOK_HEADER` (from `orch.addons.api`): the marker line of orch's commit-msg hook, to show whether a repo has orch's commit check.

The default addons `addons/github-reviews` and `addons/github-issues` are complete examples of all of this.

## Providers and snapshots

A provider has `id` (`^[a-z][a-z0-9-]*$`), `kind` (`reviews`, `issues`, `status`, `pages`), `interval_s` (default 300), `scopes(ctx) -> list[str]` (for example one per repo) and `fetch(ctx, scope, previous) -> Snapshot`. Fetches run only in the background scheduler of `orch serve`: while a Mission Control tab is open, or after the human presses Refresh. Results are cached in `orchestrator/.state/addons/<name>/`. Pages read only that cache.

`Snapshot(provider, scope, fetched_at, health="ok", message="", complete=True, retry_after=None, me=None, items=())` with an aware UTC `fetched_at` (`ctx.now()`). Health is one of `ok`, `stale`, `auth_required`, `offline`, `rate_limited`, `error`, `never_fetched`. Return a health instead of raising. Set `retry_after` with `rate_limited`, and put the login command in `message` with `auth_required`. If you raise, or return no items with a failing health, orch keeps the previous items and shows them as stale.

### Background and long-poll providers

A provider is fetched only while a Mission Control tab is open, or after the human presses Refresh — unless it sets `always_on = True`, which also fetches it without an open tab, but only once the human switches on **"Keep syncing while Mission Control runs"** for that addon in that workspace (Workspace & addons; there is no CLI for this switch). Set `mode = "long_poll"` to have orch fetch the provider again right away after a good fetch (for a provider whose own `ctx.run` blocks until something changes); each `ctx.run` call it makes is capped at 35 s, and the whole `fetch` at 40 s, whatever timeout you pass.

Item formats. Datetimes are ISO 8601 strings with an offset, and other fields are passed through:

- **ReviewItem** (`reviews`): `provider, host, repo, number, url, title, state (open|merged|closed), draft, review (none|required|approved|changes_requested), checks {state, …}`; optional `native_id, author, source_branch, target_branch, head_sha, approvals, requested_reviewers, my_role, mergeable, unresolved_threads, additions, deletions, changed_files, labels, created_at, updated_at`. orch links tickets itself (branch pattern, trackers).
- **ExternalItem** (`issues`): `tracker, key, url, title, category (todo|in_progress|done)`; optional `native_id, type, raw_status, resolution, priority, rank, assignee, reporter, assigned_to_me, labels, sprint {name, state, start, end}, parent_key, estimate, due, created_at, updated_at`.
- **PageItem** (`pages`): `provider, space, id, title, url, path, updated_at`; optional `author, excerpt, links (ticket keys), documents (repo-relative globs)`.
- **StatusItem** (`status`): `id, label, role (ok|info|warn|err|neu), text`.

## Widgets and slots

Return widgets from `orch.addons.widgets`, never HTML or strings: `Card(title, body, role?, href?, layout?)` (`layout="grid"` puts its child Cards side by side as compact cards), `KV(rows, layout?)` (`layout="stats"` draws a few counts as big numbers over small labels), `Chips(items, label?)` (a wrapping row of Badge, Link and Text; its Links are filter chips, `Link(..., current=True)` marks the one shown), `Table(columns, rows, empty?, key=0)` (`key` is the column that names a row: the title of a stacked row and the sticky column; long text cells wrap, the rest stay on one line; by column count a table turns into stacked rows in a narrow container, see DESIGN.md), `Callout(role, title, text?)`, `Badge(role, text)`, `Link(text, url, current?)`, `Copy(label, text)`, `Action(action, label, target, quiet=False)` (`quiet=True` for the lesser action of a row or card; consecutive Actions share one row), `Text(text)`, `Markdown(text, here="", pages=(), widgets=False, files="")` (API 2.2, `widgets` and `files` 2.3: a block of Markdown such as a wiki page, up to 256 KiB, drawn by core with the renderer ticket text uses: raw HTML in it is shown as text, never as markup, links follow the same R24 rules, and it never has a ticket's artifact scope: `/a/...`, `artifacts/...` and `artifact:` links and images are shown as text, since no gate hash covers addon text; with `pages`, the ids of your pages, and `here`, the id shown, a relative link such as `other.md`, `sub/other` or `../other.md#part` that resolves, after one decode and dot segments, to a listed id opens `/addons/<name>/?page=<id>`, and one that leaves the folder or is not listed stays text), `Search(name, value?, placeholder?)` (a GET form on your own page; the value comes back in `view.params[name]`), `Tabs(items, label?)` (Links as same-page view tabs, `current=True` on the shown one; your own page only), `Time(at, style="ago"|"at")` (an ISO 8601 time with an offset or Z; core shows "2 min ago" or "03.10. 14:32" with the full time as tooltip; also as a table cell or KV value), `Tile(label, value, role, href?, sub?)` for `today.summary` only, `Chart(...)` (API 2.6, below), and `QR(text, caption="")` (see QR codes). Roles are `ok`, `info`, `warn`, `err`, `neu`. `you` is reserved for what needs the human, and addon items never count there. Badge text, Callout and Card titles and Tile labels must not be empty. Links must be `http(s)://` or a same-origin path. Ticket keys of the workspace (`DEMO-0004`) in Text, Callout text and table cells are linked to the ticket by core. orch renders widgets with autoescaping and adds the health line: a muted "Updated 4 min ago" when fresh, "Last updated 10:42" with a warn icon when stale (your data stays readable below it), quiet lines for "Rate limited · resets 14:05" and "Not fetched yet", and one callout at the top of your page for login needed, offline or a failed fetch. For login needed, end the Snapshot's `message` with "run <command>" (`login needed: run gh auth login`) and core offers the command with a Copy button. `complete=False` adds "Showing part of the data". Don't draw your own freshness line. `value=None` in a Tile shows "unknown", which is not the same as 0.

**Changes in the design-system release (still `requires_api: "2"`).** The new fields and widgets (`Action(quiet=)`, `Table(key=)`, `Tabs`, `Time`) are optional, so existing addons keep working. One check is stricter: an empty or blank Badge text, Callout title, Card title or Tile label is now an invalid widget. Core drops that widget at render time (if it sits inside a Card, the whole top-level Card goes) and shows an err Callout "<addon> returned an invalid widget" in its place; `orch addon check` reports it as an error. Give every one of them words.

**API 2.2 (still `requires_api: "2"`).** `Markdown(text)` is new and optional; an addon that does not use it is unchanged. It needs the dashboard extra like every widget.

**API 2.3 (still `requires_api: "2"`).** `Markdown(text, here, pages, widgets=False, files="")`: with `widgets=True` core draws the page's ```orch blocks like a ticket (docs/widgets.md, "Widgets on a wiki page"); `files` is your page folder, workspace-relative, where blocks name `_files/<name>` by digest. For the frame of an agent-HTML block core calls your addon object's optional `page_source(page_id) -> (text, folder) | None` (the text your page was drawn from). `ctx.page_widget_text(text, page_id, folder)` returns a page's text with each widget block replaced by its text alternative (for search), and `ctx.ticket_widget_copy(ref, sections)` the blocks of a ticket's sections with their pinned files, digest-checked, ready for a page of yours. All three are optional; an addon that does not use them is unchanged.

**API 2.4 (still `requires_api: "2"`).** `RemoteResult.code` is new: every result core returns carries one of the codes below, and an addon that ignores it is unchanged (a `RemoteResult` you build yourself, as in `FakeRemote`, may leave it `""`). `orch.remote.verify.CODES` lists them.

| code | status | meaning |
| --- | --- | --- |
| `applied` | applied | the decision was applied as the human |
| `malformed` | pending | not a version 1 decision, a field of the wrong type, or a target or ticket request that does not check out |
| `kind-not-allowed` | pending | a phone cannot decide this kind (move, close, import ...) |
| `not-paired` | pending | the phone is unknown, revoked or paired with another addon |
| `bad-signature` | pending | the signature did not verify |
| `implausible-time` | pending | the decision's time is missing or too far in the future |
| `kind-switched-off` | pending | the owner switched that kind off in the workspace settings |
| `question-not-found` | pending | the ticket has no such question |
| `refused-retry` | pending | core could not apply it for a reason that may pass later; `message` says which; not ledgered |
| `refused-final` | stale | a check core runs for every human refused the change (for example a validation or transition rule, or a ticket request whose text core refuses); `message` says which; ledgered, final |
| `too-old` | stale | older than 14 days; never applied automatically |
| `changed-since` | stale | the question, gate text, plan or verdict criteria changed after the phone showed them |
| `wrong-status` | stale | a verdict for a ticket that is not in testing |
| `wrong-round` | stale | a verdict signed for an earlier testing round |
| `superseded` | superseded | another phone decision on the same target was applied first |
| `already-approved` | superseded | the gate was already approved |
| `answered-locally` | answered-locally | the question was answered on the desktop first |
| `already-handled` | duplicate | this decision id was handled before |
| `no-such-ticket` | unlinked | the workspace has no such ticket |

Every `pending` code (`malformed`, `kind-not-allowed`, `not-paired`, `bad-signature`, `implausible-time`, `kind-switched-off`, `question-not-found`, `refused-retry`) means nothing was applied or ledgered, and the same decision may apply later (after pairing again, switching the kind on, or on the desktop); every other status is final. A later minor version may add a code: treat an unknown one by its `status`.

`widgets` runs during page renders: read `view.snapshots(provider_id)`, `view.settings`, `view.ticket` (for `ticket.*` slots), `view.params` (the cleaned GET query, on `page.<name>` and `board.external` only), `view.workspace_name`. Never run commands there (`ctx.run` refuses while a page renders).

### QR codes

`QR(text, caption="")` draws a QR code as an inline SVG, server-side, from a module matrix — never from addon markup, and never JavaScript or an external image URL (dashboard extra only, needs `segno`). `text` is capped at 1000 UTF-8 bytes; if it is too long to encode, orch shows a "QR code not shown" Callout instead of failing the page. Use it to show a link or a short code a human can scan, for example a pairing link.

**API 2.5 (still `requires_api: "2"`).** `menu_badge(view) -> Badge | None` is new and optional: an addon with a `page` and a `menu` may show one status chip (`Badge(role, text, title="")`, `ok`, `warn`, `err` or `neu`) next to its sidebar and phone-menu entry, e.g. "39 %" in amber. `view` is the read-only view widgets get (snapshots only, no `ctx.run`); `title` is the tooltip and the chip's accessible name. Return `None` for no chip. An exception or any other return value also shows no chip and is logged; the page never fails. `menu_badge` may instead return `MenuStatus(badge=None, line=())`: the chip plus a short muted line under the entry's label (one line, ellipsis) of `Badge` parts (small coloured text), `Text` and `Countdown(until, done="reset")`, an ISO 8601 time with Z or an offset drawn as "3h05" / "12 min" / `done` and refreshed every 30 s by core's page script (the server text stays without JS). Parts of any other type, empty texts and bad times are dropped and logged. An addon without the method is unchanged.

**API 2.6 (still `requires_api: "2"`, additive).** `Chart(title, labels, series, style="bar", stacked=False, unit="", x="category", horizontal=False)` draws a bar or line chart: `series` is a tuple of `ChartSeries(name, values, token="", stack="")`, one number per label (`style` is `"bar"` or `"line"`; `kind` is every widget's type name). Core draws it with its vendored Chart.js, in the theme's colours (`token` names one, `series-1` to `series-8` or a role mark such as `ok-mark`; empty takes the next series colour in order, never repeating one), with a legend for two or more series and tooltips, redrawn when the theme changes and without animation under reduced motion. `title` may be `""` (no heading, for a chart inside a Card that names it; the figure keeps an aria-label from its series). `x="linear"` takes numeric labels and spaces them by value; `x="time"` takes epoch seconds and draws them as clock times in the browser's local zone (HH:MM within a day, dd.MM HH:MM across days); `stacked=True` stacks the series, or only the ones that share a `stack` name; `horizontal=True` turns a bar chart on its side. Numbers only (no `None`, NaN or text), at most 8 series and 400 points, `unit` at most 20 characters, labels and names at most 200. Every chart also writes its numbers as a table under "Show the numbers", so it reads without JavaScript and for a screen reader. A Chart is allowed wherever a Card body is, and `orch addon check` reports a malformed one like any other widget. An addon that does not use it is unchanged.

**API 2.8 (still `requires_api: "2"`, additive): menu rows.** `MenuStatus(badge, line, rows=(), stale=False)` may carry `rows`, each a `MenuRow(label, value, meter=None, role="neu", note=(), muted=False)`, for an entry that reports a pair of values ("5h 1 % resets in 4h49", "Week 59 % resets Mon 09:00"). Core draws them in one grid under the label: the label, a small meter (`meter` 0 to 100, clamped; `None` draws "label — value"), the value in the role's colour (`ok`, `warn`, `err` or `neu`) and, on its own line, a `note` of `Text` and `Countdown` parts. `muted=True` greys one row (a window that has reset); `stale=True` greys them all, so say how old the data is in `line` ("as of 13:52"). Where the rows fit, the chip is hidden and the entry takes its `title` as tooltip and accessible name, so give the chip a label ("Week 59 %"); in a menu too narrow for the rows, the chip shows instead. `Countdown(until, done, prefix="")` draws `prefix` before the countdown ("resets in 3h05") and drops it once it shows `done`. Bad rows are dropped and logged. A MenuStatus without rows is unchanged.

**API 2.7 (still `requires_api: "2"`, additive): the `launch` capability.** An addon may shape a Start agent preview and launch without being able to start anything or change a ticket. For every preview and every launch of a session (a terminal or Terminals alike) core calls `launch(req, ctx)` on each enabled, trusted addon that declares `launch`, in addon name order. `req` is `LaunchRequest(ticket, mode, harness)` (mode `refine`, `work`, `fix-checks` or `continue`; harness `claude`, `copilot`, `codex` or a name from launch.json); `ctx` is your `AddonContext` (`ctx.settings`, `ctx.state_dir`, `ctx.ticket_option(ref, id)`, `ctx.show(ref)`). It must only read: no commands, no writes, no network. Return `None` (or a plan with nothing in it) to leave the start alone, or a `LaunchPlan`:

| Field | Meaning |
|---|---|
| `model` | a model name; core puts the harness's model argument group in front of the prompt (`--model <model>` for claude; the other built-in harnesses have none and refuse a model). A bare `--model` with its value dropped would swallow the prompt, so the group is added or omitted as a whole. Letters, digits and `. _ [ ] : / -`, at most 64 characters |
| `env` | variables for the launched process only, never the dashboard's: names `ORCH_*` or `CLAUDE_CODE_*` (upper case), values of letters, digits and `. _ [ ] : / + @ = -`. Core runs the command through `env NAME=value ... <command>`, so it also reaches a tmux-launched process; Windows Terminal cannot carry it and refuses the start with a sentence |
| `note` | one sentence (at most 400 characters, no control characters, not starting with `-`) appended to the prompt as its own paragraph; never ticket text |
| `label` | short text kept with the session and shown on its Terminals tile as "Started on ..." (stored in the state folder's `run/launches.json`, newest 200) |
| `reason` | one line for the Start agent box: why this plan |
| `warnings` | one-line sentences for the Start agent box |

Core validates every field (a value that breaks a rule is an error, never trimmed) and merges the plans of several addons: the first addon by name wins `model`, `label` and `reason`, `env` keeps the first value of each name, notes and warnings are joined. The preview and the launch use the same resolver, so the command shown is the command run; with no addon that declares `launch`, nothing differs from before. In a preview an addon that raises shows its message as a warning; at a launch it stops the start with that message (an addon that was switched on to choose a model is not skipped silently). After core started the session it calls `launched(req, routing, ctx, session)` (`routing` is core's merged plan: `model`, `env` as pairs, `note`, `label`, `reason`; `session` is the name of the tmux session or ticket key), best effort and logged on failure, for one-shot state: for example the `model-routing` default addon turns off a ticket's one-shot "next start" choice there with `ctx.ops().relay_ticket_option`. A session whose start failed is not reported. `orch addon check` requires a `launch()` method for the capability.

**A section on the How it works page (`guide.section`, additive).** An addon with the `panel` capability and `guide.section` in `slots` may add a short section to `/guide`, under "Addons that meet the work where it is". Return one `Card` (title = the section heading) with `Text`, `Chips`, `Link` and `Callout` children, in plain words about what the addon does for a person: it is explanation, not a status page (no tables, no Actions, no data from a provider). Core renders it like any other addon widget (autoescaped), shows it only while the addon is enabled and trusted in that workspace, and draws no health banner. An addon with `remote_humans: true` that fills this slot replaces core's generic phone paragraph on the page. `widgets("guide.section", view)` runs during a page render: no commands, no network.

**A yes/no choice on every ticket (`ticket_options`, additive).** An addon may declare up to three boolean options in its manifest, e.g. `{"id": "notify", "label": "Notify my phone about this ticket", "help": "Off: the ticket still syncs.", "default": false}`. For an enabled, trusted addon core draws each one in three places: a checkbox on the new-ticket form, a checkbox on the approve card and approve form for the requirements or plan gate (and "Approve requirements and plan"), and a card on the ticket page with a Turn on / Turn off button. Core stores the value per ticket in the addon's own state folder (`ticket-options.json`, local to this machine, never committed) and appends a `ticket.option` event (`{addon, option, value}`) that reaches your `on_event` like any other, so sync again when it changes. Read it with `ctx.ticket_option(ref, option_id) -> bool` (also on the provider context): the human's value, or `default` when never set. **Only a human sets it**: Mission Control (same token and same-origin check as every other action) and `orch addon ticket-option set <ticket> <addon>/<option> on|off`, which needs a terminal and a typed ticket id and is refused under an agent harness (the Claude guard denies it too). Agents may read: `orch addon ticket-option list <ticket> [--json]`. The one other writer is the addon itself for its OWN options, to relay a choice the same human made on a paired phone: `ctx.ops().relay_ticket_option(ref, option_id, value)`, recorded as `addon:<name>`, never as the human. An addon that is not enabled and trusted draws nothing and cannot be written to.

## Settings

Fields from `settings_schema` appear on Workspace & addons and are saved per workspace by the human. Read them with `ctx.settings` / `view.settings`. Never store a secret in a setting: read credentials from the tool's own login (`gh auth`, `databricks auth`, the OS keychain).

## Actions and pending decisions

An action changes something outside orch (rerun failed checks, mark a PR ready). Declare it in the manifest and show it with `Action(id, label, target)`. `idempotent: true` declares that running it twice is harmless; only such an action may drop its dialog with `Action(..., confirm="")`, any other falls back to the default confirm. orch renders a button that asks in an in-page dialog (a confirm page without JS; never a browser popup), calls `act(action_id, target, ctx)` only from a human POST (`target` at most 500 characters), and logs an `addon.action` event. A **PendingDecision** `(id, title, body, ticket, stale, choices, role, anchor=None, origin=None)` appears under "From addons" on Today: title at most 200 characters, body at most 2000, at most 6 choices with labels of at most 80. When the human picks a choice, orch calls `resolve(id, choice, ctx)` and logs an `addon.decision` event (with `origin`, a short text such as `phone`, when the item sets one). An item that sets `origin` is drawn on Today as its own card at the top, "From your phone · waiting for Apply" (`From <origin>` for any other text), with Apply and Ignore, instead of inside its question card. Mark stale items `stale=True`, and orch then refuses Apply.

### Decisions and intents

Both `resolve` and `act` get a `ProviderContext` (`ctx.run` allowed), never an `Ops`. To change something in orch, return a **`TicketIntent`** (an `Intent` from `orch.addons.api`, under the name the guide uses for it); orch checks it and runs it itself as the human:

```python
def resolve(self, decision_id, choice, ctx):
    if choice == "apply":
        return TicketIntent("answer", ref="L-0042", qid="Q1", value="yes", expected_hash=self._hash)
    return None  # "ignore": nothing changes
```

| `TicketIntent(kind, ...)` | Fields | Notes |
|---|---|---|
| `answer` | `ref, qid, value, reason?` | `reason` becomes the answer's note; comes only from `resolve` |
| `approve` | `ref, gate, expected_hash` | the gate hash of the text the human saw; a changed gate is refused; comes only from `resolve` |
| `request_changes` | `ref, gate, reason` | comes only from `resolve` |
| `verdict` | `ref, value (done|follow-up), reason, expected_hash` | the verdict hash the human saw (the ticket document's `verdict.hash`); follow-up needs a reason; comes only from `resolve` |
| `move` | `ref, value (status)` | comes only from `resolve` |
| `new` | `value (title), reason (the Ask)` | no `ref` (the ticket does not exist yet); only from `resolve()` of an addon declaring `decisions`; lands in backlog |
| `import` | `ref (external key), value (title)`, optional `data={"ask": <issue text>, "type": <ticket type>, "priority": <priority>}` (type and priority apply only when orch knows them; never an epic) | actions with `tickets: true` only; idempotent by key; the issue text (at most 20 000 characters) becomes the new ticket's Ask, neutralised so it can never open a section |
| `close`, `reopen` | `ref, reason` | actions with `tickets: true` only; close moves any status but done to done, reopen moves done to open (requirements still approved) or backlog; the reason is logged |
| `none` | `reason?` | changes nothing; same as returning a string (shown as the message) or `None` |

`ref` must be the decision's `ticket` (for `resolve`) or the action's `target` (for `act`); an intent about any other ticket is refused and nothing changes. A `PendingDecision.anchor` (`Q<n>`, `gate:requirements`, `gate:plan` or `verdict`) ties the card to one spot on the ticket: when it is set to a question, `resolve` may only answer that same question, even if the ticket gained more questions since the card was drawn.

An action declared with `"tickets": true` changes local tickets: its `act` returns an `import`, `close` or `reopen` Intent (for example `Intent("close", ref=target, reason="GH-13 is closed in GitHub")`), which orch runs as the human. Re-check the target against `ctx.snapshots()` and `ctx.links()` inside `act`, because the page may be stale, and raise `ValidationError` when it no longer applies.

Define `on_intent_result(id, outcome, message)` to hear back, in the same POST, whether the intent you returned from `resolve` was applied: `outcome` is `"applied"` or `"refused"`, `message` is what the human now sees. A raised exception there is logged and otherwise ignored.

### Actions with files

Declare `"accepts_file": {"max_bytes": N, "types": [".pdf", "image/png"]}` (`types` optional, extensions or MIME types, `()`/absent means any type; `max_bytes` at most 200 MiB) on an action to let the human attach a file. `act` then gets an extra `upload=Upload(path, name, size, mime)`: a private 0600 copy under `state_dir/addons/<name>/in/`, deleted once `act` returns, whichever way it returns. `act` may return:

- a plain `str` or `None` — shown as the message, nothing special happens;
- a `TicketIntent` — run as the human, same as any other `act` result;
- a **`FileResult(path, name, mime="application/octet-stream")`** — `path` must be a file inside your own `state_dir`; core moves it to `state_dir/addons/<name>/out/` and serves it once, as an attachment, with a single-use token good for 5 minutes (`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox`, `Cache-Control: no-store`);
- a **`Reveal(label, text)`** — a secret (a link with a key in it) shown to the human once on the same page, never logged, never cached, and never printed by a traceback (`repr(Reveal(...))` hides `text`).

### Actions from a remote device

A paired phone or other device that reaches the dashboard through the remote bridge may run an addon action only when the action id is listed in `remote_actions` and the device holds the Type scope; local use is unchanged. An unlisted action, and an action or addon that does not exist, are refused the same way before any of your code runs, so a refusal never says whether an action exists. Keep the list to actions that are safe for whoever holds a paired device. A file sent from a remote device to an action is limited to 25 MB (the local limit for `accepts_file` stays 200 MiB); a larger one is refused as too large for remote use and may be sent as a TIX file instead. Any other POST from a remote device is limited to 25 MB in total, and a ticket artifact upload to 10 MB. Downloads an action returns are not changed.

`remote_actions` makes only your *actions* opt-in. Your code still runs for a remote device through other routes, each held to its own scope: the decisions route (`/addons/<name>/decisions`, Operate; the intent your `resolve` returns is checked afterwards against the same scope, phone switch and factory rules), the addon page (a read, Look; it renders your widgets), refresh (Operate; it only queues a refresh for the scheduler) and the one-time file download of a `FileResult` (Operate, unchanged). The ticket-options route (`/t/<ref>/option`) was not examined for this change.

These checks guard an honest addon. Addon code is trusted and is unsandboxed (it runs in the dashboard process), so they do not contain a hostile addon: only trust addons you have read.

## Events and the outbox

With `events`, orch calls `on_event(event, outbox)` for each new orch event while `orch serve` runs. `event.seq` only rises (normally by one per event); lines of the log that break that order never reach `on_event`. Only enqueue there (`outbox.put({...})`, JSON values); `ctx.run` refuses. `drain(ctx, items)` runs in the background with `ctx.run` allowed and returns the ids it delivered; the rest are retried.

## Artifacts

`ctx.ops().add_artifact(ref, path, name=None, *, context=False)` copies a file your addon already has into the ticket's artifacts, as the addon's own agent (`addon:<name>`). `path` must be a plain file inside your own `state_dir/addons/<name>/` (no symlink, no hard link, at most the same cap as an upload); orch opens it once and copies from that descriptor, so nothing can be swapped in between the check and the copy. `context=True` shows it inline on the ticket page instead of only in the artifacts list. The file is linked in the ticket's frontmatter `artifacts` list (kind guessed from its name, sha256 recorded) and the `artifact.added` event carries `name` and `kind`. The workspace's `artifacts.max_mb` (default 50) also caps it.

## QR codes

See QR codes under Widgets and slots.

## Remote human decisions

With manifest `remote_humans: true` (needs `decisions`), a human can pair a phone to your addon from Workspace & addons: a QR code and a "Copy pairing link" button, from whatever `pairing_target(view) -> PairingTarget | None` returns (read-only, runs while the page renders, like `widgets`). `PairingTarget(url, label)`: `url` must be `https://`, at most 500 characters; core appends the pairing key to it. Give the human something to scan or copy from your own page or widgets.

Your phone-side flow signs a decision and hands it to core with `ctx.remote_decision(decision)` (`ProviderContext`, raises `AddonRunError` if the manifest lacks `remote_humans`). Core alone verifies the signature, the pairing, the age and the per-kind permission, and applies it as the human — your addon never sees the key and never applies anything itself. It returns a `RemoteResult(status, message, ticket=None, event_seq=None, code="")` from `orch.remote.verify`, `status` one of: `applied`, `pending` (not applied: the phone is not paired or revoked, the signature or time did not check out, or the owner switched that kind off; you may offer it for the desktop), `stale`, `superseded`, `answered-locally`, `duplicate`, `unlinked`. `code` (API 2.4) is the stable, machine-readable reason: branch on it, never on `message`, which is human text and may be reworded (codes: see API 2.4 above). In tests, use `orch.testing.FakeRemote(RemoteResult(...))` in place of `ctx.remote_decision`.

Default per-workspace permissions (R21: a paired phone is the owner): `answer`, `request_changes`, `approve`, `verdict` and `ticket_request` all on, so a decision that passes every check applies at once, with no desktop step; the owner may switch a kind off on Workspace & addons. A phone never moves, closes, reopens or imports a ticket, and a decision older than 14 days is never applied (`stale`). These checks are core's, not yours. The signed approval ledger entry of a phone decision carries `via: "phone:<label>"` and `device: "<phone id>"`.

A `ticket_request` decision creates a backlog ticket: `{v: 1, decision_id, kind: "ticket_request", space, at, value: {title, body}, device, pair, mac}` with no `ticket` and no `target` (an optional `voice: {file, transcript}` object is signed along, not applied). `title` is 1 to 200 characters after collapsing whitespace, `body` (optional) becomes the Ask. Hand it to `ctx.remote_decision` like any other kind; the result's `ticket` is the new ticket's id. Unsigned, it stays `pending`.

Epic verdicts are desktop-only for now: core applies a phone verdict only to a ticket in testing (an epic's document still publishes its `verdict.hash`, with `round` null). A signed `verdict` decision's `target` must also carry `hash`, the ticket document's `verdict.hash` the phone showed (schema 1.3; core refuses a changed one as `stale`, and a decision without it stays `pending` with "update the phone app"), and `round`: the testing round it was given in (an integer, the event seq of the latest move into testing on that ticket — see Ticket schema). Core refuses it as `stale` when the ticket has moved through testing again since, even if nothing else changed; this stops a verdict a human signed on their phone before a follow-up from landing on the next round instead.

Requirements and plan together (schema 1.4): when a ticket's `needs` has `{kind: "approve-requirements", together: true}`, the agent drafted the plan too. A phone may then approve both in one signed `approve` decision whose `target` is `{gate: "requirements", hash: <gates.requirements.hash>, plan_hash: <gates.plan.hash>}`; show both texts in full first (each gate's `covers`). Core checks each gate as a desktop approval would and writes two approvals and two signed ledger entries; a changed plan or requirements text makes it `stale`. Without `plan_hash` an approval covers the requirements only, as before.

## Ticket schema

The document's `signed` object (schema 1.6, a required key; readers of 1.5 may ignore it; the addon API stays `requires_api: "2"`) says per approved gate and for a done verdict whether this machine's signed ledger backs it and who by (`{signed, by}`, see docs/ticket-schema.md); a mirror should show that, not the frontmatter's `approved`. `orch schema ticket --json` prints the ticket document's JSON Schema; `orch schema example` prints an example document in the current `schema_version` (semver, starting `"1.0.0"`; the export includes the MC2-T task list, `format: "orch.tasks.v1"`). Use these to validate what you read or to build a phone client without guessing the shape. A ticket's `needs` array has an item `{kind: "verdict", round}` while it is in testing: sign a verdict decision's `target.round` with that same number (also the document's `verdict.round`) and `target.hash` with the document's `verdict.hash`. A question's `question_hash` is `"sha256:" + sha256(canonical_json({id, text, why, type, options, recommended, blocking}))`, over the same `canonical_json` orch uses everywhere (`json.dumps(..., ensure_ascii=False, separators=(",", ":"), sort_keys=True)`); an `answer` or `approve` intent's `expected_hash` must match it, and a `verdict` intent's the document's `verdict.hash`.

## orch wait

`orch wait <ref> [--timeout S] [--after SEQ]` is agent-callable and read-only (no lock, no event, no human check): it blocks until the human answers, approves, requests changes or gives a verdict on that ticket, then prints the event and the new status. `--after` defaults to the agent's own last event on the ticket, so a decision already given before `orch wait` started is still seen at once. `--timeout 0` (the default) waits forever; any other value raises `WaitTimeout` once it elapses. Use it in an agent's loop instead of polling.

## ctx.run

`ctx.run(argv, timeout=20) -> RunResult(argv, returncode, stdout, stderr)`. argv is a list (no shell). `argv[0]` must be in `binaries`. The environment is scrubbed except for `env`. A missing binary or a timeout raises `AddonRunError`. Never import `subprocess`, `socket`, `urllib.request` or `http.client`, and never call `os.system`, `eval` or `exec`: `orch addon check` refuses them.

## Security rules

- Addons never act as the human and never get an `Ops`; human decisions come back only as Intents, which orch validates and executes inside a human POST. `answer`, `approve`, `request_changes`, `verdict` and `move` come only from `resolve` (the human saw the decision); an action's `act` may return only `none`, or `close`, `reopen` or `import` when the action is declared with `"tickets": true`.
- Widgets only; no HTML, no routes, no side effects when a page renders.
- argv-only `ctx.run` with allowlisted binaries; no direct subprocess or network.
- No secrets in files or settings.
- Imports: stdlib, `yaml`, `jsonschema`, `filelock`, and `orch.addons.api`, `orch.addons.widgets`, `orch.addons.runner`, `orch.addons.manifest`, `orch.clock`, `orch.errors`. No `_`-prefixed names from `orch` or from `ctx`/`view`/`ops` objects, no `__globals__`, `__closure__`, `__code__`, `__builtins__` or `__dict__`, no frame introspection (`tb_frame`, `f_globals`, `f_back`, `f_locals`), no `runpy`, `importlib.util`, `pkgutil.resolve_name`, `operator.attrgetter`/`methodcaller`, process pools, and no star imports.
- No symlinks anywhere in the addon folder: install and trust refuse them.
- Compiled files are refused: a `.pyc`, `.pyo`, `.so`, `.pyd` or `.dylib` anywhere in the addon folder (outside `__pycache__`) fails `orch addon check`, install and trust. `__pycache__` is deleted after a copy or clone, and orch compiles addon modules from the `.py` sources every time; it never uses cached bytecode. Ship sources only.

**The trust pin is the security boundary, not the API.** An in-process addon runs inside `orch serve` with full Python power and the user's permissions once it is trusted. The API (no human `Ops`, `ctx.run` only, widgets only) and the `orch addon check` lint only prevent accidents and make the contract explicit; they are not a sandbox, and determined code can get around them. What protects the user is that a human reviews and trusts one exact version (a hash of every source file), and any change disables the addon until it is reviewed and trusted again: in-process code is trusted by hash, nothing else. Admin is the human's at every level: `orch addon install|update|trust|enable|disable|rollback|remove` and the functions behind them refuse inside an agent harness, and the guard denies agents Python that calls them or writes to the orch config dir.

## Testing with orch.testing

```python
# tests/conftest.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orch.testing.pytest_plugin import orch_user_dir, orch_workspace  # noqa: E402,F401
```

- `FakeRunner.from_dir("tests/fixtures")` replays recorded CLI output (`{argv, returncode, stdout | stdout_json, stderr, raises: timeout|missing}`; `"*"` matches any one argument). Record real output once, for example from the demo repo.
- `orch_workspace` is a fake workspace (`fake_workspace(...)` for your own tickets). Use `.provider_context(manifest, runner=...)`, `.load(addon_dir)`, `.cache(name, snapshot)` and `.enable(name, config)`.
- `class TestProvider(ProviderContract)` with fixtures `provider` and `provider_ctx` checks ids, scopes, snapshots, item formats and that `fetch` uses only `ctx.run`.
- `class TestAddon(AddonContract)` with `addon_dir` (and `runner`) runs the static checks and the contract: capabilities match methods, widgets are valid for every health state, data is escaped, nothing runs commands during a render, `resolve()` returns a `str`, `TicketIntent` or `None` for each decision it offers, and `act()` (for actions without `accepts_file`) returns a `str`, `FileResult`, `Reveal`, `TicketIntent` or `None`, or cleanly refuses the contract's made-up target.
- `FakeProvider.each_health()` gives a provider per health state for page tests.
- `FakeRemote(result)` stands in for `ctx.remote_decision` in a phone-flow test: records every decision dict passed to it (`.seen`) and returns `result` (a `RemoteResult`) every time.

## Versioning and CHANGELOG

Bump `version` for every change you ship and add a `CHANGELOG.md` section. Installed addons that change are disabled until the human re-trusts them. The trust screen shows the old and new version, the changed files and any new capabilities, binaries, env vars or actions, so keep permissions minimal.

## Install, trust, enable, update

`orch addon install <folder | git URL> [--ref] [--path <folder in the repo>]` (`--path` installs an addon that lives in a subfolder of a larger repository, such as `addons/orch-tix`; `update` keeps using that folder), `orch addon trust <name>`, `orch addon enable|disable <name>` (current workspace), `orch addon update <name> | --all [--check]`, `orch addon rollback <name>`, `orch addon remove <name>`. All are human-only: they refuse inside an agent harness or without a terminal, and the guard denies them to agents. Trust, enable, settings and Check for updates are also on Mission Control → Workspace & addons. Each addon there is a card with its manifest `description`, where it shows up (from `menu`, `slots` and capabilities) and what it needs (its `binaries`, checked on PATH, and `env`), so write a description that says what a human gets; addons listed in `addons/external.json` appear under More addons with their install commands. `orch addon list` and `orch addon check` are open to everyone.

## Done checklist

`orch addon check <folder>` verifies items 1 to 7 (`--static` only 1 to 3, without importing your code); item 8 is yours. It also prints the design warnings from `DESIGN.md` as `△` lines; `--strict` makes them fail, and `--json --format v2` returns `{"errors": [...], "warnings": [...]}` (plain `--json` keeps the flat problem list; with `--json --strict` and no errors, that flat list holds the warnings and the exit code is 5). Run both before you hand the addon over.

1. `orch-addon.json` is valid: name (not reserved), title, semver version, `requires_api: "2"`, `kind: "in-process"`, known capabilities and slots, bare binaries, env names, `module:factory` entry, menu for a page, schema for settings, no secret fields.
2. The entry package has `__init__.py`; `README.md` exists; no dependency files; no symlinks; no compiled files.
3. Imports are allowed ones only; no `subprocess`, sockets, HTTP clients, `os.system`, `eval` or `exec`, no private orch names or introspection, outside `tests/`.
4. The factory runs; every capability has its methods; no MC2-1 hooks (`setup`, `cli`, `dashboard`, `instructions`, `pull`, `artifact_modes`).
5. Providers pass the ProviderContract with your FakeRunner recordings.
6. Widgets are valid for every slot and health state, and data comes out escaped; with `--strict`, they also pass the design warnings W1–W18 (`DESIGN.md`).
7. `decisions()` returns PendingDecision objects, and `resolve()` returns None, a string or a TicketIntent for the decision's own ticket; `act()` returns None, a string, a FileResult, a Reveal or a TicketIntent (or cleanly refuses); `on_event` only enqueues.
8. Your own tests pass: `uv run --project <orch-core plugin folder> pytest <folder>/tests`.

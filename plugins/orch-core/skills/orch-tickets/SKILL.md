---
name: orch-tickets
description: Use for any work with this workspace's local ticket system (orch) — creating, finding, showing, moving or logging tickets, asking the human questions, linking branches and PRs/MRs, adding artifacts, and filing follow-up tickets. Also use whenever a ticket key such as L-0042 or an external key such as ABC-123 comes up.
---
# Tickets (orch)

Tickets live in `orchestrator/tickets/<status>/`, one markdown file each; the folder is the status. Always go through `orch`: never move ticket files or edit their status, gates, answers or claims by hand. Add `--json` when you need to parse output.

## Lifecycle

`backlog` → human approves requirements (with the plan, when you drafted it during refinement: one confirm) → `open` → you claim → `in-progress` → you write the plan and the task list → human approves the plan (skipped for size `xs`; until then `orch task start` and `orch task done` refuse) → work the tasks down → every task done or skipped → `testing` → human verdict → `done`. A blocking question moves `in-progress` → `waiting` until the human answers.

You may: create tickets (they land in backlog), refine backlog tickets, claim, write sections, log, ask, link, add artifacts, move `in-progress` → `waiting` or `testing`, create and work tasks while you hold the claim (not `owner: human` ones).
Only the human may: approve, request changes, answer, give verdicts, tick `owner: human` tasks, take over an open agent task (`orch task edit <id> T3 --owner human`, logged as "taken over by you"; the task is then theirs to tick), move to `open`, `in-progress` or `done`, or back to `backlog`. orch tells you from the human by your harness's environment and by process ancestry; never try to look human (no stripped environment, no terminal wrappers), the guard and `orch check` both report it. Size and type are part of the requirements approval: once it is approved, leave them alone. The Log is append-only and never names the human as actor (`orch log` adds your lines). Questions for the human go through `orch ask`, never into Requirements or Plan text: a line starting with "Open question", "Question for the human/you" or a bare "TBD" value blocks the approval until the human overrides it. An approval or answer that is not in this machine's approval ledger (made elsewhere, before the ledger, or written by hand) stops claim, task start/done and testing: tell the user to review it with `orch ledger adopt <id>`, or to request changes if they did not make it. Never touch the ledger or its key in the orch config dir. A change request on the requirements or plan is cleared only by editing that gate's section (`orch section set`); a reply in the Log alone does not clear it. When a command exits 3 it is the human's turn: tell them the exact command (for example `orch approve L-0042 plan`) or point them to the dashboard. Do not retry.

## Commands

| Goal | Command |
|---|---|
| What should I work on? | `orch next` · `orch list --status open` · `orch list --mine` |
| What else touches this code? | `orch related <id> [--path src/x] [--json]`: done tickets that changed those files (their decisions), open tickets in the same files, files usually changed with them, linked tickets |
| Read a ticket | `orch show <id>` (ID, number or external key); with `--json` its `move` says whose turn it is (`who`: you, agent or nobody) |
| Create | `orch new --title "..." --type bug --size s [--external ABC-123] [--body-file ask.md] [--requirements-file r.md] [--acceptance-file ac.md] [--out-of-scope-file o.md] [--summary-file s.md]` (`## Requirements`, `## Acceptance criteria`, `## Out of scope`, `## Summary` in the body file go into those sections) |
| Follow-up | `orch new --from <id> --title "..."` |
| Epic | `orch new --type epic --title "..."` · child: `orch new --epic <epic> --title "..."` · `orch link <id> --epic <epic>` / `--no-epic` · `orch epic show <epic>` |
| Labels | `orch new --label customer:acme --label admin ...` · `orch label add <id> customer:acme admin` · `orch label remove <id> admin` · `orch list --label admin` (one word each: no spaces or commas) |
| Sprint | `orch sprint list` · `orch sprint current` · `orch new --sprint S1 ...` · `orch link <id> --sprint S1` / `--no-sprint` |
| Claim / release | `orch claim <id>` · `orch release <id>` |
| Write a section | `orch section set <id> Plan --file plan.md` (or `-m "..."`) |
| Task list | `orch task list <id> [--json]` (where you are, `next`, refs) |
| Create tasks | `orch task add <id> --file tasks.yaml` · `orch task add <id> "text" --ref file:repo/path --verify "cmd" --needs T2` |
| Work a task | `orch task start <id> T3` · `orch task done <id> T3 -m "evidence"` |
| Close otherwise | `orch task skip <id> T3 -m "reason"` · `orch task block <id> T3 -m "reason" --on Q2` · `orch task reopen <id> T3` |
| Old workspace formats | `orch migrate` (dry run, shows the diff), then `orch migrate --apply` (Proposal/Decisions, old artifact links, bare-number trackers, Plan checklists of worked tickets) |
| Handoff note | `orch state <id> -m "..."` (rewrites Current state, shown as the Handoff) |
| Log a step | `orch log <id> -m "..."` |
| Ask the human | `orch ask <id> --file questions.yaml` |
| Wait for the human | `orch wait <id> [--timeout S] --json` — block until the human answers, approves, requests changes or gives a verdict (agents may run it) |
| Link work | `orch link <id> --repo hub` (the ticket touches that repo) · `--repo hub --branch feature/x` · `--pr 22` (a number resolves in `--repo`, default the workspace repo) or `--pr <url>` · `--external TIX-17` |
| Worktree | `orch worktree add <id> --repo hub [--base develop]` (branch from `git.branch_pattern`, worktree at `.claude/worktrees/hub/<slug>`, both linked, harness files linked in) · `orch worktree remove <id> --repo hub` (keeps the branch; refuses uncommitted changes) |
| Evidence (files) | `orch artifact add <id> shot.png --ac 2 --inline --label "Login after the fix"` · `orch artifact add <id> report.html --task T3` (`--kind` screenshot, report, log, dataset, build, diagram, other; guessed when left out) |
| Evidence (links) | `orch artifact add <id> --url https://github.com/acme/app/actions/runs/123 --kind build --label "CI run"` (http/https only; never fetched) |
| Loose files | `orch artifact list <id>` shows what is linked and what is not · `orch artifact scan <id>` links files written straight into `orchestrator/artifacts/<id>/` |
| Hand over | `orch move <id> testing` (warns when a criterion has no evidence or no branch/PR is linked) |
| Health | `orch check` · `orch rules` · `orch feedback add --file …` (orch itself confusing or broken: once, then carry on; the human reviews it) |

Put scratch files in `orchestrator/temporary/`; files that must stay go in `orchestrator/static/`.

**Every file or URL you produce for a ticket goes in with `orch artifact add`**: screenshots, reports, logs, exported
data, dashboards, PR checks and CI runs. A URL or a `/tmp/…png` path written only into the Log or Verification is
invisible to the human; `orch check` and `orch move … testing` warn about it. Tie each artifact to what it proves
(`--ac 2`, `--task T3`); `--inline` with `--ac` also writes the Verification line that shows it.

**Show, not tell.** The human decides from the ticket page and the phone, often between two meetings: a screenshot of
the UI change, a before/after image, a small chart or diagram exported as PNG/SVG, or a Markdown table beats a
paragraph. Add the file, then show it in any section with `![what it shows](artifact:<name>)` (alt text required;
only the ticket's own artifacts render, web images stay links). An image shown in Requirements or Plan is part of the
approval: replacing it (`--replace`) asks the human to approve again. Keep the text around it short.

## Epics

An epic groups children (tickets whose parent is the epic); it has Requirements and Acceptance criteria but no plan, tasks or claim, and epics do not nest. When the human approves an epic they approve its charter: the epic and every child that is not done (whatever its status) with its requirements and plan as they were at that moment. A plan you write after claiming a child was not in that approval: tell the human to approve the waiting plans of the epic together with `orch approve <epic> plans` (one confirmation, one approval per child plan). Covered children are then claimed and worked to testing like any approved ticket. Editing a covered child's Requirements, Acceptance criteria, Out of scope, Summary or Plan takes it out of the approval ("changed since epic approval"): the human approves the epic again before you go on with it (`orch claim`, task start/done and the move to testing refuse until then). The same holds for any ticket whose approved requirements or plan you edit. A child you add later is not covered either. Once an epic is approved you cannot move tickets into or out of it (`orch link --epic`/`--no-epic` exit 3); create new children with `orch new --epic`.

Delegation is the human's choice when they approve the epic; you can never turn it on, widen or extend it. While `orch epic show <epic>` says it is active, a child you (an agent) created and refined (Requirements, Acceptance criteria, and a Plan when its size needs one; no open questions) may be approved with `orch epic auto-approve <child>`: once per child, within the shown limits (count and size; larger children and anything about the epic's own text need the human). Each auto-approval is recorded as yours and shown to the human. If the delegation is paused, the epic changed, or a limit is reached, the command refuses: stop and ask the human. Verdicts are always the human's.

When you create several related tickets, group them under an epic (`orch new --type epic`, then `orch new --epic <epic>`) and refine every child, plan included, before handing over: the human approves the charter once instead of each ticket on its own.

## Writing good tickets

A ticket file holds only the sections someone wrote; `orch section set` creates a section on first write. Use `###` for headings inside a section: orch refuses section text with a `## ` line outside a code fence (it would start a section of its own) or with a fence left open.

- **Ask** — the human's words (imported from the tracker on import). Never rewrite them; orch refuses agent edits. The one exception: an Ask you wrote yourself with `orch new` (no tracker key, never edited by the human) stays yours to clean up until the requirements are approved; if the human rewrote it, leave their words alone. Requirements and criteria never go into the Ask: the requirements gate reads only the sections below and refuses while Requirements or Acceptance criteria are empty.
- **Summary** — one to three plain bullets: what will be true afterwards, for the human reading the ticket in five seconds. Optional; no status, no progress. When it has text, the requirements approval binds it, so editing it later invalidates that approval.
- **Requirements** — what must be true afterwards, from the user's point of view. No implementation detail.
- **Acceptance criteria** — `- [ ]` checkboxes, each observable and testable ("`orch check` exits 0 on a clean workspace"), never "works well". They are AC1…ACn in order.
- **Out of scope** — name the tempting neighbours you will not touch.
- **Plan** — the approach, not a checklist. Progress lives in Tasks.
- **Tasks** — written only with `orch task`. One verifiable change each, stable IDs (`T1`…), at most one in progress. Refs: `file:<repo>/<path>[#L1-9]`, `static:<path>` (under orchestrator/static), `artifact:<name>`, `ticket:<id>`, `ext:<key>`, `url:<url>`, `section:<name>`, `ac:<n>` (n-th acceptance criterion), `q:<Qn>`. Add ` — label` after a ref for a short note. The format is described in `docs/tasks-format.md` of the plugin.
- **Current state** (the Handoff) — rewrite it (don't append) at the end of every session; the next session starts from it.
- **Verification** — evidence per criterion, one line each: `- AC2: curl returned 404 once, no retry (curl -i …)`; several criteria may share a line (`- AC1, AC3: …`), output goes indented below it. Lines without `AC<n>` are general evidence. A criterion counts as proven only with such a line, and the line must say something: a placeholder (`todo`, `TBD`, `n/a`, `?`) or a few characters is not evidence. Tick a criterion only after its line exists; orch refuses the tick otherwise, both through `orch section set` and in a direct edit of the file.
- **Context**, **Findings** — your notes; the dashboard shows them folded under "Agent notes", with your name.
- **Proposal**, **Decisions** — gone: approaches go into Context (or a question with options), decisions are the answered questions and approvals. A ticket file that still has them does not load until `orch migrate --apply` has moved them into Context.

## Questions file

```yaml
questions:
  - text: One backup repo, or one per environment?
    why: Decides the repo layout and the permissions needed
    options:
      - {key: A, label: One repo, cost: "simple, mixed permissions"}
      - {key: B, label: One per environment, cost: "three repos to maintain"}
    recommended: A
  - text: Should the job also alert on Teams?
    type: confirm
    recommended: yes
    blocking: false
  - text: Keep the old endpoint?
    options:
      - {key: A, label: "Yes", cost: "two APIs to maintain"}
      - {key: B, label: "No"}
```

Quote labels like `"Yes"`, `"No"`, `"On"` and `"Off"`: unquoted, YAML would read them as booleans. orch keeps the text you wrote for them, but a quoted label is unambiguous.

Types: `single` (default), `multi`, `confirm`, `text`. Questions are blocking unless `blocking: false`.

## Tasks file

```yaml
tasks:
  - text: Add the serverless environment spec to the bundle
    refs: [file:dbt-models/databricks.yml, ac:1]
    verify: databricks bundle validate -t dev
  - text: Switch the prod schedules
    needs: [T1]
  - text: Grant SELECT on finance.* to the job principal
    owner: human
```

IDs are assigned in file order from the next free number, so `needs` may name tasks the same file creates. Keys: `text` (required, one line), `refs`, `verify`, `needs`, `owner` (`agent` default, or `human`).

## Follow-ups instead of scope creep

When you notice something outside the approved scope, add it to `## Findings`, file it with `orch new --from <id> --title "..." --type bug`, and mention it in your report. Don't fix it now.

## Widgets

A widget is a small visual block, a ```` ```orch ```` fence holding one JSON object, that saves the human reading:
measured numbers, a verdict per criterion, the files a change touches. Default to one for Verification: a `checks` widget with one row per acceptance criterion. Add
`screens` or `compare` for a UI change, `stats` for measured numbers, `options` when you ask the human to choose, and
`callout`, `table` or `diff` when a warning or a list of changes reads faster than prose. Write prose only when none fits.

- **Where:** Context, Current state, Verification, Findings (and extra `## Headings`). Never in Ask, Summary,
  Requirements, Acceptance criteria, Out of scope, Plan (a gate hashes them), Tasks or Log: there it shows as code.
- **Add:** `orch widget add <id> --section Verification --type checks --data '{"rows": [...]}' --source "pytest -q"`
  (validates, pins artifact digests and the template version, appends through the normal section write). Run
  `orch widget check <id>` before you hand over. Never edit a template version a ticket already uses: that is drift,
  the widget stops drawing and a pending verdict goes stale. A changed template is a new version.
- **Cite the source, never invent numbers.** Every number comes from a command, a file or a measurement you name
  in `source`. No source, no widget.
- **Types:** `orch widget types --json` lists every type with its schema and an example; `orch widget show <type>`
  shows one. The seed types:

```
{"type": "text", "text": "The cache is **cold** after every deploy."}
{"type": "callout", "role": "warn", "text": "The migration locks the orders table for about 40 s."}
{"type": "stats", "source": "npm run size", "items": [{"label": "main.js", "value": "286 kB", "delta": -126, "role": "ok"}]}
{"type": "table", "columns": ["Route", "p95 ms"], "rows": [["/t/{id}", 41]]}
{"type": "chips", "items": ["src/auth.py", {"text": "sessions table", "role": "warn"}]}
{"type": "links", "items": [{"label": "Preview", "url": "https://…", "expect": "the new empty state"}]}
{"type": "checks", "source": "pytest -q", "rows": [{"ac": "AC1", "verdict": "met", "evidence": "returns 404", "ref": "tests/test_api.py::test_404"}]}
```

`checks` goes in Verification: `AC<n>` is the n-th acceptance criterion, verdicts `met`, `not_met`, `unproven`.
The dashboard shows the latest verdict next to that criterion as the agent's check; it never ticks one, and it is
not evidence: a criterion is proven only by a Verification line citing it. Common keys on every block:
`title`, `source`, `caption`, `id`. `orch show <id> --widgets` prints each widget's text alternative.

## Exit codes

0 ok · 2 usage or not found · 3 not allowed / human-only · 4 claimed by another session · 5 validation failed or gate missing · 6 ticket file broken (ask the human to repair it).

## Setup

If the repository has no `orchestrator/` folder yet, or the session start lists open setup items, use the orch-setup skill. Outside Claude Code (for example GitHub Copilot), install the command line once with `uv tool install "<path to the orch-core plugin>[dashboard]"`.

# Ticket task lists — format orch.tasks.v1

A ticket's work breaks down into the `## Tasks` section of its Markdown file, between `## Plan` and
`## Current state`. The section is written only through `orch task …` or the dashboard's task
buttons (a raw edit is validated the same way), and agents read it through `orch task list --json`,
never by parsing Markdown. TIX mirrors it read-only. This document is the contract for format
`orch.tasks.v1`.

## Grammar

```
- [<box>] T<n> <text>
  - <field>: <value>
```

A task line, then its field lines (two spaces, `- `, field name, `: `, value). Nothing else may
stand in the section.

| Box | State |
|---|---|
| `[ ]` | todo |
| `[/]` | doing |
| `[x]` | done |
| `[-]` | skipped |
| `[!]` | blocked |

Fields, in canonical order. Each appears at most once per task, except `ref`.

| Field | Value |
|---|---|
| `ref` | repeatable; `<kind>:<target>[ — label]` (see References) |
| `verify` | how the task is proved done (a command or a check), one line |
| `needs` | comma list of task IDs `T<n>` that must be closed (done or skipped) first |
| `owner` | `agent` or `human`; `agent` is the default and is never written |
| `why` | the reason; required for skipped and blocked tasks |
| `on` | what a blocked task waits for: a question `Q<n>`, a task `T<n>` or a ticket or external key (`DEMO-0042`, `ABC-77`) |
| `note` | the last `-m` of `orch task done` (the evidence) |
| `added` | `<stamp>[ after plan approval]`, e.g. `2026-10-02T09:14Z after plan approval`; set on tasks added after the plan was approved |

Task IDs are `T<n>` with `n ≥ 1`. Text and values are one line each; whitespace runs collapse to
one space.

**Parse slack.** The parser accepts what a hand edit in an editor such as Obsidian produces: blank
lines, `[X]`, CRLF line ends, fields in any order, field lines indented with a tab or two or more
spaces, `*` or `+` bullets, and upper-case field names or ref kinds. Values are normalised:
`q3` → `Q3`, `t4` → `T4`, `demo-0042` → `DEMO-0042`, `ac:02` → `ac:2`.

**Canonical render.** orch always writes the canonical form: `- ` bullets, lower-case boxes, field
lines indented by exactly two spaces in the order above, `owner: agent` omitted, no blank lines,
no trailing newline inside the section. `parse(render(tasks)) == tasks`, and the canonical form
round-trips byte for byte.

Unknown fields, free text and duplicate IDs make the section invalid (`orch check` error
`tasks-parse`, line number). So do an unknown box, a task without text, `T0`, a field given twice,
a field without a value and a malformed `ref`, `needs`, `owner`, `on` or `added`. Line numbers
count from the first line after the `## Tasks` heading (`Tasks line N: …`).

## References

| Kind | Target | Example |
|---|---|---|
| `file` | path relative to the workspace root; the first segment is normally a repo from `git.repos`; optional `#L12` or `#L12-40` | `ref: file:dbt-models/resources/jobs/gold/` |
| `static` | file under `orchestrator/static/` (task notes that must stay) | `ref: static:DEMO-0043/serverless-notes.md — init-script findings` |
| `artifact` | name in `artifacts/<ticket>/` | `ref: artifact:job-inventory.csv` |
| `ticket` | local ticket ID | `ref: ticket:DEMO-0042` |
| `ext` | external key matching `external_trackers` | `ref: ext:ABC-128` |
| `url` | http(s) URL only | `ref: url:https://docs.databricks.com/aws/en/jobs/run-serverless-jobs` |
| `section` | a section of this ticket | `ref: section:Context` |
| `ac` | acceptance criterion number (1-based checkbox in Acceptance criteria) | `ref: ac:2` |
| `q` | question ID | `ref: q:Q2` |

The first colon splits kind and target, so `file:dbt-models/x.yml#L4` and `url:https://…` both
work. An optional label follows after ` — ` (space, em dash, space).

A `file`, `static` or `artifact` path must be relative and may not contain `..`; an absolute path,
a drive letter or a `..` segment is a parse error, so existence checks and links never leave the
workspace. A `url` with any scheme other than `http` or `https` is a parse error too.

Glob refs are not supported: a `*` or `?` in a `file`, `static` or `artifact` path is a parse error
("glob patterns (* or ?) are not supported"). Name the file or the folder instead. The `exists`
check looks at that literal path only.

## Rules

- At most one task is doing at a time; `orch task start` refuses a second one.
- IDs are never renumbered and never reused, not even after a task is deleted by hand: the next
  ID is one more than the highest ID in the section or in any `task.added` event.
- `orch task done` takes `-m` as its evidence (stored as `note:`); `-m` is required when the task
  has a `verify:` line. `done` needs the task's `needs` closed. Done straight from todo is allowed
  and, for an agent task, logged as "done without start" (a human task needs no start).
- `orch task skip` and `orch task block` need `-m` (stored as `why:`); `block --on` names what it
  waits for and checks that the question or task exists.
- Done and skipped tasks are closed; they cannot be edited until they are reopened.
- Tasks change only while the ticket is in progress or waiting. An agent needs its own unexpired
  claim for every task write.
- `owner: human` tasks belong to the human: an agent may not start, tick, skip, block, reopen or
  edit them, and only the human changes an owner. The human may skip (with a reason) or reopen an
  agent's task but never tick it done, in the CLI, the dashboard and raw file edits alike.
- The one way for the human to tick an agent's task is an explicit takeover first:
  `orch task edit <id> T3 --owner human` on an open task, logged as "T3 taken over by you"; after
  that the task is the human's. A raw file edit is judged by the owner a task had before the save,
  so a save that adds `owner: human` and ticks `[x]` in one go is refused.
- Moving a ticket to testing needs at least one task and no todo, doing or blocked task, for every
  size including `xs`.
- `orch task list` on a Tasks section that does not parse prints the view with its `error` and
  `line` and exits 6 (broken ticket file).
- A blocking question asked while an agent task is doing blocks that task with `on: Q<n>` (a doing
  `owner: human` task is never blocked); once no
  blocking question is open, the answer restarts it (back to doing).
- A Plan checklist of a ticket written before task lists becomes tasks through `orch migrate`; the Plan
  text and its approval stay unchanged.

## Receipts (orch task done --run)

`orch task done <id> T<n> --run` proves a task with a receipt instead of a sentence. orch runs the task's `verify`
line itself, in the directory the agent stands in, and keeps what happened:

- A `verify` line `check:<name>` runs the workspace's named check: `checks.<name>.steps` in
  `orchestrator/config.json`, in order (`{"steps": [{"name": "build", "run": "npm run build"}, ...], "keep_going":
  false}`). What verification takes differs per project, so the project says it once; an agent picks the check by
  name and cannot change what it runs (the guard refuses an agent edit of `checks`). The human signs the checks
  with `orch checks sign` in their own terminal (a digest per check, in the approval ledger, like `widgets.html`);
  `orch checks` lists each as signed, changed (edited since) or unsigned, `orch check` warns about the last two, and
  a run of such a check still happens but its output, note and `gates` source say it was not signed. A `verify` line
  `cmd: <command>` is one step named `verify`. Any other line is prose (`deploy and one green run per job`) and is
  never run: `--run` refuses it. After a failing step the rest are skipped unless `keep_going`. The timeout covers
  the whole run: `--timeout`, else the check's own `timeout` (seconds), else 540 s, under the 600 s an agent
  harness allows one shell call. On a timeout, Ctrl-C or SIGTERM the step's whole process group is killed.
- Receipts and who added an artifact are orch's to write: `orch artifact add --kind receipt`, and replacing a
  receipt's file, are refused, and the
  guard refuses an agent's edit of the ticket file that adds or changes a receipt, its `run` or any entry's `by`.
- The receipt is an artifact of the reserved kind `receipt`, `receipt-T<n>-<UTC stamp>.log`: each step's command
  and output (the tail, within the artifact size limit). Its entry carries `run`: `exit`, `timed_out`, `commit`,
  `dirty`, `repo` (the checkout's folder name), `at`, `seconds`, `check` and `steps` (`name`, `run`, `status` pass|fail|skip, `exit`, `timed_out`,
  `seconds`).
- Verification gets a core `gates` widget with the id `receipt-t<n>`: a row per step with its status and time,
  the receipt and the commit as its source. The next run of the same task replaces it.
- The receipt's output is kept on disk while the command runs in a bounded temp file (twice the receipt's size cap
  at most; older output is dropped, and the receipt then starts with a cut marker).
- The task is ticked only when every step passed; its note names the receipt. A failing run keeps its receipt and
  widget, leaves the task open and exits with code 5.

Nothing runs before the claim, the plan approval and the task's own rules allow `orch task done`.

## Input file (orch task add --file)

The same YAML can sit under a `## Tasks` heading in the `--body-file` of `orch new` (bare or inside one ```yaml fence); it is validated like `task add --file` before the ticket gets an ID.

```yaml
tasks:
  - text: Inventory the 14 jobs and their cluster settings
    refs: [file:dbt-models/resources/jobs/]
  - text: Switch the gold-layer jobs to serverless in dev
    refs: [file:dbt-models/resources/jobs/gold/, ac:1]
    verify: databricks bundle deploy -t dev and one green run per job
    needs: [T1]            # IDs are assigned in file order from the next free number
  - text: Grant the job service principal SELECT on finance.*
    owner: human
```

Keys per task: `text` (required), `refs`, `verify`, `needs`, `owner`. Any other key is an error.

## JSON view

`orch task list <ref> --json` (also `tasks` in `orch show --json`) returns:

| Key | Meaning |
|---|---|
| `format` | `"orch.tasks.v1"` |
| `ticket` | ticket ID |
| `status` | ticket status |
| `error` | `null`, or the parse error (`"Tasks line N: …"`) when the section is invalid |
| `line` | only with an `error`: the section line number |
| `summary` | `{total, todo, doing, done, skipped, blocked, closed}` |
| `doing` | ID of the doing task or `null` |
| `next` | the doing task, else the first todo `owner: agent` task whose `needs` are closed, else `null` |
| `open` | IDs of todo, doing and blocked tasks |
| `can_move_to_testing` | the real result of the move rule |
| `plan_approved` | the plan approval stamp or `null` |
| `added_since_approval` | number of tasks added after plan approval |
| `tasks` | list of tasks, below |

Each task: `id`, `state`, `text`, `owner`, `needs`, `needs_open`, `refs[]`, `verify`, `why`, `on`,
`on_ref`, `note`, `added`, `added_after_approval`, `waits_on_you`.

Each ref is `{kind, target, label, exists}` plus the keys of its kind. `target` is what the ref
says (normalised as in the grammar); `exists` is whether it resolves.

| Kind | Extra keys |
|---|---|
| `file` | `repo` (the `git.repos` name the path starts with, or `null`), `path` (relative to that repo, else to the workspace), `lines` (`"12"`, `"12-40"` or `null`) |
| `static` | `path`, `lines` |
| `artifact` | `url` (`/a/<ticket>/<name>`) |
| `ticket` | `resolved` (the local ticket ID, or `null` when nothing matches), `status`, `title` |
| `ext`, `url` | `url` (the tracker link, or `null` without a matching tracker; the URL itself for `url`) |
| `section` | none; `target` is the section's canonical name |
| `ac` | `text` (the criterion, or `null`) |
| `q` | `text` (the question, or `null`), `answered` |

`on_ref` is `null` without `on`, else one of:

| `kind` | Shape |
|---|---|
| `question` | `{kind, id, exists, answered}` |
| `task` | `{kind, id, exists, state, owner}` (`state`, `owner` are `null` for an unknown task) |
| `ticket` | `{kind, id, exists: true, status, title}` (`id` is the local ticket ID) |
| `ext` | `{kind, id, exists, url}` (`exists` is whether a tracker matches) |

The machine-readable contract is [`tasks-view.schema.json`](tasks-view.schema.json) (JSON Schema
draft 2020-12, format `orch.tasks.v1`); the test suite validates `view()` output against it.

## Events

| Kind | Data |
|---|---|
| `task.added` | `{tasks, after_approval[, imported]}` |
| `task.edited` | `{task, fields}` |
| `task.moved` | `{task, was, now[, note\|why\|on]}` |

Task events never carry a `from` or `to` key; those mean a ticket status change. When a blocking
question blocks the doing task, the `question.asked` event gets `task_blocked`; when the answer
restarts it, the `question.answered` event gets `task_restarted`.

## Versioning

The version lives in the JSON (`"format": "orch.tasks.v1"`) and in this document, with no marker
in the ticket file. Additive fields or ref kinds keep `orch.tasks.v1`; a change that old readers
would misread bumps to `orch.tasks.v2`, and orch then reads both.

## Example

```markdown
- [x] T1 Inventory the 14 jobs and their cluster settings
  - ref: file:dbt-models/resources/jobs/
  - ref: artifact:job-inventory.csv
  - note: 14 jobs, 3 with spark_conf overrides
- [x] T2 Check every job for serverless blockers (init scripts, RDD API, custom JARs)
  - ref: static:DEMO-0043/serverless-notes.md — findings per job
  - ref: url:https://docs.databricks.com/aws/en/compute/serverless/limitations
- [-] T3 Rewrite init scripts as environment dependencies
  - why: T2 found no job with init scripts
- [x] T4 Add the serverless environment spec to the bundle
  - ref: file:dbt-models/databricks.yml#L40-58
  - verify: `databricks bundle validate -t dev`
  - note: validate OK, 0 warnings
- [/] T5 Switch the gold-layer jobs to serverless in dev
  - ref: file:dbt-models/resources/jobs/gold/
  - ref: ac:1
  - verify: `databricks bundle deploy -t dev` and one green run per job
  - note: 8 of 14 jobs switched
- [!] T6 Switch the prod schedules
  - ref: ticket:DEMO-0042
  - needs: T5
  - why: prod runs read finance.*; SELECT not granted yet
  - on: DEMO-0042
- [ ] T7 Compare one week of run costs, old vs serverless
  - ref: ac:3
  - verify: cost query saved as artifact cost-compare.csv
  - needs: T5, T6
  - added: 2026-10-02T09:14Z after plan approval
- [ ] T8 Grant the job service principal SELECT on finance.*
  - owner: human
```

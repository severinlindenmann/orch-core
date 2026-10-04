# Ticket document · schema 1

A ticket as one JSON document: its frontmatter, sections, gates, questions, task list, what it needs
from the human and its artifacts. Tools that mirror or render tickets (for example a phone app) read
this document instead of the Markdown file. `orch schema ticket --json` prints the JSON Schema
(draft 2020-12, generated from the core model, so new statuses or sections show up without a second
list), and `orch schema example` prints a fixed example ticket that validates against it. Both always
print JSON and run outside a workspace.

## Versioning

`schema_version` is semver and starts at `1.0.0`.

- A new optional key is a minor bump (`1.1.0`).
- A fix to the schema that accepts the same documents is a patch bump.
- A removed, renamed or retyped key is a major bump (`2.0.0`).

A tool must check the major version and refuse a document with an unknown major, with a clear message
such as "this ticket uses schema 2; update the app". Unknown keys inside a known major are ignored.

## Keys

| Key | Meaning |
|---|---|
| `schema_version` | Semver of this document format, `1.x.y`. |
| `id` | Ticket ID, for example `DEMO-0038`. |
| `title` | Ticket title. |
| `type` | One of the core ticket types (`feature`, `bug`, `chore`, `spike`, `investigation`, `epic`). An epic groups children and has only the requirements gate (see Epics). |
| `priority` | `low`, `normal`, `high` or `urgent`. |
| `size` | `xs`, `s`, `m` or `l`. |
| `status` | `backlog`, `open`, `in-progress`, `waiting`, `testing` or `done`. |
| `created` | Creation stamp (`YYYY-MM-DDTHH:MMZ`). |
| `updated` | Last change stamp. |
| `labels` | Free labels. |
| `parent` | Parent ticket ID, or null: the epic of a child, or the source of a follow-up. |
| `sprint` | A sprint id from the workspace config, or null (1.2; planning only). |
| `blocked_by` | Ticket IDs this ticket waits for. |
| `follow_ups` | Follow-up ticket IDs. |
| `external` | External tracker keys: `{key, url}`. |
| `repos` | Repositories the ticket touches. |
| `branches` | Branches per repository. |
| `prs` | Pull or merge requests. |
| `gates` | `requirements` and `plan`: `{state, hash, covers, approved, via, changes_requested}`; `verify`: `{verdict, at, via}`. `covers` lists what `hash` binds, in order: section names, then frontmatter keys. Show all of it before approving. |
| `questions` | The questions asked on the ticket, each with its `hash` (see below). |
| `sections` | Section name to Markdown text, every core section always present. |
| `tasks` | The task-list view, format `orch.tasks.v1` (see Task list). |
| `needs` | What the ticket needs from the human now: `{kind, detail}`, kinds as in the dashboard's needs-you list (`broken`, `answer`, `task`, `approve-requirements`, `approve-plan`, `re-approve`, `approve-epic`, `verdict`). 1.4: `together: true` on `approve-requirements` when the plan is drafted too and may be approved with the requirements in one decision (target `{gate: "requirements", hash, plan_hash}`). |
| `move` | 1.4: whose move it is, by the same rules as the dashboard's move chip (`orch.dashboard.data.cards`): `{who, kind, label, ref, why?, epic?}`. `who` is `you`, `agent` or `nobody`; `kind` one of `approve-requirements`, `approve-plan`, `re-approve`, `approve-epic`, `answer`, `task`, `verdict`, `repair`, `working`, `stale`, `blocked`, `ready`, `done`; `ref` the gate, question or task it is about (or null); `why` one line of rule text when the move is yours; `epic` on a `re-approve` that is the epic's. It covers what a client cannot derive without the gate hashes: a change request whose text has since changed is yours again (`approve-*`), and an epic child whose gate the epic's signed charter no longer covers is yours as `re-approve` of the epic (`epic` names it). `orch show --json` and each `orch list --json` row carry the same `move`. |
| `claim` | The agent session holding the ticket: `{session, harness, at}`. |
| `artifacts` | Artifact paths, relative to the ticket's artifact folder. |
| `artifact_items` | 1.5: what the ticket links (its frontmatter `artifacts` list, see Artifacts): `{source, kind, label}` with `source` `file`, `link` or `static`, `kind` one of `screenshot`, `report`, `log`, `link`, `dataset`, `build`, `diagram`, `other`, `receipt`; a file also has `name` and `sha256`; `task` and `ac` when the artifact belongs to a task or proves a criterion. 1.7: `by`, who added it (`human:you`, `agent:<harness>:<session>`); a `receipt` (written only by `orch task done --run`, docs/tasks-format.md "Receipts") also has `run`: `{exit, timed_out, commit, dirty, repo, at, seconds, check, steps: [{name, status, seconds}]}` (`repo` is the name of the checkout it ran in, never a path; each fact only when well typed), `status` `pass`, `fail` or `skip`, `exit` null when a step timed out. The commands and the output stay in the ticket file and the receipt itself. Never a URL or a local path: a phone shows the label and kind, and may fetch a file by name where its transport offers that. |
| `verdict` | 1.3: what a verdict given elsewhere must echo, `{hash, round}`, or null while no verdict is due: a ticket in testing (`round` = its testing round, as on the `verdict` need), or an open epic whose open children are all in testing (`round` null). See Verdict hash. |
| `signed` | 1.6: whether the signed ledger on this machine backs each approved gate and a done ticket, as the dashboard's chips decide it (`orch.dashboard.data.story.gate_signers` / `done_signer`): an object with a key `requirements`, `plan` and `verdict` only where there is something to attest (an approved gate; a done ticket), each `{signed: bool, by: string or null}`. `by` is `you`, `from your phone`, `by your epic charter` (covered by the epic's signed charter) or `by delegation` for a gate, `accepted` or `closed` for the verdict. `signed` is true only for a human's own entry (or a phone's) the ledger verifies; a delegation, an unknown or tampered entry and a missing ledger are `signed: false` (`by` null, or `by delegation`). Never key material or MACs. Show `signed` only when `gates.<gate>.state` is `approved` (an invalidated gate can still have a matching ledger entry for its old hash). A mirror shows this instead of trusting the frontmatter's `approved` and `via`. |
| `revalidate` | 1.7: `{idle_days}` when an open or backlog ticket was not touched for the workspace's `dashboard.revalidate_days` (default 30, 0 turns it off), else null. Derived from `updated` when the document is built; it blocks nobody and is no need. |

`state` of a gate is `pending`, `approved` or `invalidated` (approved, but the text changed since).

## Question hash

The hash binds an answer given elsewhere to the exact question the human saw. If the text, the reason,
the options or whether it blocks change, the hash changes and an old answer is refused.

```
question_hash(q) = "sha256:" + sha256(canonical_json({id, text, why, type, options, recommended, blocking}))
canonical_json   = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
```

`options` is the list of `{key, label, cost}`; `blocking` is a boolean (default true). The answer fields
(`answer`, `note`, `answered`, `via`) and `asked` are not part of the hash. The needs-you list (`orch.core.query.needs_you`)
carries the same hashes in its `answer` items under `hashes` (`{qid: hash}`).

## Verdict hash

The verdict hash binds a verdict to the acceptance criteria and evidence the human read:

```
verdict_hash(tickets) = "sha256:" + sha256(canonical_json([{id, status, ac, verification}, ...]))
```

one object per ticket in id order, `ac` and `verification` being the raw `Acceptance criteria` and `Verification`
sections (when they show artifacts inline, the object also has `artifacts`, `{"artifact <name>": "sha256:<hex>" or
"missing"}`, so a replaced evidence image changes the hash). A ```orch widget in those sections adds what it pins to
the same `artifacts` object (`orch.core.artifacts.widget_binding`): `"widget <name@v>"` for a template version and
`"widget file <ref>"` for each file it names, each `"sha256:<pin>"` while the bytes on disk match the pin, `"drift"`
when they changed and `"missing"` when they are gone. So a template edited in place, or a pinned file swapped, after the
human read the widget changes the hash and the verdict is refused; the widget itself then draws as a drift callout,
never the new content (docs/widgets.md, "Pinned templates"). Every caller that shows or checks a verdict computes the
hash with the workspace, so the disk is checked on both sides. A ticket's verdict hashes the list of that one ticket,
an epic's the list of its open children. Every verdict
(dashboard, CLI, addon intent, phone decision) sends it; core refuses a missing or changed one. A done verdict is also
refused while the requirements or plan approval is `invalidated` (re-approve or send back first). A phone app sends the
document's `verdict.hash` as `target.hash` of its signed verdict decision. Epic verdicts are desktop-only for now:
core accepts a phone verdict only for a ticket in testing, so an epic's `verdict.hash` is published (the dashboard and
`orch verdict <epic> done` bind it) but a phone decision on an epic is not applied. Each child closed by an epic
verdict is bound to its own part of that hash.

Test vector (the example ticket in testing, Verification `- AC1: opened 3 files in Excel`):

```
canonical_json: [{"ac":"- [ ] Opens in Excel","id":"DEMO-0038","status":"testing","verification":"- AC1: opened 3 files in Excel"}]
verdict_hash:   sha256:061c01e67c0692b2686d641ffd7d580c2c6310928dd8818b5a7ff5f68eb62a3b
```

## Gate hash

`gate_hash` (`core/gates.py`) is `"sha256:" + sha256` of the gate's sections, normalised:

- requirements: `Requirements`, `Acceptance criteria`, `Out of scope`; plan: `Plan`;
- version 2 (every new approval): the requirements gate starts with `Summary` when that section is not
  empty, and after the sections adds a blank line and the lines `size: <size>` and `type: <type>`;
- each written as `## <name>`, a blank line, then its text;
- version 3 (every new approval): after those lines, one line `artifact <name>: sha256:<hex>` (or
  `artifact <name>: missing`) for each ticket artifact the gated text references inline (`![alt](artifact:<name>)`
  or `[text](artifact:<name>)`, in any form Markdown resolves to it: reference links, `<…>` destinations,
  entities, backslash escapes, percent-encoding, this ticket's `artifacts/<ticket>/<name>` (also after `./` or `../`)
  and `/a/<ticket>/<name>` paths, matched on the whole percent-decoded path with any `?query` and `#fragment`
  dropped; read from the same parsed tokens the dashboard renders, which shows or links an artifact only when it is
  bound, always with `?v=<sha256 prefix>`, and renders any other link to this ticket's artifacts as plain text),
  in order of first reference, with the sha256 the ticket's artifact entry records.
  Replacing the file (`orch artifact add … --replace`) changes it and invalidates the approval like a text edit.
  Without inline artifacts the version 3 hash equals the version 2 hash;
- ticked boxes count as unticked (`- [x]` becomes `- [ ]`), so ticking an acceptance criterion does
  not invalidate the gate;
- trailing spaces removed, runs of blank lines folded to one, joined with a blank line, UTF-8.

An approval stores the hash and its version (`hash_v`; absent means an approval from before version 2,
which keeps matching its version 1 hash; a version 2 approval keeps matching its version 2 hash); when the current hash of that version differs, the gate is
`invalidated`. The `hash` in the document is always the current version: an approval sends that one. Needs-you items
of kind `approve-requirements`, `approve-plan` and `re-approve` carry `gate` and `gate_hash`.

## Artifacts

A ticket links what was produced for it in its frontmatter `artifacts` list, written by `orch artifact add`
(and `orch artifact scan` for files put straight into the ticket's folder). Each entry has exactly one source:
`name` (a file in `orchestrator/artifacts/<ticket>/`, with `sha256` and `size` of its content), `url` (an http(s)
page; orch never fetches it) or `static` (a file under `orchestrator/static/`). Optional: `kind`, `label` (one line,
no hidden characters), `task` (`T3`), `ac` (criterion number), `added`, `context`.

`orch artifact list <id> --json` prints these entries as objects (with `kind` filled in, and `"unlinked": true` for
a file in the folder that the ticket does not link); older versions printed `<ticket>/<name>` strings.

Sections may show a ticket artifact inline with Markdown image syntax: `![Login after the fix](artifact:login.png)`.
The dashboard renders it as a lazy-loaded thumbnail that opens the full file, only for a linked image whose bytes
still match its `sha256`; an `http(s)` image is never loaded (it stays a link), and SVG is only ever an `<img>`.
Alt text is expected (`orch check` warns without it).

Every other link in ticket Markdown is checked on its canonical form (decoded once, dot segments resolved, scheme and
host split off). It stays a live link only when it is: another ticket's artifact, emitted as `/a/<ID>/<name>` (a
strict ticket id that is not this ticket, any case, and a plain file name); an `http(s)` or `mailto` URL, or a
root-relative path without dot segments, whose path does not start with `/a/` (any case); or an in-page `#fragment`.
Everything else, including relative paths and any URL on any host whose path starts with `/a/`, is shown as text.
External sites whose paths happen to start with `/a/` are therefore not clickable; that is accepted. A gated section that shows an artifact binds it (Gate hash,
version 3).

## Open questions in gated text

A gated section should state decisions, not ask the human. `orch approve` (and the dashboard) refuse a gate in which
a line, after an optional list marker (`-`, `*`, `+`, `1.`) and optional bold (`**`), starts with:

- `Open question` or `Open questions`, optionally followed by `for the human|you|user|owner`, then `:` or `?`;
- `Question for the human|you|user|owner` (or `Questions for …`), then `:` or `?`;
- `TBD` as a whole value: the line is `TBD …`, or `<label>: TBD`.

The human can approve anyway after reading it (`orch approve <id> <gate> --despite-open-question`, or the dashboard's
checkbox); the `gate.approved` event and the ledger entry carry `despite_open_question: true`. Agents ask questions
with `orch ask` instead.

## Approval ledger

Human approvals, answers, verdicts and closes are also signed into one per-user file, `ledger.jsonl` in the orch
config dir. Each line is a JSON object `{workspace, ticket, kind: "gate"|"answer"|"verdict"|"close"|"charter"|"pause"|"ticket_request", gate, hash,
hash_v, despite_open_question, qid, answer, question_hash, verdict, verify_at, verdict_hash, prev, reason, adopted, n, actor, via, at,
evidence, mac}` (fields by kind), plus `device` (the paired phone's id) on a decision applied from a phone (`via`
`phone:<label>`); a backlog ticket a paired phone requested is signed as `ticket_request` with its `decision` id. A
`phone:` event of a signed kind without a matching entry is `unverified-remote` in `orch check`, and Today shows its
receipt as "unverified". Closes, verdicts and reopens form one chain per ticket: each carries `prev`, the `mac` of the ticket's previous such
entry, and a reopen is signed like the others. A done counts as human-verified only when the ledger alone says so: the
ticket's newest signed status entry is a `close`, or a done `verdict` that matches the ticket's verify block
(`verify_at`, `verdict_hash` against `gates.verify.hash`), and its `prev` is the `mac` of the entry before it. A later
reopen or follow-up verdict ends every earlier close; the event log and the ticket file are not inputs. An entry
from before the chain existed has no `prev`; it counts only while no later signed status entry exists and the event
log shows exactly one done for the ticket (a review with `orch ledger adopt` signs a chained entry). A signed
head record, `ledger.head` beside the ledger, holds the number of signed entries, the `mac` of the newest one and
whether the ledger was already cut when it was written, signed with the same key and rewritten under the ledger's
lock on every append; every entry written since carries `n`, its place among the signed entries. When the ledger
does not hold exactly the signed entries the head counts, its entry at that count is not the one named, an `n` does not
match its place, an unnumbered entry follows a numbered one, or the head record is missing, damaged or older than the newest numbered entry, the ledger was cut: every chained decision (done verdicts, closes, reopens
and workspace settings) then counts as not verified, `orch check` reports `ledger-cut` as an error, and the head
stays marked as cut after later appends. The human recovers by restoring the ledger and its head record from a
backup, or by starting a new ledger (every decision then needs `orch ledger adopt`). A ledger without a head record
counts only while no entry carries `n` (a ledger from before the head record existed): nothing that verified before
changes, and its next append writes one. Deleting the
head record together with every numbered entry falls back to that older mode and is not detected, and neither is
rolling the ledger and the head record back together to an earlier consistent state: no anchor inside these files can
close either. `workspace` is the first 16 hex digits of sha256 of `"<customer>|<id prefix>"`
from the workspace config; `mac` is HMAC-SHA256 over the canonical JSON of the other fields with the 32-byte key
in `ledger.key`. A line with a bad `mac` is ignored. An approved gate, answered question or done verdict without a
matching line is `unsigned-decision` in `orch check`, and agents do not claim, start or finish tasks, or move to
testing on an unsigned approval or blocking answer. `orch ledger adopt` lets the human review and sign such
decisions one by one (`adopted: true`).

## Epics

A ticket of type `epic` groups the tickets whose `parent` is its id (no nested epics). It has the requirements gate
and no plan or tasks. Its approval is a **charter**: one human decision over the epic's requirements (hash v2) and, for
every child that is not done, `{id, requirements, plan}` (the child's requirements hash v2 and plan hash, `null`
while it has no plan). The ledger gets the epic's ordinary `gate` line and a `charter` line `{charter, epic_hash,
hash_v, children, delegate, delegation}`; `charter` is sha256 over the canonical JSON of `{epic, epic_hash, hash_v,
children, delegate}`. Covered children get their gates written with `epic: <id>` and one `gate.approved` event per
gate carrying `epic` and `charter`. A child counts as approved while the latest charter of its current parent lists
its current hashes and the epic's requirements still hash as signed; a child that changed, joined or left is not
covered until the epic is approved again (`approve-epic` in `needs`) or the child on its own.

Delegation is opt-in at the epic's approval (`delegate: {max_children, max_size}`, defaults 10 and `m`; the human
may choose any size up to `l`), stored in the charter line and the approval event only. While it is active (no signed
`pause` line for it and the epic's requirements unchanged), an agent may approve a child that an agent created, once,
within the limits (`orch epic auto-approve`): the gates then carry `delegation: <charter>` and a `gate.delegated`
event per gate is written by the agent. These are not signed human decisions. An agent proceeds on one only while
the delegation is signed and the epic unchanged, the child is within the limits (size; its position among the
children the delegation approved, counting both the events and the frontmatter that names the delegation), was
created by an agent, never appeared in a charter, holds no hidden characters, and the event matches. A `pause` line
`{delegation, kept: [{id, requirements, plan}]}` stops further auto-approvals only: the children whose
auto-approval was valid when the human paused are kept, with exactly those hashes. `orch check` lists each valid
one as `delegated-approval` (level `info`).

Limit: the event log is a file in the repository. Code running as the same user outside the guard's view could add
events; the checks above keep that from creating an approval beyond the signed limits, but the order of
auto-approvals is only as trustworthy as that file (see the ledger's limits).

Sprints are planning metadata: `sprints: [{id, name, start, end}]` in the workspace config and `sprint: <id>` in a
ticket's frontmatter. No gate depends on them.

## Task list

`tasks` is exactly the view of `orch task list <id> --json`, format `orch.tasks.v1`. Its grammar and
fields are described in [tasks-format.md](tasks-format.md), and its JSON Schema is
[tasks-view.schema.json](tasks-view.schema.json). The ticket schema embeds that file unchanged under
`$defs.tasks_view` and references it by its `$id`; it never defines tasks a second time.

# Add-on skills, the add-on index and add-on commands (AP, 27 Oct 2026)

Status: design, owner go 27 Oct ("start building it"); challenged by Opus (Claude API) and Codex on 27 Oct, both
"build with the listed changes", which are folded in (§9). Part of #1290. Builds on [addon-platform.md](addon-platform.md)
(declarative UI, tiers) and ticket-format §8 / §8.1 (manifest, grant, `agents_md`).

## 1. Goal

The owner's goal, 27 Oct:

- **A. Mount, don't patch.** We and third parties add an add-on without changing core code. Deep technical
  exceptions (a new capability, a new UI node) are core work done once, never per add-on.
- **B. Agents follow the flow.** When an add-on is active, an agent (also a weak one, Haiku) uses it as intended:
  `AGENTS.orch.md` names each active add-on and when to use it, and each add-on ships its own skill with the exact
  steps. A switched-off add-on is never improvised around.
- **C. UI: native and declarative, no iframe yet.** Add-on screens are data the dashboard draws with its own
  components; the component set grows when a real screen needs it. No add-on JavaScript in the dashboard, no script
  frames for now.

## 2. What is true today (develop 3ae35d55d)

| Piece | State |
|---|---|
| `agents_md` | validated (one line, 200 ASCII chars), shown in the install review, but **never written**: `instructions sync` calls `write_workspace_files()` → `workspace_files()` without the add-on lines, so agents see nothing of any add-on |
| `skills`, `cli` | reserved in `addon-manifest.json` (`maxItems: 0`, `maxProperties: 0`) |
| `orch help` / `describe` | bare `orch help` lists workflows, `describe` lists operations; add-on operations (quick, follow-up) are core ops, listed whether the add-on is on or off |
| `orch set` | core keys only (`SET_PATTERN` in `ops/_dsl.py`); add-on fields cannot be set from the CLI |
| hooks | core-only: session-start (6 lines), pre-compact (4), waiting |
| UI | declarative node set + six slots exist (`addon-ui/`); built-in screens are compiled pages |

## 3. Evidence: Haiku simulation (state/addon-skill-sim/REPORT.md)

345 Haiku runs, real `claude -p` against a stub `orch`, 8 add-ons (3 real, 5 invented with overlapping triggers, 1
installed but off), 19 tasks, 3 reps.

| Condition | Haiku correct |
|---|---|
| C0 today's one-liners, no skills | 86/102 (84%) |
| C1 index + one skill per add-on | 96/102 (94%) |
| C4 = C1 + "Off here, do not improvise" line + "Do not use when" boundaries | **60/60** |
| Sonnet C0 / C4 | 8/10 / 10/10 |

Findings: (1) the index line routes; skill style matters less; (2) "read its skill first" made 138/139 add-on calls
read the skill; (3) the top failure is improvising around an off add-on, whose skill is not installed, so **the index
and the hook carry the off rule**; (4) lookalikes need a "Do not use when" line; (5) N = 3, 5, 8 showed no
degradation, but the 40-line cap breaks at N ≥ 5; (6) no over-use in 345 runs.

Limits (both challenges): the stub's help listed only active add-on commands, which the real CLI does not, so the real
baseline is likely worse than 84%; every prompt carried an "uses orch" cue; C4 is at ceiling on development tasks.
The evidence supports the direction, not a reliability guarantee (§4 D8).

## 4. Decisions

### D1. An add-on ships one skill in its package

Manifest key `skill` (orch.addon/3, api ≥ 1.2): `"skills/main"`, a folder in the package with exactly one file,
`SKILL.md`. Installed name: `orch-addon-<addon>` (the add-on name already matches the skill-name rule
`[a-z][a-z0-9-]{0,63}` minus the prefix length; longer names are refused at pack). One skill per add-on: the index
routes by add-on, the simulation tested one, and several skills per add-on reopen name collisions
(`foo`+`bar` vs `foo-bar`). The reserved `skills` array stays reserved.

`SKILL.md` rules (validator, `orch addon pack`, install; a breach is `market.package_invalid`):

- frontmatter has exactly `name` (= installed name) and `description` (1 to 300 printable chars, trigger first);
  every other key (`allowed-tools`, `model`, `hooks`, …) is refused: a skill grants nothing;
- at most 2400 chars (`SKILL_MAX_CHARS`), UTF-8, no NUL, no CR;
- **command lint (error):** every `` `orch …` `` in the text must resolve to a core operation, one of this add-on's
  `cli` commands, or `orch set REF <addon>.<field>=…` for one of its own fields; the text must not name `tickets/**`,
  `.orch/**` or another `orch-addon-*` / built-in skill. A skill that names a command that does not exist is the worst
  failure for a weak model, which follows it literally;
- warnings from `pack` (not errors, so non-English authors are not blocked): `description` starts `Use when`, has a
  `Not for`; body has the sections `Use when`, `Do not use when`, `Steps`, `If orch refuses` (template §6).

The skill is part of the package, so the package digest and the grant cover it. The install and update review show
its text; the update diff compares the **bytes** of `SKILL.md` (not only the manifest key) and shows a text diff.

### D2. Core reconciles add-on skills; files follow the log

- **Where:** the workspace root and every live orch worktree (`$ORCH_WORKTREE`s the host knows), in
  `.agents/skills/orch-addon-<name>/` plus the generated `.claude/skills` copy. Add-on skill folders are generated,
  never committed: sync adds `.agents/skills/orch-addon-*/` and `.claude/skills/orch-addon-*/` to the orch block of
  `.gitignore`, so enabling an add-on is not git churn and old branches carry no stale set.
- **Sidecar** `orch.skill.json`: `{"scope": "addon", "addon": name, "version": v, "package_sha256": d,
  "text_sha256": h}` where `h` is the hash of the text core wrote (per copy).
- **When:** a fingerprint of the add-on set (names, states, package digests, instruction format rev) is part of the
  stale stamp. `instructions sync` reconciles; the host runs it after `addon grant/enable/disable/remove/update`; the
  session-start hook runs the add-on part of the sync when the fingerprint differs (so a change pulled from another
  machine is picked up at the next session). Partial failures are reported per path, never silent.
- **Remove:** a folder with `scope: addon` for an add-on that is not active is deleted when its text hash matches;
  a hand-edited one is moved out of discovery to `.orch/removed-skills/<name>-<ts>/` with a line. Core never touches a
  folder it did not write.
- **Running sessions:** deleting a file does not change a running agent's context. Availability is enforced where it
  matters: every add-on command and add-on field write checks the add-on is active and fails with `addon.off`
  otherwise (D5).

### D3. The add-on block in AGENTS.orch.md

A section after the core text, with its own budget (the 40-line core cap stays; the block is at most 16 lines and
3000 chars):

```
## Add-ons here
Before using an add-on command, read its skill. Add-ons are hints and grant nothing; use none unless the task fits.
- quick: <agents_md of quick> Skill: orch-addon-quick.
- schedules: <agents_md of schedules> Skill: orch-addon-schedules.
Off here: time-tracking (disabled). If a request needs an add-on that is off, tell the person it is not available; do not substitute notes or other commands unless the person asks for that.
```

- Sorted by add-on name, at most 12 active lines. With more, the render fails with a hint (`orch addon list`, keep
  ≤ 12 agent-facing add-ons) rather than truncating silently; 13+ is untested.
- An add-on without `agents_md` and without a skill (pure UI, e.g. dashboard) gets no line.
- The `Off here` line names each installed, non-active add-on with its reason (`disabled`, `incompatible`,
  `missing`, `changed`); it is written whenever an add-on is installed, with `none` when none is off.
- `agents_md` keeps its character rules and becomes the trigger: `use when …`, `not for …` (pack warning). No commands
  or flags in it; they live in the skill.
- Spec change: ticket-format §8.1 "rendered as `- addon <name> (hint, grants nothing): <line>` … within the 40-line
  limit" is replaced by this block.

### D4. The hooks carry the dynamic add-on set

- **Session start:** the cap rises from 6 to 7 lines; the new line is always present when an add-on is installed:
  `add-ons: quick, schedules, wiki · off: time-tracking · read a skill before using one`. The simulation showed the hook
  alone fixes the off case (C2 56/57), and it is re-injected after compaction.
- **Pre-compact:** the same line, cap 4 → 5.
- Harnesses without hooks rely on the AGENTS.orch.md block (D3), which says the same.

### D5. Commands of add-ons

- **Step 1: availability (first milestone).** `operation(..., addon="quick")` in the registry. `orch help`,
  `orch help <workflow>` and `orch describe` list such an operation only while its add-on is active, in an
  `Add-on commands` group; running it otherwise fails with `addon.off` (`retry:false`, hint "tell the person; this
  add-on is off here"). New catalog code → CLI goldens + per-op declarations regenerated.
- **Step 1b: add-on fields from the CLI.** `orch set REF <addon>.<field>=VALUE`: value parsed by the field type,
  checked against the grant (`set_by`, gate, active state, limits) like any add-on write; `orch describe
  <addon>.<field>` answers from the grant. This is what a field-only add-on (changelog) needs; it failed in the
  simulation because there was nothing to call.
- **Step 2: third-party commands, piloted on one built-in.** Manifest `cli`: `{"commands": [{"name": token, "kind":
  "read" | "propose", "summary": line ≤ 80, "who": "agent" | "member", "args": JSON Schema object (strings, ints,
  bools, enums only, ≤ 8 properties)}]}`, ≤ 8 commands. `orch <addon> <command> --arg v`:
  - `read`: answered by a named read of the add-on (the same mechanism as `addon.view`), no proposal, ≤ 20 lines;
  - `propose`: the runner method `command` returns the `propose` shape (only the add-on's own ticket fields, sections
    and artifacts, checked by `Registry.check_proposal`, signed by the host) plus ≤ 20 lines.
  - Add-on names that equal a core operation's first word or a reserved word (`help`, `show`, `task`, `set`, `x`, …)
    are refused at pack (`market.reserved_id`).
  - The pilot is one built-in whose commands fit this contract. Commands that create tickets, schedule runs or write
    workspace content (quick, schedules, wiki writes) need host capabilities the proposal does not have; they stay
    core operations with `addon=` (step 1) until a pilot shows which capability is missing. AP7 is not promised for
    every built-in.

### D6. No add-on hooks in v1

The simulation needed no hook beyond core's own (D4). An add-on hook runs add-on code at every session start; that is
a capability, not a hint. If one is needed later it is a declarative `session_line` (text), not a command.

### D7. UI: one real declarative screen first, no iframe

The script-frame tier is not built (owner 27 Oct). Instead of a component list, AP6 takes **one** built-in screen end
to end onto the declarative tier with the smallest new node set and explicit read and action contracts (named read,
pagination, loading and error states, conflict on save). Candidate: wiki read-only viewer (`tree` + `markdown` +
`search`). Further nodes (`code`, `diff`, `markdown_editor`, `upload`, `master_detail`, `paged_list`) come only when a
concrete screen needs them. Terminals, start-agent, dashboard and the host side of worktrees stay core.

### D8. Evals: contract tests in CI, a model eval by hand

- **Deterministic (CI):** the rendered block for fixed add-on sets (golden), line/char budgets, the lint of D1 on
  every built-in skill, reconcile/remove/quarantine, `addon.off` on every add-on operation, help filtering.
- **Model eval (by hand, `scripts/addon-eval/`):** built from the real `render_agents_md`, the real skill writer and
  the real CLI's help/describe output (a recording stub for side effects only). Task sets: the simulation's set plus a
  **held-out** set: vague phrasing, no "uses orch" cue, multi-step, lookalikes at N = 12, an add-on disabled mid-run,
  command failures, a field write. Results are judged per named failure cell (off add-on, lookalike, side chore, field
  write), not by the total. Run before merging AP2/AP4 and before a release of an official add-on; authors can run it
  on their own add-on with an optional `evals.json` in the package.

### D9. Built-in add-ons use the same path

Each agent-facing built-in add-on gets `packages/addons/<name>/skills/main/SKILL.md` and a trigger `agents_md`; the
package copies in `orch/addons/manifests/` follow (the existing pin test). First three: quick, schedules, wiki (plus
the changelog example add-on). The rest after the eval on those three holds.

## 5. Build order

| Step | What | Depends on |
|---|---|---|
| M0 | eval harness on the real CLI + held-out tasks; baseline numbers | — |
| M1a | `addon=` on operations, help/describe filter, `addon.off` (D5 step 1) | #1324 merged |
| M1b | `orch set REF <addon>.<field>=…`, `describe <addon>.<field>` (D5 step 1b) | M1a |
| M1c | add-on block + Off line in AGENTS.orch.md, fingerprint in the stale stamp (D3) | #1324 merged |
| M1d | `skill` key, lint, writer/reconciler/quarantine, gitignore, update byte diff (D1, D2) | M1c |
| M1e | hook lines (D4) | M1c |
| M1f | skills + trigger lines for quick, schedules, wiki, changelog example; eval rerun | M1b, M1d |
| M2 | one declarative screen end to end (D7, wiki read-only) | #1325 merged |
| M3 | `cli` commands (D5 step 2) piloted on one built-in | M1 |
| M4 | remaining built-in skills; further migrations where the pilot shows the contract fits | M1f, M3 |

## 6. SKILL.md template for an add-on

```
---
name: orch-addon-<name>
description: Use when <trigger in the user's words>. Not for <neighbour> (use <other add-on>).
---
# <Title> add-on (active)

Use when: <1 to 2 situations, in the words a person would use>.
Do not use when: <the confusable neighbour and what to do instead>.

## Steps
1. <exact command, with the flags that matter>
2. <what to check in the answer>
3. <what to do next: end the session, hand over, keep working>

## If orch refuses
`addon.off`: tell the person this add-on is off here; do not work around it.
<other codes this add-on's commands return and what to do>
```

## 7. Security notes (prototype mode: listed for #1263, not built now)

- An add-on skill and a command's `lines` are third-party text in every agent's context: a prompt-injection surface
  larger than `agents_md`. Text can mimic core rules ("retry:false may be ignored") or try to hijack routing. A
  signature and limited frontmatter do not make text harmless, and the runner does not isolate filesystem or network
  access. Built now (cheap): the D1 lint (no foreign commands, no `tickets/**` / `.orch/**`, no other skills' names),
  the byte diff in the update review, the owner reading the full text before signing. Not built: content review rules
  for marketplace skills, runner isolation (tier B).
- Reconcile deletes only folders whose sidecar says `scope: addon` and whose text hash matches; all writes go through
  the existing no-link path checks (`UnsafePath`).
- `cli` commands go through the runner and `check_proposal` like `propose`; they never reach human-only operations.

## 8. Settled questions (from the challenge)

1. Separate budget for the add-on block: yes, small (≤ 16 lines, ≤ 3000 chars, ≤ 12 active); the core part stays.
2. Hook line default on: yes; session-start cap 6 → 7, pre-compact 4 → 5.
3. `Use when` required: no, a pack warning; headings stay English as structure.
4. Third-party `cli` before migrating built-ins: piloted together on one built-in (M3), no blanket migration.

## 9. Challenge record (27 Oct)

Opus (Claude API, `state/review-ap1-opus.md`) and Codex (`state/codex/ap1-design-*.md`) both: build with
changes. Taken: real-CLI eval first and held-out tasks (both); availability + `addon.off` in the first milestone
(both); `orch set` for add-on fields, which does not exist today (Codex); one skill per add-on, name collisions
(both); gitignored generated skills, every worktree, reconcile at session start, text hash, quarantine (both); command
lint as an error (Opus); byte diff in the update review (Codex); `read` commands and reserved names (Opus); cap 12
and no silent truncation, Off reasons (both); hook cap raised and pre-compact line (Opus); proposals cannot create
tickets or schedule, so no blanket AP7 (Codex); one declarative screen before a node list (Codex).

# orch v2: landing, skills, connections and simple auth

Status: **draft for the owner's review**, 9 Oct 2026 (D53–D57). Challenged by an independent Codex review before
this draft; its findings are folded in. Nothing here is built yet.

## 1. Landing (merge lane), D53

Agents already work in lanes: one git worktree and branch per ticket (worktrees addon), with claims and task leases.
What is missing is **landing**: getting finished, approved work onto the target branch one at a time, so the target
never goes red unnoticed. (Seen on 8 Oct 2026: three PRs merged in parallel left CI on `develop` red.)

**The land addon** (P2) runs as its own out-of-process **landing worker** (§8 of the format: addons run out of
process), with a narrow grant: push to the ticket's own branch, merge into an allowed-targets list. `main` is never
an allowed target (D33).

**Approval binds to code.** A gate approval today binds ticket content (format §5), not a source revision. Landing
adds a **landing record** event per attempt:

```json
{"type": "land.attempt", "ticket": "...", "remote": "...", "target": "develop",
 "source_sha": "...", "target_sha": "...", "candidate_sha": "...",
 "checks": [{"name": "...", "result": "pass", "url": "..."}], "outcome": "merged | failed | requeued"}
```

- A **clean rebase** (no conflicts, no edits) keeps the approval, but checks must pass on the exact rebased
  `candidate_sha` against an unchanged `target_sha`. If the target moves, the candidate is rebuilt and re-checked.
- **Any conflict resolution** (by an agent or a person) voids the approval: the ticket goes back to review with the
  resolution diff.
- What was approved, what was tested and what was merged are the same `candidate_sha`, recorded in the event.
- **The verdict itself binds to the branch head (D58):** the verify gate's hash material includes `source_sha`,
  read by the host from git, and the Touch ID prompt shows the commit and diffstat. A new commit on the ticket branch
  after the verdict appends `gate.invalidated` (verify, `new_commits`) and sends the ticket back to testing. With
  the optional code gate on (D59), landing also needs a code approval on the same commit.

**Scheduling.**
- One queue per **(canonical remote URL, target branch)**, shared across workspaces on the same machine (file lock in
  the host state dir), so two workspaces never race on one remote.
- Only tickets whose gates are approved enter. Stacked tickets land parent first; a failed parent blocks its
  descendants (they never land with the parent's unapproved changes). No automatic restacking yet.
- Checks: the workspace's full integration checks (T2 level) on the candidate, then the PR's CI on the exact SHA,
  with a timeout. No affected-package optimisation yet.
- Failure: the ticket gets a "needs: conflict / red checks" item for its agent; the queue moves on.
- Attempts are persisted; after a crash the worker resumes from the last recorded attempt, never repeating a merge.
- Pushes: only to the ticket's own branch, with `--force-with-lease`; never a force push to a shared branch.

Start with one serial worker, not a general merge scheduler.

## 2. The relay has no pages, D54

orch-relay is an API (directory, bridge, drop, push). Every UI is in the workspace dashboard (P2) or the iPhone app
(P4). One exception: the pairing universal link `https://<relay>/pair#…` needs `apple-app-site-association` and a
**static fallback page**: no scripts that read the fragment, no analytics, no telemetry; it says "open this link on
the iPhone with orch installed" and how to pair manually (scan the QR in the app). The fragment never leaves the
device. An org admin UI, when it exists (P6), is a dashboard addon calling the relay API.

## 3. Skills and connections, D55

Two separate things:

- **Skill:** instructions plus optional scripts, in the Claude Code `SKILL.md` format (frontmatter `name`,
  `description` only), so it works unchanged in other harnesses.
- **Connection:** a named, authenticated tool or API configured by the owner in the workspace config, bound to an
  identity: `{name, kind, tool, account/tenant/endpoint or profile, check, login_hint}`. Skills only **reference**
  connections by name; they never define how to authenticate.

**Skill metadata** lives in a JSON sidecar next to `SKILL.md`, not in the frontmatter (the core has no YAML
dependency, and extra frontmatter keys are not proven harmless across harnesses):

```json
// orch.skill.json
{"schema_version": 1, "skill_version": "1.2.0", "scope": "workspace",
 "connections": ["databricks-prod"], "env": ["DATABRICKS_HOST"]}
```

- A skill without a sidecar has **unknown** needs (`orch doctor` warns), not "no needs".
- **Scopes:** built in (orch-core's skills, addons; changed by release), workspace (in the workspace repo), org (P6).
- Adding a connection reference or an env name to a skill is a **credential grant**: it needs the owner's approval,
  separate from edits to the skill's prose.
- **Org skills** (P6): signed by the org, sealed to members via the relay, pinned by hash; a new or changed org skill
  needs one owner approval per workspace before agents use it.
- Before claiming harness compatibility, a test loads an orch skill in each supported harness (Claude Code, Codex).

## 4. Auth, kept simple, D56

Owner decision: only two kinds for now.

**A. The CLI's own login** (`gh`, `databricks`, `gcloud`, `az`, …). orch never copies or stores their tokens; the
tool keeps its own store. orch only runs the connection's check. From P2, agents run as their own OS user (#287), so
the login must exist **for the user that runs the command**: a dedicated, restricted account per tool for the agent
user where possible, not the owner's personal login.

**B. Plain API tokens** in a file outside every repo:

- Path: `<host state dir>/secrets/<workspace-uuid>.env`; directory 0700, file 0600, owned by the user that runs the
  agents.
- Format: `NAME=value` lines, `#` comments, nothing else. orch **parses** it (no shell sourcing, no expansion, no
  command substitution).
- Exposure is per need: a variable reaches a process only if a skill in the current ticket declares it (§3), and
  when orch runs a tool itself it passes the variable **per invocation** with an environment allowlist, not to the
  whole agent session. Where a whole session needs it, the doc says so plainly: the agent can read it.
- Output of checks and orch-run tools is filtered for known secret values before it reaches logs, events or
  transcripts.
- Rotation: edit the file; running sessions keep the old value until restarted; `orch doctor` lists sessions started
  before the file's last change.
- Not in scope now: OS keychain for these tokens, OAuth refresh handling, org-shared secrets.

## 5. Checks and re-login, D57

- Each connection has a `check` command (`gh auth status`, `databricks current-user me`). orch runs it with a timeout,
  closed stdin and bounded output, and classifies the result: **ok**, **auth expired**, **wrong identity** (the
  check's account/tenant differs from the connection's), **service down**, **unknown**.
- When: on demand (`orch doctor`), at session start, and before an agent claims a ticket whose skills need the
  connection. No periodic polling yet.
- A failing check blocks only tickets that need that connection, and only for auth or identity failures.
- Re-login: orch shows the connection's `login_hint` to the owner (desktop in P2, phone from P4). Silent refresh only
  where the CLI does it by itself. Agents never handle logins. No passwords are ever stored. No generic device-code
  orchestration yet.

## 6. Phasing

| Phase | What |
|---|---|
| P1 (C8, C10) | Skill sidecar schema and validation; connection config schema; `orch doctor` runs checks on demand; the secrets file format and parser |
| P2 | Per-invocation env injection and output filtering; checks at session start and before claim; desktop re-login prompts; the landing worker (serial, one queue per remote + branch) |
| P4 | Re-login prompts on the phone |
| P6 | Org skills via the relay |

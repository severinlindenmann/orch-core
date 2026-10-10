# Concept: Mandates (delegated signing and an autonomous workspace)

Status: **owner approved Step 1 only (the pilot), 10 Oct 2026** — recorded as D62 in the draft spec PR #340. Steps 2–3 are future owner decisions. Revision 2, after an adversarial review by Codex (its findings are cited below as Critical/High/Medium #n).
No code changes in core yet. Origin: owner feedback item J (10 Oct 2026).

> "an option that allows me to give an agent user rights … temporary / permanent rights to sign in my name … an
> orchestrator agent that runs the whole workspace and decides and signs for me when I want to change the workspace to
> be completely autonomous"

---

## Summary (one page)

**What you get.** You can sign a **mandate**: a scoped, time-limited delegation from you to one orchestrator.
While it is in force, the **host** signs certain decisions for you (approvals, verdicts, some answers) when that
orchestrator asks, if every rule below holds. The agent never holds a key. Every such decision reads "via mandate
md_3, for Severin — no person reviewed this" and can never pass for your own signature. Today's factory charter is
the first, smallest mandate. Over three steps this grows into an autonomous workspace that runs for a week without
you and asks you only where you asked to be asked.

**What it can never do.** It cannot issue grants or mandates. It cannot change members, roles, devices or relay
pairing. It cannot touch secrets, credentials or connections, install or upgrade addons or their capabilities, or
change policies or settings. It cannot `restore`, purge, make a first send to a peer, publish publicly, or land on
`main`. It never signs the code review gate. It cannot approve a change to **protected paths**: CI, build and test
tooling, deploy config, dependency manifests, orch config, skills, hooks, and code classed as security. A person
approves those, always. It cannot override a person's "no", and that "no" stays in place until a person lifts it.

**The honest risk.** A model that can cause a signature can be talked into causing one. Within its limits, an injected
orchestrator *will* approve what it is told to. The damage is not limited to "bad code on `develop`". Code the
mandate approves runs in tests, agent sessions, CI and any deploy that follows `develop`, with whatever credentials
and network those places have. **That execution boundary is the real blast radius**, so this concept limits it
directly (§2.2) rather than trusting the model.

**What must exist first** (the host checks these at startup and refuses mandates otherwise, §2.12):
1. P2 custody: the host process holds every signing key, agents run under their own OS user, addons are isolated
   from host code and state, and the keychain or Secure Enclave backend is one the host has verified.
2. Isolated execution for mandated sessions: no deploy or production credentials, an egress allowlist, a
   sandboxed test runner, and no CI or deploy triggered from `develop` with secrets while a mandate is in force.
3. Host-minted session identities and a host-provisioned checker (§2.5).
4. Typed, core-validated effects for every delegable action (§2.4), durable counters, and a time and rollback guard
   (§2.10).

**The decision I'm asking for.** Approve **Step 1 only: a Factory-sized pilot**. It covers one epic you admit, its
children up to size m, requirements, plan and verdict approvals only, a 7-day maximum, and no renewal. It reuses the
charter you already accepted, rebuilt on the mandate machinery. Workspace-wide Autonomous (Step 3) is decided later,
only once the listed controls have run in the pilot (§5).

---

## 1. Problem and risk

**Problem.** Every gate approval, answer, verdict and addon decision waits for a person (format §5 "Signed human
events", §10.3 "Human only"). The charter removes that wait only inside one factory epic (children of size ≤ m, 25
children or 72 h; DECISIONS-LOG Task 30a, owner decision 6 and review fix 3). Anywhere else, the workspace stops at
each gate.

**Root of trust.** D41 and core §4 exist so that "an agent signs as the human" is impossible. A mandate opens a
labelled, bounded exception. The orchestrator's judgement and everything it reads are treated as **untrusted**. Only
what the host enforces counts as a control. Rate limits, tripwires and the checker *detect* problems. They do not
*authorize* anything (§2.9).

## 2. Design

### 2.1 The mandate document

```
mandate {
  id, revision, root_id,                       // revision n of mandate root_id (extensions are revisions, §2.11)
  workspace_id, issuer: "p_sev",
  orchestrator: { identity: "si_…" },          // host-minted session identity (§2.5), not a name the agent gives
  checker:      { identity: "si_…", provisioned_by: "host" },
  admitted:     [ "DEMO-0050" ],               // tickets/epics you admitted (§2.3)
  repos_paths:  { allow: ["web/src/**"], protected: "workspace default" },
  effects:      { "gate.approve:requirements": {...}, "gate.approve:plan": {...}, "verdict.pass": {...} },
  addon_pins:   { factory: "sha256:…" },        // package + manifest + effect schema hashes (§2.4)
  limits:       { see §2.8 },
  not_before, expires, max_until,              // max_until immutable across revisions
  policy_hash, people_hash, list_seq, checkpoint,
  delegation_pub
}
```

You sign it on the Mac with Touch ID (D41), under context `orch/v2/mandate|<workspace_id>`. The Touch ID prompt names
the effects, the admitted tickets and `max_until`. **Issuer:** owners only, for themselves. **Issue, extend or widen:**
Mac only. Under D49 the phone signs whenever it is unlocked, so it may only **acknowledge, suspend, stop and veto**,
and none of these widen anything.

### 2.2 Execution boundary (Critical #1)

Approved code runs before any human sees it. The design therefore bounds where it runs, not just what gets signed.

- **Isolation:** mandated sessions and their tests run under the agent OS user in a sandbox with an **egress
  allowlist** (package registry mirror, git remote, model API). They have no access to the host's state directory,
  keychain or other workspaces.
- **Credentials:** mandated sessions get only test-scoped connections (D55, D56) that the mandate names. Production or
  deploy credentials are never in reach. CI jobs and deploys that run on `develop` with secrets are paused while a
  mandate is in force, or they require a person's release. The startup check (§2.12) verifies this for the
  repos the mandate covers.
- **Protected paths:** CI workflows, build and test scripts, deploy config, dependency manifests and lockfiles,
  `AGENTS*.md`, skills, hooks, orch config, and paths classed `security`. A diff that touches any of them makes the
  verdict and landing **human-only** (`mandate.protected_path`), whatever the mandate says. The workspace sets the
  list, and you can extend it but not shrink it below the core default.
- **Stated boundary:** with these controls, an injected mandate can at most put wrong application code on
  `develop` inside the allowed paths, and run it in a sandbox without secrets. You still merge to `main` (D33).

### 2.3 Scope you admitted, not scope the agent describes (High #2, #12)

- **Admission is a human act.** A mandate covers only tickets you admitted (signed `mandate.admitted`), or children
  of an admitted epic created within that epic's budget. Labels, type and size are **frozen at admission or creation**
  and recorded in the mandate's view. A later edit does not widen scope. Size already works this way for the charter
  (HANDOVER "Charter size").
- **Security classification is frozen** and set by a person. An agent cannot reclassify, and new tickets derived from a
  `security` ticket inherit the class.
- **Paths:** a verdict or landing is refused if the diff leaves `repos_paths.allow`.
- **Cumulative limits per admission root:** total children, lines changed, files touched and rework cycles across
  every ticket under one admission. Splitting work into many small tickets therefore does not escape them.
- **Provenance, not just taint:** all content a model can see is untrusted. Core records the provenance of every
  section edit (person, mandated session, other session, `edit.external`, peer) and carries it into derived tickets.
  Content whose gated sections were last changed by someone other than a person or the mandated tree, or that came
  from a peer (D13), restricted tickets, or `edit.external`, is out of scope. Only a person's review clears it.

### 2.4 Typed effects (High #3, #13)

A mandate lists **effects**, not loose "classes". Each effect is defined in core with a schema, exact resource bounds
and what it can change. Core validates every request against it. Anything not listed is denied.

| Effect | Pilot (Step 1) | Later | Never |
|---|---|---|---|
| `gate.approve` requirements / plan | yes | yes | |
| `verdict.pass` on `source_sha` | yes (≤ m, no protected paths) | yes, with checker | |
| `gate.request_changes` | yes, at most 3 cycles per ticket | yes | |
| `question.answer` | no | only questions addressed to you and typed `choice`, with options fixed by the asker. Never free text that grants a permit, credential or D55 approval. | |
| `addon.decide` | no | only effects an addon declares in its **pinned** manifest as core effect types (e.g. `permit.once {command_hash}`), with a bounded resource. An addon update unpins it, and the mandate stops covering it until you re-sign. | `permit.epic`/standing permits, anything that maps to a "never" effect |
| `land.enqueue` | no | yes, to listed targets, with checker | `main` |
| `ticket.close` / `reopen` / `ticket.create` | no | create only under an admitted epic within budget. Close only for tickets the mandate itself delivered. | dismissing findings, questions or vetoes that are open, or tickets classed `security` |
| `gate.approve:code` | | | always human |
| grants, mandates, members/roles/devices, relay, secrets/connections/skill credentials, addon install/grant/update, policies/settings, `restore`, purge, first peer send, public publish | | | **never**, by any path, including addon effects and answers |

### 2.5 Identities (High #4)

- The host **mints** a session credential for the orchestrator and for each subagent. Lineage (parent → child) is
  recorded by the host and is immutable. `ORCH_SESSION` names are a display only.
- **Separation of duties:** the session that approves or judges must be outside the authoring lineage. A verdict is
  never given by the tree that ran the tasks.
- **The checker is host-provisioned.** Its identity, mandate, model and harness are fixed in the mandate. The host
  builds its input from content hashes: diff at `source_sha`, acceptance criteria, evidence artifacts. The
  orchestrator cannot pick, prompt or feed it. The checker is a **second opinion, not an authorization boundary**
  (Codex Q4). It can stop a decision, but it cannot authorize anything the mandate does not already allow. To
  override a checker refusal, a person must sign an explicit override with a written reason.

### 2.6 What exactly is signed (High #6)

- **Canonical payload**, JCS: `{decision_id (ULID, durable), effect, args, workspace_id, uid, gate, content_hash,
  source_sha, target, mandate_id, mandate_revision, mandate_hash, policy_hash, people_hash, list_seq,
  workspace_checkpoint, based_on}`. `dsig` (context `orch/v2/sig/mandate-event|…`) and any second signature (checker
  or phone tap) are over **the same digest**.
- **Replay:** core refuses a `decision_id` it has already seen, a stale `based_on`, checkpoint or policy, and any
  digest presented in another ticket or workspace.
- **Atomic reservation:** the host checks limits, reserves the budget, appends the event and commits the counters in
  one transaction under the store lock. If the append fails, the reservation is released, and retries count.

### 2.7 Human vetoes, quorum, eligibility (High #9, #15)

- **A veto persists.** When a person requests changes, refuses, or answers "no" on a ticket, the ticket gets a
  `veto` flag. It survives edits, close and reopen, derived tickets and mandate replacement until an eligible person
  clears it (`veto.cleared`). Core checks the flag at signing and again at execution.
- **Eligibility is preserved.** A mandate seat counts only if you, the issuer, would be eligible now: current
  member with an eligible role, not an assignee since the content changed, under the current `policy_hash` (format §5).
  If you lose that authority, every mandate you issued is suspended.
- **Quorum:** count distinct eligible *people*. A mandate fills at most your one seat. `human_min` (new field in the
  T5 gate policy) counts only people, and checker signatures never count toward it.

### 2.8 Budgets that bound damage, not signatures (High #10)

- **Aggregate limits** apply per owner across all their mandates and per workspace. Examples: decisions per day, admitted
  tickets in flight, lines changed per day, landings per day, agent hours.
- **Resource caps per action** are enforced through the execution grant (session runtime, CPU time, egress bytes).
  Retries, rework cycles and descendant sessions count toward them.
- **Money:** not shown as a limit until the usage addon can enforce it. A limit that is displayed but not enforced
  would mislead you.
- When a limit is reached, the mandate is suspended (`mandate.suspended {reason: limit}`). It does not quietly stop
  one effect.

### 2.9 Rates and tripwires: detection only (Medium #11)

Core keeps rolling aggregate rates per effect and per admission root. It suspends on: repeated refusals,
approvals of content changed only moments earlier by any non-person source (judged by provenance, not just "same
session"), rework loops past the cap, or any request for a "never" effect. A suspension adds a "needs you" item on
Today. None of these checks is counted as a reason the system is safe.

### 2.10 Time, restart, rollback (High #14)

Deadlines are stored in UTC. Durations are measured on a monotonic clock that survives restarts. Counters and seen
decision ids are durable and covered by the WSK checkpoint. If the host detects a clock jump, NTP loss (chrony,
Part B), a restored state, or a checkpoint lower than the relay's, it **suspends** every mandate until you re-acknowledge
it on the Mac.

### 2.11 Duration: acknowledging and extending are different acts (High #8)

- `expires` is the current end, and `max_until` is a hard ceiling fixed at the first issuance. Neither the phone nor
  an acknowledgement can move it.
- **Weekly acknowledgement** (phone allowed) only confirms that you have seen the digest. Without it, the mandate is
  suspended. An acknowledgement never extends `expires`.
- **Extension** is a new `revision` of the same `root_id`, signed on the Mac. It is bound to the original, keeps the
  lifetime counters, and pins the current policy and addon semantics. If anything widens, the Touch ID prompt shows
  the difference.
- "Permanent" therefore means a chain of extensions you sign, each at most 30 days, under a `max_until` of at most
  180 days. Step 1 has no extensions at all.

### 2.12 Custody prerequisites, enforced (High #5)

At startup and before every mandate is issued, the host runs `mandate.preflight` and refuses mandates
(`mandate.custody_unsupported`) unless every check passes:
- the host process runs under its own user;
- every agent runs under the agent user;
- addons run out of process without read access to host code, state or keychain items;
- the delegation key is non-exportable in a verified backend;
- the execution sandbox and credential checks from §2.2 pass for the covered repos.

The result is shown in Settings → Autonomy. If a check fails, issuance is disabled. It is never a warning you can
click past.

### 2.13 Stop (High #7)

- **Stop** (dashboard, phone, CLI) is a signed request. The host **acknowledges** it with a `mandate.stopped
  {boundary_seq}` event. Nothing after `boundary_seq` is signed. Queued effects the mandate authorized are cancelled:
  landing entries are dequeued, pending checker runs end, and with "also stop agents" sessions are stopped and the
  orchestrator's grant is revoked.
- **Execution-time recheck:** the land worker and other executors re-verify the mandate, veto and policy just before
  acting, not only when the decision was signed.
- **Remote stops:** if the host cannot be reached, the phone shows "Stop sent, not yet acknowledged". The host only
  signs while it holds a **lease** from the relay that it renews every 10 minutes. If the lease cannot be renewed, the
  mandate is suspended, so an offline host cannot keep signing for longer than one lease.
- **Revoke and void:** `gate.invalidated {cause: mandate_revoked}` for every mandate decision on work that has not
  landed, through the existing D53 void path. Landed work is listed for your review.

### 2.14 Conflicts and code review / D53 / D41 / D49

- A person's decision always wins, and their "no" persists (§2.7). In a race, the safer decision stands.
- **Code review gate:** never delegable. If you turn it on for chosen types, it marks where you want to look.
- **D53:** unchanged. The verdict signs `source_sha`, new commits void it, and the land worker uses the signed
  commit and re-checks the mandate right before merging (§2.13). `main` is never a target.
- **D41:** every *human* signature still needs Touch ID. A mandate signature is a third kind (`presence: "none"`,
  `via: "mandate"`). Its human moments, issuance and extension, use Touch ID. **D49:** the phone acknowledges, stops
  and vetoes. Phone batch taps (a second key for later steps) show the **exact effect and the immutable item digest**
  per item and sign that digest, not a summary.

## 3. How it shows in the dashboard

- **Shell banner** (calm, not orange): "Mandate md_3 · for Severin · epic DEMO-0050 · 9 decisions · until Fri ·
  **Stop**". After Stop: "Stopping… / Stopped at #1842".
- **Today:** a "Decided for you" digest with "Looks right", "Veto", and "Revoke and void". Below it, everything the
  mandate refused or skipped (protected path, provenance, veto, limit) as normal "needs you" items.
- **Labels everywhere** (gates strip, History, verdict line, factory Children, Ready report): "Verdict: via mandate
  md_3, for Severin — no person reviewed this (commit b7e1f02)", plus "checked by checker si_… (refused/passed)".
- **Agents → Mandates:** mandates with revisions, limits as meters, preflight status, the decision log, stops and
  their acknowledgement.
- **Settings → Autonomy:** preflight, protected paths, admission, and the presets.

## 4. Alternatives

| Option | Verdict |
|---|---|
| A. Nothing beyond the charter | Safe, but does not meet the request. |
| B. Widen the charter | Rejected: same power without effects, identities, vetoes or the execution boundary. |
| C. Deterministic auto-approve rules on owner-admitted, low-impact paths | **Adopted as a component.** Effects carry deterministic preconditions (checks green, paths allowed, size). Not enough on its own, because it cannot judge requirements. |
| D. Phone batch approvals | Kept as an override and second key, signing exact digests. Not autonomy. |
| E. Autonomous preparation + human release | **The fallback** if Step 3 is never approved. Agents do everything up to the verdict, and you release batches. |
| F. Single-epic capability (Codex) | **Adopted as Step 1.** |
| G. Hand the agent your real signature | Rejected. Breaks D41 and core §4. |

## 5. Rollout

1. **Step 1, Factory pilot (P2):** one admitted epic, children ≤ m, effects requirements, plan, verdict and
   request-changes (3 cycles). No answers, addon decisions, close/reopen, landing or extension. 7 days maximum. Built
   with full controls: preflight, execution boundary, protected paths, host-minted identities, canonical payload,
   reservation, vetoes, Stop with acknowledgement, time guard. The charter becomes this mandate, and old records keep
   `via: "factory_charter"`.
2. **Step 2, Assist:** several admitted epics, typed `question.answer` (choice only), `addon.decide` for pinned
   typed effects, checker on verdicts, weekly acknowledgement, extensions up to 30 days.
3. **Step 3, Autonomous (workspace-wide):** decided separately, and only after Steps 1–2 have run with no gap in these
   gating controls: the relay lease, execution-time recheck in the land worker, aggregate limits enforced through
   execution grants, provenance carried across derived tickets, enforced sandbox and credential checks for every
   covered repo, and `land.enqueue` with the checker. Admission becomes "admit by rule" (types and paths you sign),
   still frozen at admission.

## 6. Open questions for the owner

1. Approve Step 1 as scoped above?
2. Owners only as issuers (recommended), or maintainers too, for Step 1 only?
3. Is the default protected-path list right (CI, build/test, deploy, manifests and lockfiles, orch config, skills,
   hooks, security-classed code)?
4. CI/deploy from `develop` while a mandate is in force: pause it (recommended), or require a per-run release?
5. Checker: must it be a different model from the orchestrator?
6. Ceiling for "permanent": 180-day `max_until` with 30-day extensions and a weekly acknowledgement?
7. Who may suspend your mandate: any member with Operate scope (recommended), or only you?
8. Multi-person workspaces (D39): should a mandate seat ever count where other people are listed approvers?

## 7. Format and core changes (all provisional; event types are unsettled, PR #336 / #338)

- **Workspace events:** `mandate.issued`, `mandate.extended` (revision), `mandate.acknowledged`, `mandate.admitted`,
  `mandate.suspended {reason}`, `mandate.resumed`, `mandate.stop_requested`, `mandate.stopped {boundary_seq}`,
  `mandate.revoked {void_unlanded, stop_agents}`, `mandate.tripped`, `mandate.preflight`.
- **Ticket events:** existing decision events gain `via: "mandate"`, `decision_id`, `mandate_id`,
  `mandate_revision`, `mandate_hash`, `presence: "none"`, `dsig`, and `second? {via, actor, sig}` over the same
  digest. `veto.set` / `veto.cleared` (human). `checker.override` (human, with a reason). `gate.invalidated` gains
  `cause: "mandate_revoked"`. Section edits carry `provenance`. Legacy `via: "factory_charter"` is read-only.
- **Format §5:** a third signature kind (`dsig`) and the canonical decision payload. "Who may approve" gains the mandate
  rules (§2.5, §2.7). T5 policy gains `human_min` (people only). Protected-path and security classification are
  frozen fields.
- **Core §3:** new `who: mandate` (needs a host-minted session credential, a grant, and a mandate covering the
  typed effect). New ops: `mandate.issue`, `mandate.extend`, `mandate.admit` (human, Mac); `mandate.acknowledge`,
  `mandate.stop`, `veto.set`/`clear` (human, phone allowed); `mandate.resume`, `mandate.revoke`, `checker.override`
  (human); `mandate.preflight`, `mandate.check`, `mandate.list` (read). Core keeps a new **effect registry** (schemas,
  bounds, the "never" set) alongside the operation registry. New errors include `mandate.custody_unsupported`,
  `mandate.protected_path`, `mandate.not_admitted`, `mandate.provenance`, `mandate.veto`, `mandate.replay`,
  `mandate.limit`, `mandate.stopped`, `mandate.time_uncertain` and `mandate.effect_unknown`.
- **Core §4:** a new row, "A mandated agent is prompt-injected → it can decide only typed effects on admitted work,
  outside protected paths, within aggregate limits, and the result runs only in a sandbox without secrets. It is
  labelled, vetoable, stoppable with acknowledgement, and voidable before landing. Nothing reaches `main`."

## 8. Changes after Codex review

All 15 findings were adopted. Where an adoption is only partial, the row says why.

| # | Finding | Response |
|---|---|---|
| 1 | "Maximum damage" too narrow | Replaced by the execution boundary (§2.2): isolation, egress allowlist, no deploy credentials, CI/deploy paused, protected paths human-only. |
| 2 | Agent-editable scope, splitting | Owner admission, frozen labels, size and security class, repo/path allow-list, cumulative limits per admission root (§2.3). |
| 3 | Addon decisions confer powers | Typed core effects, pinned package and schema hashes, unknown effects denied, "never" applies on every path (§2.4). Addon decisions are not in Step 1. |
| 4 | Session names ≠ identity | Host-minted credentials, immutable lineage, host-provisioned checker (§2.5). |
| 5 | Custody prerequisites | `mandate.preflight` refuses issuance on unsupported configs (§2.12). |
| 6 | Replay / cross-log | Canonical payload, one digest for all signatures, durable decision ids, atomic reservation (§2.6). |
| 7 | Stop leaves work running | Acknowledged boundary, queued effects cancelled, execution-time recheck, relay lease (§2.13). |
| 8 | Renewal extends authority | Acknowledgement separate from extension, revisions bound to the root, immutable `max_until`, lifetime counters (§2.11). |
| 9 | Veto persistence | Persistent `veto` flag, cleared only by a person, rechecked at execution (§2.7). |
| 10 | Budgets bound signatures | Aggregate limits, reservation, retries and descendants counted, caps through execution grants. Money is not shown until enforced (§2.8). |
| 11 | Rates are detection | Stated as detection only, with rolling aggregates, provenance-aware freshness and rework caps (§2.9). |
| 12 | Taint too narrow | Provenance on every edit, inherited by derived tickets, all model-visible content untrusted, cleared only by a person (§2.3). |
| 13 | Lifecycle bypasses | No dismissal of open findings or vetoes. Vetoes survive reopen. Cycle and creation caps. Answers typed and unable to grant (§2.4). |
| 14 | Clock / restart | UTC plus monotonic time, durable counters, rollback detection, suspend when time is uncertain (§2.10). |
| 15 | Quorum / eligibility | Eligibility checked now, suspend on lost authority, distinct people counted, `human_min` counts people only (§2.7). |
| Q4 | Checker / phone batch | Checker is a second opinion only. A refusal needs a signed human override with a reason. Phone taps sign exact item digests (§2.5, §2.14). |
| Q5 | Alternatives | Single-epic capability is now Step 1. Preparation + human release is the fallback. Deterministic rules are a component (§4, §5). |

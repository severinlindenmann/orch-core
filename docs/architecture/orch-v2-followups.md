# orch v2: Schedules follow-ups (the `follow-up` kind)

Status: **draft, 21 Oct 2026** (orch-core #714, #715-#720; orch-relay #81). Written from the Opus design check of
21 Oct 2026 and the owner decisions of 19 Oct 2026. The pure rules (§3) are built in `orch/model/followups.py`; the
engine, ops/CLI (`orch follow-up`), runner one-run grant slot and host runner (time triggers) are built (parts 1 and 2); event, webhook and add-on triggers and the skill are built (below); the relay inbox is not. The owner answered Q1, Q2, Q5 and Q7 on 21 Oct 2026 (the recommended answers, §9); the numbers are named constants
in the code.

**Built (27 Oct 2026, #717/#719):** the event shape `{source, kind, ref, status, at}` (`model/followup_events.py`: orch kinds
question.asked/answered, ticket.changed/closed, gate.approved/changes_requested, task.done; webhook hook.rang; add-on events `<addon>.<kind>` are built in #718: addon-api 1.2, the host polls only while a follow-up waits),
`orch follow-up --on question.answered | ticket.changed:testing | webhook:<id>` (`--in/--at` is then the deadline), `orch webhook new`
(a doorbell address, shown once; only `H(id)` is stored), the host receiver (`host/hooks.py`), the deadline fallback with `follow-up show`
printing the reason before the brief, and the work-skill text (`orch help wait`). Laptop delivery through the relay is #1285.

## 1. What it is

An agent working on a ticket can schedule **its own next session** on that ticket: "look at the backfill again on
Tuesday 09:00", or "when job 8812 finishes, latest Tuesday 09:00". The agent sets it itself; enabling the Schedules
add-on is the person's opt-in. The ticket stays `in_progress` while it waits. At the due time (or when the event comes,
or at the deadline as a fallback) the workspace's host starts a fresh session that reads the agent's own brief from the
ledger and continues. Every outcome is shown with a reason.

A follow-up runs **once**, as the person whose agent set it, with **at most that agent's rights**, on **that ticket
only**. No new person-signed grant exists; the follow-up grant is derived by the model (FU1).

## 2. Terms

| Term | Meaning |
|---|---|
| follow-up | `fu_<ULID>`, on one ticket, created by `followup.set` |
| parent grant | the grant of the agent that appended `followup.set` (a person's grant or an earlier follow-up grant) |
| root grant | the person-issued grant at the top of that chain of grants |
| follow-up grant (G') | `gr_<ULID>`, derived at `followup.fired`; never a `grant.issued` event |
| chain | the follow-ups set on a ticket since the last person event on that ticket; a person event resets it |
| hook | a webhook doorbell, `hk_` plus 128 random bits; only `H(id)` is stored anywhere (label `orch/v2/hook|`) |

## 3. Invariants FU1 to FU16

"Model" means: checked identically at append and at replay. Each rule is enforced in one place.

| # | Rule | One place |
|---|---|---|
| **FU1 No widening** | G' = `derive(parent, set, fired)`: person = parent.person; scope = this ticket's uid only; verbs = parent.verbs; duration = min(parent duration, `FU_RUN_HOURS`, Q2); issued_at = fired.at; expires_at = issued_at + duration; `secret_hash` from fired. No field of `followup.set` or `followup.fired` can choose scope, verbs, hours or person. | `model/followups.derive_grant`, called from the engine's `followup.fired` handler |
| **FU2 One ticket, one session family** | A G' event is refused on any other ticket log or on the workspace log, and from a session outside the family (`claims.in_family`) of the first session that used G'. | `model/authz.authorize_agent`, follow-up branch (T7) |
| **FU3 One run, then over** | At most one `followup.fired` per follow-up, and only from `pending`. G' is invalid from its `followup.outcome` position or from expires_at, whichever comes first. "Used up" is a ledger fact, not a host flag. | `model/followups.next_state`, `fgrant_usable` |
| **FU4 Revocation follows the chain; expiry does not** | G', and the firing of a pending follow-up, need every ancestor up to the root grant unrevoked and the root person still a non-viewer member. `grant.revoked` of any ancestor (or of G' itself) cancels the pending descendants. When the parent grant expires, nothing ends (owner, 19 Oct). | `model/followups.chain_valid`, `ancestors` |
| **FU5 Who may set one** | The actor of `followup.set` is an agent with a grant (never `unattended`), holds the live claim on the ticket (in family), works on an `in_progress` ticket, and acts while `schedules` is enabled with follow-ups. The setting has due >= at + 1 h, due <= deadline <= at + `max_days`, a deadline always for event triggers, and a brief of at most 4 KiB that passes the text rules (NFC, no control or bidi characters). A new set replaces the pending one, and `followup.set` releases the setter's claim in the same event. | `model/followups.check_set` |
| **FU6 Caps come from the signed enable** | `max_days`, `chain_max`, `pending_max` and `runs_per_day` are workspace settings written in the owner-signed `addon_enable` review (#737). The code ceilings are 14 days / 5 without progress / 20 pending / 24 runs a day; a setting can lower a cap, never raise it past the ceiling. | `model/followups.effective_caps` |
| **FU7 Chain cap** | A chain counts follow-ups since the last person event. The count resets on `task.done` only for a task that existed at the chain's first `followup.set` (an agent cannot add a task and finish it to reset). At the cap the set is accepted with state `needs_person`: the host never fires it, and Today shows Run now / Reschedule / Dismiss. Absolute ceiling (owner decision 21 Oct, Q1): 30 days or 10 follow-ups since the last person action, never reset by progress. | `model/followups.chain_state`, `set_state` |
| **FU8 Only the workspace's host fires, inside the window** | `followup.fired` is H (`host_sig`) and names a pending follow-up. Time trigger: fired.at in [due - 300 s, deadline + 24 h]. Event, add-on or webhook trigger: fired.at in [set.at, deadline + 24 h]. `trigger` is one of `time`, `orch`, `addon`, `webhook`, `deadline`. For `deadline`, fired.at in [deadline - 300 s, deadline + 24 h] and a `fallback` reason from the closed list of §6. | `model/followups.check_fired` |
| **FU9 Secret custody** | The G' secret is drawn at fire time in host memory; its hash goes into `followup.fired`. It is sent once as `grant.put_run {run, grant, secret, until}` to a runner slot keyed by run id, and the `spawn` of that run consumes it. It is never on disk, never in `followup.set`, a run record, a report or a log line (existing `Redactor` / `RedactFilter`). Fire order: (1) the run intent is persisted, (2) `followup.fired` is appended, (3) `put_run` and `spawn` are sent. A crash between 2 and 3 gives `did_not_run: host_restarted`, and the follow-up is never fired again. | `host/followups.fire` (host), `runner/grants.put_run` (runner) |
| **FU10 Closed launch, no outside text in a prompt** | The session starts in a new closed mode `followup` in `host/launch.py`. The prompt is a fixed template with the ticket key and the follow-up id; the brief is read from the ledger under G' and is never in argv. A fallback reason is a closed code rendered by core. Nothing from a webhook or an add-on reaches a prompt. Harness and model are re-validated at fire time against the closed registry. | `host/launch.py` |
| **FU11 A doorbell carries nothing** | A ring is (`H(hook id)`, the relay's receive time). The relay never reads past the headers, never stores or forwards a body or signature, and has no status field. A hook belongs to at most one pending follow-up at a time. A ring that finds nothing pending is dropped without a ledger write. | orch-relay `hooks` module; FU3 |
| **FU12 Hook id secrecy** | The hook id is printed once to the agent. The ledger (`webhook.created {hook_hash, label, expires}`), the host state and the relay store only `H(id)`. A hook expires at most 14 days after creation, is revoked at the relay when its chain ends or a person revokes it, and may be re-bound by the next `followup.set` of the same chain. | `host/followups` hooks |
| **FU13 Relay inbox auth** | Register, revoke and fetch are host requests signed with WSK for that workspace only (push/drop pattern). The public `POST /h/<id>` accepts only registered, unexpired hashes; answers 204 with the same body and timing for known and unknown ids; refuses a body over 64 KiB unread; rate-limits per hook, per source IP and per workspace; keeps one inbox row per hook (the first ring) and at most 256 rows per workspace. The id does not encode the workspace. | orch-relay `hooks` module |
| **FU14 Every outcome is a fact with a closed reason** | Each state other than `pending` is `followup.outcome {outcome: ran, failed or did_not_run, reason, run?}` (H), `followup.cancelled {followup, reason}` (P or A), or a model-derived cancel. Reasons come from the closed lists of §6; the UI shows only these. | `model/followups.REASONS`, `DERIVED_CANCEL_REASONS` |
| **FU15 Clocks** | `due` and `deadline` are UTC; the setter's CLI resolves `--at "+2d 09:00"` in local time once, at set time. The host fires by wall clock: every tick (60 s or less) it compares the wall-clock delta with the continuous clock (`CLOCK_BOOTTIME` / `mach_continuous_time`); a jump of more than 5 min without sleep is a clock change, is logged, nothing fires on that tick, and it shows on Schedules. After sleep, overdue follow-ups fire marked `late by ...`, at most 2 at once, in due order; past deadline + 24 h they get `did_not_run: no_host`. | `host/followups` timer |
| **FU16 What the person signs is what runs** | Enabling Schedules goes through `addon_enable`, owner only. Its review states the delegated power in words with the numbers from the signed settings (text in §8). Nothing about follow-ups is signed per run. | `addon_enable` review lines (#737) |

## 4. Events

All are core types on the **ticket log**, not `schedules.*` add-on events, because the grant derivation must live in
the model. Actor classes: P person (signed), A agent or person, G agent with a grant only, H the workspace's host
(`host_sig`).

| Event | Actor | Payload | Effect |
|---|---|---|---|
| `followup.set` | G | `{id, trigger, due?, deadline?, brief, hook?}` | creates a follow-up `pending` (or `needs_person` at the chain cap, FU7); replaces the pending one (`cancelled: replaced`); releases the setter's claim in the same event. The ticket stays `in_progress`: "waiting" is derived. Attested by the host like every G event; it carries **no** `secret_hash` and no signature. |
| `followup.fired` | H | `{id, grant, trigger, fallback?, secret_hash, run}` | `pending` to `fired`; the engine derives G' with `derive_grant` (FU1) and stores it in `ws.grants`. |
| `followup.outcome` | H | `{id, outcome, reason?, run?}` | `fired` to `ran`, `failed` or `did_not_run` (a `pending` one may become `did_not_run`, e.g. `no_host`). Ends G'. |
| `followup.cancelled` | P or A | `{followup, reason}` (`followup` is the follow-up ID; a person signs it: the follow-up's person or a manager, reason `person`) | `pending` or `needs_person` to `cancelled`. A person's Cancel or Run now is signed (Q6). |
| `followup.restored` | P | `{followup}` | Undo a person cancellation by its canceller within 120 seconds and at or before the effective deadline; no other live or newer-set follow-up. Restore the pre-cancel state; derived cancellation still applies. |
| `followup.rescheduled` | P | `{followup, due?, deadline?}` | Moves pending times within set caps (person or manager); only its own person resumes `needs_person` to pending and resets the chain. Time triggers require due and retain max(old deadline, due) when deadline is omitted; event triggers require deadline and forbid due. |
| `webhook.created` | G | `{hook_hash, label, expires}` | records a hook; the id is shown once. |
| `webhook.revoked` | P or A | `{hook_hash}` | ends a hook. |

The four implemented events use `followup` for the payload follow-up ID (`fu_<ULID>`); the envelope `id` remains a unique event ULID. The `hook` field is not implemented.

A model-derived cancel writes no event; the engine derives it on replay with `derived_cancel_reason`.

## 5. Chain state

`chain_state` takes the ticket events since the last person event: each `followup.set` (time) and each `task.done`
(time, task). `tasks_at_start` is the set of tasks that existed at the first `followup.set` of the chain.

* **Progress** is a `task.done` of a task in `tasks_at_start`, after the first set. Tasks added later never count.
* `without_progress` is the number of sets after the last progress; every set counts, including a replacing one.
* The chain is `needs_person` (`chain_cap`) when `without_progress >= chain_max`, or the total reaches the ceiling
  count, or more than the ceiling days passed since the chain start (below). The ceilings are never reset by progress
  (owner decision 21 Oct, Q1: 30 days, 10 follow-ups).
* The ceilings count from the last person event on the ticket (`since`, the chain start); without it, from the first set.
* A new set is `pending` unless the chain before it is already `needs_person`, or its times lie beyond the ceiling.

## 6. States and closed reason lists

States: `pending`, `needs_person`, `fired`, `ran`, `failed`, `did_not_run`, `cancelled`. Transitions:

* `pending` to `fired` (once), to `cancelled`, to `did_not_run`;
* `needs_person` to `cancelled` (Run now, Cancel, or a derived cancel); it never fires;
* `fired` to `ran`, `failed` or `did_not_run`;
* all others are terminal.

**Outcome reasons** (`failed` and `did_not_run`): `no_host`, `harness_failed`, `budget`, `runner_unavailable`,
`runner_restarted`, `host_restarted`, `grant_revoked`, `ticket_moved_on`, `no_writes`, `error`. `ran` has no reason.
A run that wrote nothing is `failed: no_writes`.

**Derived cancel reasons**: `replaced`, `ticket_left_in_progress`, `claimed_by_other`, `grant_revoked`,
`member_removed`, `addon_disabled`, `chain_cap`. Parent grant expiry is **not** a reason. The setter's own end of
session or unclaim does not cancel (it is how the follow-up works).

**Fallback reasons** (trigger `deadline`): `no_event` (the `orch` event never came), `no_ring` (the webhook never
rang), `addon_silent` (the add-on event never came). A time follow-up has no deadline fallback: it fires at its due, and a later deadline only widens its late window (FU8).

**Late**: a follow-up fired after its due is shown `late by <duration>`; this is display, not a state.

## 7. Where the pieces live

* Pure rules: `packages/core/src/orch/model/followups.py` (no clock, no I/O, no engine import).
* Engine wiring: event types, schemas, `ACTOR_RULES`, emits, derived G' in `ws.grants`, `authorize_agent` follow-up
  branch (T6, T7).
* Host timer `host/followups.py` (index from ledger, ticks, jump detection, triggers, budgets of 2 at once and
  `runs_per_day`, late and `no_host`), fire and launch (FU9, FU10). One host per workspace (`ws.host_id`): only it
  appends `followup.fired`.
* Runner: `grant.put_run` one-shot slot keyed by run id, never the person's slot.
* Relay: hooks inbox, relay inbox only in v1 (laptop and VPS alike); no HMAC, no status from a ring.
* Runtime state `<state>/hosts/<ws>/followups.json` (0600 in 0700): timer index and hook map (hashes only),
  rebuildable from the ledger, never a secret.

## 8. What the person signs

The `addon_enable` review for Schedules says: "Agents working for anyone in this workspace (members too) may
schedule their own follow-up session on the ticket they work on, up to <max_days> days ahead, without asking. Each
follow-up runs once, as the person whose agent set it, with at most that agent's rights, on that ticket only, for at
most <hours> h. After <chain_max> follow-ups with no task done, orch asks a person. A follow-up can have a web address
that wakes it early: anyone with the address can wake it, and nothing they send is read. Revoking an agent's grant
cancels its follow-ups."

"Run now" by a person cancels the follow-up and starts the agent the normal way with the person's own runner grant
(`agent.start`, mode `followup`); G' is not used.

## 9. Owner questions (Q1, Q2, Q5, Q7 decided on 21 Oct 2026)

| Q | Question | Answer (decided ones in bold; constants in code) |
|---|---|---|
| Q1 | Chain ceiling | **decided:** progress counts only tasks that existed at chain start; a chain also ends after 30 days or 10 follow-ups (`FU_CHAIN_CEILING_DAYS`, `FU_CHAIN_CEILING_COUNT`) |
| Q2 | How long a follow-up session may run | **decided:** the parent grant's length, at most 8 h (`FU_RUN_HOURS`) |
| Q3 | Webhooks on a VPS | relay inbox later (#1285); **built (#717):** direct receive on the host, `orch host run --hook-listen HOST:PORT --hook-url URL` |
| Q4 | Optional HMAC | **built (#717), direct receive only:** GitHub `X-Hub-Signature-256`, secret kept in `<state>/hosts/hook-secrets.json` by `orch host hook-secret <id>`; the body is read (64 KiB) only to verify, then dropped |
| Q5 | Budgets | **decided:** 20 pending per workspace, 24 runs a day, 2 at once (`FU_PENDING_MAX`, `FU_RUNS_PER_DAY`, `FU_CONCURRENT_RUNS`) |
| Q6 | Cancel and Reschedule by a person are signed events | yes |
| Q7 | Another session claims the waiting ticket | **decided:** cancel ("someone picked the ticket up") |
| Q8 | First add-on events | Databricks run finished, GitHub checks finished, after C9 |

## 10. Threat answers

* **Widened, replayed or outliving its run?** Widened: no (FU1, one pure function; the host only supplies `at`,
  `secret_hash` and the run). Replayed: no (FU3; the secret exists only in the runner slot and the agent's
  environment). Outliving: no (ends at `followup.outcome` or expiry). Subagents: FU2. Residual: the session holds its
  secret until it exits or expires, like every agent today.
* **Scheduling beyond the caps?** Horizon, one pending per ticket and claim-holder only: FU5. Chain: FU7, not
  resettable through tasks the agent adds. Per workspace: FU6 and the host budgets. Remaining gap: a chain with real
  progress has no end except the Q1 ceiling.
* **A webhook carrying data or triggering more than one run?** No (FU11, FU3, FU10). A leaked URL can only wake the
  follow-up early.
* **Laptop asleep or clock changes?** FU15. A wrong host clock can fire a follow-up early, no worse than an early
  ring; the model checks FU8 only for consistency.

## Real-host end to end, and the dev shortcuts (26 Oct 2026)

`packages/core/tests/host/test_followups_real_host.py` (slow, about 5 minutes) drives a real dev-sandbox host and a real
foreground runner with a fake harness: set, fire, brief read through the one-run grant (never argv), outcome `ran`, cancel,
host restart across the due time (fires late), a chained follow-up, the add-on turned off. Only a dev sandbox workspace (signed
genesis prefix `SBX`, never a real one) gets the shortcuts that make it fast: a 30 s minimum lead instead of FU5's hour, no
early-fire tolerance (FU8's 5 minutes), and a 2 s timer tick instead of 30 s. A follow-up run needs the `schedules` add-on and
an enabled runner, not the `start-agent` add-on.

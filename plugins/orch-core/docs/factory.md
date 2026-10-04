# AI Factory (phase 1)

One epic in, finished work out: you write an epic and start it as a factory, and agents split, specify,
auto-approve and build its children. You hear from them when they need a permission they do not hold, and at the
end for the verdict. Issue #2 tracks the whole feature; this page describes what phase 1 ships.

AI Factory is **off by default**. Phase 1 works from the terminal only; the dashboard cards, the runner and the
Ready and Stopped reports follow in later phases.

## Switching it on

Set `factory.enabled` to `true` in `orchestrator/config.json`. That alone changes nothing: a factory epic needs
your signed start, and switching the factory off again turns its epics back into ordinary delegated epics.

## Starting a factory epic (you only)

```bash
orch approve <epic> requirements --factory
```

This is the epic approval you already know, with delegation, run in your own terminal with the typed
confirmation (owner decision D7). It signs one charter into the ledger with the factory's limits:

- up to **25 children or 72 hours**, whichever comes first (D5; `--max-children` changes the count);
- children of **size m or smaller** only; larger ones wait for you (D6);
- everything the existing delegation already rules: agents auto-approve only children they wrote, requirements and
  plan, once per child, while the epic text is unchanged and the delegation is not paused.

When the time budget is used up, agents stop: no new auto-approvals, no claims or task starts on its children, and
grants answer nothing until you decide again. A used-up budget (time or children) is a card in `orch permit list`;
`orch epic show <epic>` shows the state.

The child count also comes from one marker per auto-approved child, kept beside the ledger outside the repository,
not only from the event log or ticket files, so editing those cannot understate it. The budget is decided by the
signed charter alone: the factory switch does not lift it. At the limit, a child the delegation approved goes on;
any other child without a human approval is refused a claim or task start.

## What changes for agents in a factory epic

- `orch ask` is refused. The agent decides within the epic's text and records why in the ticket log (a note, never
  an answer), or leaves the item out and lists it as not built.
- A missing permission is a request, not a question: `orch permit request "<command>" --ticket <id> --reason ...`.
  It signs nothing and grants nothing.
- The verdict stays yours (D1): children go to testing as usual, and you sign the epic's verdict.

## Permissions: one system, answered by you

orch-core's plugin registers a Claude Code `PermissionRequest` hook (`orch permit hook`). Outside a factory session,
or with the factory off, it gives no answer and the harness asks you as usual (with the factory off it reads only
the workspace config). A factory session is one whose claimed tickets all belong to one factory epic; a session
with claims in more than one factory epic gets no answer. Binding a session to its epic when you start it is the
runner's job, in a later phase. In a factory session:

- a **live signed grant** for this exact command answers `allow`;
- otherwise the hook files a request (one per epic and command while it is open) and answers `deny` with "waiting
  for permission P-n", so that child parks and the others go on;
- an error, an unreadable ledger or anything unexpected never answers `allow`;
- prompts for anything other than a shell command are denied, so run factory sessions in a permission mode that
  does not prompt for file edits (accept edits, or auto mode).

A request's command text and reason are kept beside the ledger in your orch config dir, not in the repository; the
event log only records that a request with that id and command hash was filed. A once grant's use is recorded there
too, so every checkout of the workspace sees it. The guard keeps agents away from these records as it does from
the ledger.

You answer requests in your own terminal:

```bash
orch permit list                     # open requests and every grant with its state
orch permit grant P-7                # once: used up by the first matching prompt
orch permit grant P-7 --for-epic     # every prompt for this exact command, while the factory is active
orch permit deny P-7
orch permit revoke <grant id>        # an action already running finishes; the next one asks
```

Each answer prints the full command and needs the typed request id. Grants, denials and revocations are signed
ledger entries; agents cannot write them (the commands are human-only in orch itself, and the guard denies them).
A grant binds the epic, the delegation it was given under, the exact command text and its sha256. There are no
wildcards. A grant for the epic ends when the epic is done, paused, changed or approved again, or when the budget
is used up.

Some commands are never grantable, and no request is filed for them. The list is coarse and errs towards refusing:
anything the guard denies, orch's permission commands, starting the dashboard, the harness's own settings, hooks and
plugins, orch's config, state and ledger, the variables that decide where orch keeps its records, elevated rights,
merging pull requests, force pushes, deleting remote branches, sweeping recursive removals, a shell running a
substituted command, permission changes on orch's config dir, and any
command text outside printable ASCII or spanning several lines. Requests are shown with such characters escaped.

## Harness settings and auto mode

orch never writes into the harness's settings (D2 B). There is no *Always*: the hook answers every prompt itself.

A hook cannot overturn an **auto-mode classifier denial**: the harness does not consult a `PermissionRequest` hook
for an action its classifier has already denied. So the factory's agents file `orch permit request` after "Denied
by auto mode". Your grant then answers the next prompt the harness raises through the hook; if the classifier
denies the same action again, that is a new request each time. Making such an action pass the classifier is your
own settings change, never orch's. The test suite covers this path (`tests/test_factory.py`) without touching any
real settings.

## Coming in later phases

- Dashboard: the factory switch on the new-epic form, permission cards on Today and in Your move, standing grants
  with Revoke, and phone cards through the signed phone-decision flow.
- The Ready report (what is live, where to look, decisions made, what was not built) with your one-tap verdict, and
  the Stopped message (D3).
- The runner: `orch serve` keeping agents going in Mission Control's terminals (D4), waking a parked child after a
  grant.
- `factory.ask`: actions the harness would allow that you still want asked.

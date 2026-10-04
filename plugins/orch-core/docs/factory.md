# AI Factory (phases 1 to 3)

One epic in, finished work out: you write an epic and start it as a factory, and agents split, specify,
auto-approve and build its children. You hear from them when they need a permission they do not hold, and at the
end for the verdict. Issue #2 tracks the whole feature; this page describes what phases 1 to 3 ship.

AI Factory is **off by default**. Phase 1 works from the terminal; phase 2 adds the dashboard surface and phase 3 the
Ready report and the Stopped message, all described below. The runner follows in a later phase.

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

## On the dashboard (phase 2)

Only while `factory.enabled` is on; otherwise none of it is drawn and the routes refuse. Everything here is yours:
the dashboard's own cookie and same-origin checks apply, and orch refuses these answers to a process running inside
an agent harness, as it does for the terminal commands. Each is a signed ledger entry written by the same functions
as `orch permit ...`, with the dashboard's inline confirm (no popup), bound to the hash of the command the card shows.

- The human-only check runs in the dashboard process, as for approvals: keep the dashboard link and its cookie
  yours, because a grant lets a command run.
- **Start**: the epic's approval form has a "Start as an AI Factory" choice (25 children or 72 hours, size m or
  smaller); it is the same signed charter approval as `orch approve <epic> requirements --factory`.
- **Epic page**: a factory section with the state, children used out of the limit and hours left, the epic's open
  permission cards, its standing grants with Revoke, and the budget card once the budget is used up.
- **Permission cards** on Today and in the Board's Your move: the exact command (escaped), the reason, the epic and
  the asking ticket, with Grant once, Grant for this epic and Deny. They are not part of the decision count.
- **Board**: Group by "Factory epic" gathers each factory epic and its children; everything else sits under
  "Not in a factory".

## Ready and Stopped (phase 3)

Both are derived, read-only views of an epic whose signed charter is a factory one. Nothing in them approves, grants,
starts or signs anything, and nothing an agent can write turns one on or hides it: they come from the signed charter,
the signed ledger entries, the budget markers beside the ledger and the tickets' own state.

**Ready.** Every child is in testing or done, at least one is in testing, and every acceptance criterion of every
child in testing has evidence. The report lists, per child, how it was approved, its criteria count, its
Verification text (where to look), its links and what its Findings leave open, all as plain text. It has one action,
yours: **Accept the epic and close its children**. That is the epic verdict that already existed (`orch verdict <epic>
done`, or the epic page): it signs the epic's verdict hash, the hash of exactly the children and evidence the report
showed, and is refused if any of it changed since. The report never closes a child by itself, and an agent cannot
give the verdict (D1: the verdict stays whole).

**Stopped.** The factory is at a dead end the agents cannot leave on their own. The message names every reason that
holds, and what you can do about it; it has no action of its own. A reason is one of:

- the budget is used up (25 children or 72 hours, or what you signed);
- a child you sent back with a follow-up verdict three times is still not finished (counted from your signed verdicts);
- a request you denied still holds a child back (counted from your signed denials);
- the approval ledger on this machine was cut, so no approval or grant counts.

A Ready epic is never also Stopped. A child blocked in some other way, or one that failed its own checks, is not a
reason yet: those need the runner's records and come with it.

**Where you see them.** Today (an "AI Factory" section) and the Board's Your move strip show a card each; the epic
page shows them in its factory section. Both count as items waiting on you: they are in the Today headline, the
menu badge, the tab title and the session-start summary, and `orch wait <epic or child>` returns when the factory
becomes Ready or Stopped (event kind `factory.ready` or `factory.stopped`, actor `orch:factory`, derived and not
written to the event log). A Stopped card for a used-up budget replaces the budget card.

## Coming in later phases

- The factory switch on the new-epic form, and phone cards through the signed phone-decision flow.
- The runner: `orch serve` keeping agents going in Mission Control's terminals (D4), waking a parked child after a
  grant.
- `factory.ask`: actions the harness would allow that you still want asked.

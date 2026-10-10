# Proposal: factory full run (D61 option)

Status: **proposal, owner decision 10 Oct 2026 (evening)** — an extra option on the AI Factory (D61). Shown in the
mockup (AI Factory → Full runs). Event names and fields are provisional.

> "I put a ticket in (request), sign it, and the factory does plan, requirements, test, validate, evidence and even
> deploys (to dev or prod), whatever I define when creating the request."

## Words

The factory is not only for code, so the two last stages have general names:

- **Preview** — made and checked, visible only inside the workspace (a deploy to dev, a draft campaign, a report
  preview).
- **Deliver** — it goes out (deploy to production, publish the campaign, send the report to your boss).

## The request

One form, one signature:

| Field | What it says |
|---|---|
| Goal | What the factory should make, in your words. |
| How far may the factory go on its own? | **Up to Preview** (default) or **All the way to Deliver**. |
| What Deliver means | Required for Deliver. The concrete destination: "Deploy to production", "Publish campaign to the newsletter list", "Send to finance@example.test (boss)". |
| Hold window | 15 min, **30 min** (default), 1 h or 4 h: how long Deliver waits, with a notice and Stop, before it goes out. |
| Size cap | As the charter: children of size m or smaller; larger ones wait for you. |

Core's signing prompt names every value in core lines ("Goal (goal): …", "Goes up to (goes_up_to): Deliver",
"Deliver means (deliver_means): …", "Hold minutes (hold_minutes): 30", "Largest child (largest_child): m"), with each
value shown exactly (invisible characters made visible). The host records the signature (`addon.action_signed`).

## What the factory does, step by step

1. **Plan** — splits the goal into children within the size cap.
2. **Requirements** — writes and approves each child's requirements.
3. **Build and test** — agents do the work and run the checks.
4. **Validate** — the verdict on each child (on the signed commit, D53).
5. **Evidence** — collects what proves each acceptance criterion.
6. **Preview** — made and checked, visible inside the workspace. A run that stops at Preview ends here and waits for
   you.
7. **Deliver** (only if you chose it) — a calm notice "Delivering in 28 min · Stop" on the factory page, on Today (a
   needs-you item) and in the shell. When the hold window ends with no Stop, the host delivers to the destination in the
   request: "Delivered: Deploy to production at 14:32".

Every step the factory decides is labelled, on the run, the child and in History: **"via the factory full run you
signed on 10 Oct 2026 14:02 UTC — no person reviewed this step"** (or "… Severin signed …" for other viewers, or
"via mandate md_3, for Severin" when a mandate started the run).

## Hold with Stop

- When the run reaches Deliver the host writes `factory.deliver_held {run, deliver_means, until}` and opens a core
  decision with one option, **Stop delivery**.
- **Stop** during the window is signed by a person in core's prompt (`addon.decided`, presence Touch ID). The host
  cancels the delivery: `factory.deliver_stopped {run}`; nothing goes out, and the run stays at Preview.
- When the window ends without Stop: `factory.delivered {run, deliver_means, at}`.
- The hold is automatic: nobody has to approve it, so a run you forget about still goes out. The notice is the safeguard.

## Examples

| Request | Preview | Deliver |
|---|---|---|
| Code release: "Release monthly billing v2" | deployed to dev, checks green | "Deploy to production" |
| Marketing campaign: "Autumn tariff campaign" | the campaign as a draft | "Publish campaign to the newsletter list" |
| Finance report: "September cost report for my boss" | the report as a preview | "Send to finance@example.test (boss)" |

## How it relates to D61's charter

It is an **extra option**, not a replacement. The charter's limits stay: 25 children or 72 hours, children of size m
or smaller (larger ones wait for you), and **never the code review gate** — if that gate is on for a child, the run
waits for a person at that step. What changes: (1) the run continues after the verdicts into Evidence, Preview and,
if signed, Deliver; (2) the request names the Deliver destination and the hold window; (3) a delivery waits out the
hold with Stop. A run that goes "Up to Preview" behaves like today's charter plus an evidence step.

## Host enforcement

- The request signature covers the exact Deliver destination and hold window. The host refuses a Deliver to anything
  else (`factory.deliver_mismatch`); a changed destination needs a new signed request.
- The host executes Deliver itself; agents never hold the production, publishing or mail credential (concept-mandates
  §2.2).
- Stop during the hold cancels; after `factory.delivered` there is nothing to stop.
- A paused or stopped factory holds every run where it is; the hold clock does not run while paused.

## Mandates (owner decision, 10 Oct evening)

A mandate may start a full run with a Deliver target. Then a production deploy, publish or send can happen with no
person signing anything; the hold notice with Stop is the only human checkpoint, and the destination is fixed in the
signed request. The run is labelled "via mandate md_3, for Severin — no person reviewed this step".

## Open points for the owner

1. Who may request a run that goes to Deliver: owners only (the mockup's choice) or maintainers too?
2. Hold window choices (15 min / 30 min / 1 h / 4 h, default 30 min): right? A minimum the host enforces?
3. Should Stop also be possible from the phone (D49 allows stop and veto)? Recommended: yes.
4. Should a run that reached Preview but was stopped be re-deliverable with one new signature, or always a new request?
5. Should Deliver also wait while you are known to be away (no device online), or always go after the window?

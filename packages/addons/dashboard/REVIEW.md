# Review: orch Mission Control mockup

For the owner. State on 9 Oct 2026: iterations 1–3, the UX rounds and the owner's wave-3 input (terminal dock, epics,
tabs, widgets, drawers, quick ticket, Artifacts, Relay, Drop, Landing, Skills and Connections, motion, 13" notebook)
are in. Branch `feat/dashboard-mockup`. Everything runs on simulated data in the browser; nothing reaches a server.

## Open the preview

- Local dev server: http://127.0.0.1:5180/ (live; it may be stale for a minute while branches are merged)
- Stable snapshot: http://127.0.0.1:5181/ (refreshed after each tested merge; use this one for a calm review)
- On your own machine: `cd packages/addons/dashboard && npm install && npm run dev`, then open the address it prints.
- The hosted claude.ai preview (https://claude.ai/artifact/QvyFVD1JtFPyb3MegNXjgT) is frozen at commit 98151971,
  before permanent URLs; builds since then do not load there (your decision: the local setup is the target).

Things to know before you start:

- **Every page has a permanent address.** Copy it from the address bar, with **Copy link** (ticket header, Settings,
  addon pages) or ⌘K "Copy link to this page", and paste it into a new tab: the same page, workspace, ticket tab,
  filters or settings row opens. Workspace pages read `/w/DEMO/board`, `/w/INT/settings/members`; tickets stay
  `/ticket/DEMO-0043?tab=history`. Try: open `/ticket/DEMO-0043?tab=history` cold, reload a settings addon page
  (`/w/DEMO/settings/addon/publish`), paste `/w/INT/tickets` (switches to INT), `/w/NOPE/board` (says the workspace
  does not exist), and use Back/Forward across a workspace switch.
- Desktop only, 1024 px and wider. The target is a 13" notebook (1440×900) with the terminal docked on the right.
- Dark theme only (your decision).
- What you change is kept in this browser (localStorage) until you reset the demo.

## The review tour

The **Demo data** pill in the top bar has a **Review tour** button (it shows how many steps you ticked). It opens a
sheet with the scenarios below as checklists:

- **Go** sets up the step and opens its page: it switches the person you look as (Severin, Mara or Tom), the workspace
  (DEMO unless the step says otherwise), and — after asking, because it discards your demo changes — the demo data
  (Normal or Busy day). Without a person named, Go puts you back as Severin, so no step inherits the one before.
- Tick each step when you checked it. Ticks stay in this browser; the button and each scenario show the count.
- **Reset demo data and ticks** (bottom of the sheet) starts over: the demo data go back to the seed of the current
  dataset and every tick is cleared.

Also in the pill: **Normal / Busy day** (switch the dataset, asks first) and **Reset demo** (keeps your ticks). The
person you look as is at the bottom of the sidebar ("viewing as"); the workspace switcher is at the top.

Per step, look for: the page answers your next question without hunting; feedback within about 300 ms; no sideways
scrolling; a visible focus; no cut-off text without a tooltip; empty, loading and error states; orange only on addon
UI; one name per thing; calm on the Busy day.

## Scenarios

### 1. Owner morning

Severin starts the day: read, answer, approve, decide, move a card.

- [ ] **Read what needs you** — Today: the groups, the oldest and blocking items first, the Agents column. Page: `/`.
- [ ] **Answer a question on DEMO-0043** — Questions tab: pick an option, Send answer, sign. Page: `/ticket/DEMO-0043`.
- [ ] **Approve a plan gate** — Approvals group on Today: Review, read what you sign, Approve plan. Page: `/`.
- [ ] **Decide an addon decision** — From addons group: the failed build of Publish. Core asks you to sign. Page: `/`.
- [ ] **Move a card on the Board** — Drag a card or press m on it. Done is refused: only a verdict gets there. Page: `/board`.
- [ ] **Back to Today: the items are gone** — What you answered and approved has left the list; the count went down. Page: `/`.

### 2. New work

Create a bug, find it, start an agent and answer its question.

- [ ] **Create a bug** — Press c, choose Bug, fill Title and Requirements, Create. Page: `/tickets`.
- [ ] **Find it with ⌘K** — Type its key or a word of the title; Enter opens it.
- [ ] **Start an agent on it** — Actions → Start agent on the new ticket; core shows what starts, then Start agent.
- [ ] **Watch it work** — Within about 15 s it claims the ticket, finishes T1 and asks you a question. Page: `/agents`.
- [ ] **Answer from Today** — The question is at the top of Questions; answer it there. Page: `/`.

### 3. Verdict

Read the evidence of a ticket in testing and send it back.

- [ ] **Open a ticket in testing** — DEMO-0041 waits for a verdict. Page: `/ticket/DEMO-0041`.
- [ ] **Read the evidence** — Acceptance & tasks (evidence per criterion), Artifacts (log, CSV, code, screenshot). Page: `/ticket/DEMO-0041`.
- [ ] **Read the changes** — Changes: core's diff of the branch against develop. Give verdict says "Pass on <commit> · +A −D": the verdict signs that commit. Page: `/ticket/DEMO-0041`.
- [ ] **Look at widgets** — DEMO-0046 shows charts, a flow and an image compare in its text. Page: `/ticket/DEMO-0046`.
- [ ] **Fail the verdict with a reason** — Give verdict → Send back, write why. The ticket returns to In progress. Page: `/ticket/DEMO-0041`.

### 4. Maintainer (Mara)

The same workspace as Mara: what she may and may not do.

- [ ] **Today as Mara** — Her own queue; items waiting for an owner are named as such. Page: `/` as Mara.
- [ ] **Owner-only gates** — DEMO-0044: "Approve plan (owners only)" is disabled with the reason. Page: `/ticket/DEMO-0044` as Mara.
- [ ] **Settings are read-only** — Every control is off and says "Only owners can …". Page: `/settings/gates` as Mara.
- [ ] **Approve what she may** — Approvals and verdicts that maintainers may sign. Page: `/` as Mara.
- [ ] **Decide a factory permit** — AI Factory → the folded permits row above the tabs (Busy day: the factory is installed there). Page: `/addon/factory/factory` as Mara, Busy day.

### 5. Viewer (Tom)

Everything read-only, with reasons, and no dead ends.

- [ ] **Today as Tom** — One line per person who decides; opens into read-only rows. Page: `/` as Tom.
- [ ] **A ticket page** — Disabled controls say why ("Viewers cannot change tickets."). Page: `/ticket/DEMO-0043` as Tom.
- [ ] **Board and Tickets** — No New ticket button; c says viewers cannot create tickets. Page: `/board` as Tom.
- [ ] **Wiki and Guide** — Open pages and search; Edit page is off with the reason. Page: `/addon/wiki/pages` as Tom.
- [ ] **Activity filters** — Filters apply as you choose them (Busy day: Activity is installed there). Page: `/addon/activity/activity` as Tom, Busy day.
- [ ] **No error toasts from normal use** — Click around; nothing should answer with a red toast. Page: `/settings/members` as Tom.

### 6. Owner admin

Severin runs the workspace: people, gates, addons, grants.

- [ ] **Add a member** — Members → Add member: pick a person (Enter picks, a second Enter adds). Page: `/settings/members`.
- [ ] **Change a role** — The Role of a member; the last owner cannot be demoted. Page: `/settings/members`.
- [ ] **Change a gate policy** — Gates: count, approvers, "Affects N open tickets"; each change is signed. Page: `/settings/gates`.
- [ ] **Install quick tasks** — Addons → Browse addons → Quick tasks → Install → Grant and turn on (one signature). Page: `/settings/addons`.
- [ ] **Update GitHub** — The update shows what changes; one signature grants the new version. Page: `/settings/addons`.
- [ ] **Revoke an agent grant** — Agents → Grants → Revoke grant; the agent sessions on it stop. Page: `/agents`.
- [ ] **A member grants themselves** — In CLI Tom is a member: Agents → Issue grant… covers the tickets he may work on, at most 8 h. Page: `/agents` as Tom in CLI.

### 7. Addons tour

Every addon page, panel and Today card, each marked with the orange A.

- [ ] **Publish** — Start and stop an app (Undo), revoke a share, a show-once link (asks first, then "Copy this link now"). Page: `/addon/publish/shares`.
- [ ] **GitHub** — Review on GitHub, Approve; the issues lane on the Board imports an issue as a ticket. Page: `/addon/github/reviews`.
- [ ] **Usage** — Overview and By model; every number says its period. Page: `/addon/usage/overview`.
- [ ] **Wiki** — Open a page, edit it, link it to a ticket. Page: `/addon/wiki/pages`.
- [ ] **Terminals** — Run `orch status`, `git log`, arrow-up history; an agent mirror is read-only. Page: `/addon/terminals/sessions`.
- [ ] **Estimate** — Estimate a card in its ticket panel; the Board column shows the sum. Page: `/board`.
- [ ] **Worktrees** — Add a worktree, Remove is refused on a dirty one, Open terminal here. Page: `/addon/worktrees/worktrees`, Busy day.
- [ ] **Quick tasks** — Add, close with proof, make a ticket, the outgrew decision. Page: `/addon/quick/quick`, Busy day.
- [ ] **Records** — Record changes, push twice (rejected: pull first), pull, push. Page: `/addon/records/records`, Busy day.
- [ ] **Activity** — Filters and Show older. Page: `/addon/activity/activity`, Busy day.
- [ ] **Widgets in tickets** — DEMO-0043, DEMO-0046 and DEMO-0047 (the last one has refused blocks). Page: `/ticket/DEMO-0047`.
- [ ] **Start agent** — The start dialog shows the exact command; a model add-on, when one is on, names the model it picked. Page: `/addon/start-agent/start`, Busy day.
- [ ] **Guide** — Press ? on several pages: the help follows the page. Page: `/addon/guide/guide`.
- [ ] **AI Factory** — Run demo activity, a permit, Pause and Resume (signed). Page: `/addon/factory/factory`, Busy day.
- [ ] **Schedules** — Enable a schedule (signed), Run now, file the finding. Page: `/addon/schedules/schedules`, Busy day.

### 8. Workspace hopping

Switching workspaces keeps you on the page where that makes sense.

- [ ] **Switcher previews** — Open the workspace switcher: each workspace shows what needs you there. Page: `/`.
- [ ] **⌘2 on the Board** — The Board stays; it shows INT now. Page: `/board`.
- [ ] **Switch on a ticket page** — You land on Tickets with a toast naming the ticket's workspace. Page: `/ticket/DEMO-0043`.
- [ ] **Switch on an addon page** — Publish is not in INT: you land on Today with a toast. Page: `/addon/publish/shares`.

### 9. Keyboard only

Scenarios 1 and 2 without the mouse.

- [ ] **Today and the ticket page** — Tab, g b / g t / g a, Enter, Esc; the focus is always visible. Page: `/`.
- [ ] **Tickets with j and k** — j/k move, Enter opens. Page: `/tickets`.
- [ ] **⌘K and ?** — ⌘K finds tickets and commands; ? opens help.
- [ ] **New ticket with ⌘↵** — c, type, ⌘↵ creates. Page: `/tickets`.

### 10. Terminal dock

The terminal beside any page, bottom or right.

- [ ] **Open the dock** — Ctrl+` on a ticket page: sessions for this ticket. Page: `/ticket/DEMO-0043`.
- [ ] **Bottom, then right** — The menu moves it; the page keeps at least 720 px. Page: `/ticket/DEMO-0043`.
- [ ] **Watch an agent** — An agent session is read-only and ends on what it waits for.
- [ ] **Start your own Claude Code session** — New session, with and without the ticket context.
- [ ] **Continue an earlier session** — Sessions → Earlier → Continue from summary.
- [ ] **Collapse, reopen, resize** — Drag the edge or use the arrow keys on it.

### 11. Epics

Board and Tickets grouped by epic on a busy day.

- [ ] **Board grouped by epic** — Epic lanes first (a 40-child epic folded), No epic last, 5 cards per cell then "+N more". Page: `/board`, Busy day.
- [ ] **Tickets grouped by epic** — Epic rows with progress; children indented. Page: `/tickets`, Busy day.
- [ ] **Search a child** — A folded lane opens when its child matches. Page: `/board`, Busy day.
- [ ] **Drag within a lane** — Drop in another column of the same lane; the epic never changes by drag. Page: `/board`, Busy day.
- [ ] **Group: None** — Display → Group: None gives the flat board. Page: `/board`, Busy day.

### 12. Addon pages with tabs

Compact addon pages; what needs you sits above the tabs.

- [ ] **Publish** — Apps | Shares. Page: `/addon/publish/shares`.
- [ ] **Usage by model** — The By model total equals the Overview tokens. Page: `/addon/usage/overview`.
- [ ] **Wiki page view** — On this page, Linked from, Edit page and the unsaved-changes guard. Page: `/addon/wiki/pages`.
- [ ] **GitHub** — Pull requests | Issues; Recently merged folded. Page: `/addon/github/reviews`.
- [ ] **Records** — Pending | Activity; one headline that says the next step. Page: `/addon/records/records`, Busy day.
- [ ] **Schedules** — Schedules | Runs; the finding above the tabs. Page: `/addon/schedules/schedules`, Busy day.
- [ ] **AI Factory** — The folded “n permits waiting · Review” row above the tabs; expand it to answer. Page: `/addon/factory/factory`, Busy day.
- [ ] **Quick tasks** — Active | Completed. Page: `/addon/quick/quick`, Busy day.
- [ ] **Worktrees** — Filters, then Add worktree. Page: `/addon/worktrees/worktrees`, Busy day.

### 13. Widgets

Every widget type, and widgets inside tickets.

- [ ] **Widgets gallery** — Core types and templates, each with its source. Page: `/addon/widgets/widgets`.
- [ ] **DEMO-0043** — Stats, a series chart and checks. Page: `/ticket/DEMO-0043`.
- [ ] **DEMO-0045** — Timeline and diff in Current state. Page: `/ticket/DEMO-0045`.
- [ ] **DEMO-0046** — Flow diagram, callout, image compare; Expand one. Page: `/ticket/DEMO-0046`.
- [ ] **An error widget** — DEMO-0047: refused blocks say why in plain words. Page: `/ticket/DEMO-0047`.

### 14. New ticket

The overlay and the quick ticket.

- [ ] **Open the overlay** — Press c on any page. Page: `/tickets`.
- [ ] **Quick ticket** — Type one line, Enter: a backlog ticket; the toast has Open and Undo.
- [ ] **Dictate (simulated)** — The mic fills a sample text; nothing is recorded.
- [ ] **Open full page** — The draft goes with you.
- [ ] **Unsaved prompt** — Type, then Esc: "Discard unsaved changes?".

### 15. Settings

The other settings tabs.

- [ ] **Addon settings** — Addons → Settings on a row expands its form beneath the row; Save closes it. Page: `/settings/addons`.
- [ ] **Members** — The combobox; an unknown email is refused inline. Page: `/settings/members`.
- [ ] **Gates** — Policy and "Affects N open tickets". Page: `/settings/gates`.
- [ ] **Relay & devices** — Pair a device (simulated QR and code). Page: `/settings/relay`.
- [ ] **Skills** — Grant credentials to a skill (signed). Page: `/settings/skills`.
- [ ] **Connections** — Run check, Run doctor, the secrets file (names only). Page: `/settings/connections`.

### 16. Landing (D53)

The merge lane: approval, checks and merge bind to one candidate.

- [ ] **Landing page** — Queues | Needs | Activity. Page: `/addon/land/landing`.
- [ ] **A voided approval** — DEMO-0053: a conflict resolution voided the verify approval; back to review. Page: `/ticket/DEMO-0053`.
- [ ] **New commits void the verdict** — DEMO-0042 → Changes → "Simulate: the agent pushes a commit". Core voids the verdict; the ticket is back in testing. Page: `/ticket/DEMO-0042`.
- [ ] **The code review gate** — Gates → Code review: off by default; on for every ticket or by type. It follows the verdict on the same commit, never by an assignee. Page: `/settings/gates`.
- [ ] **A charter verdict** — DEMO-0051: "Verdict: via the factory charter — no person reviewed this". The AI Factory's Children tab names each verdict (Busy day). Page: `/ticket/DEMO-0051`, Busy day.
- [ ] **Resolve it yourself** — Today → From addons → "I will resolve it", then Mark resolved on the Landing page (Busy day). Page: `/`, Busy day.
- [ ] **Board chips** — Landing state on cards (landing, checking, conflict); no extra column (Busy day). Page: `/board`, Busy day.

### 17. Artifacts and Drop

All artifacts in one place; files shared through the relay.

- [ ] **Artifacts** — List and grid, filters, preview in the drawer. Page: `/artifacts`.
- [ ] **Install Drop** — Drop starts in the catalog: Browse addons → Drop → Install. Page: `/settings/addons`.
- [ ] **Claim a file** — Drop → Inbox → Claim. Page: `/addon/drop/drop`.
- [ ] **Share a file** — Share a file; a link is shown once. Page: `/addon/drop/drop`.

### 18. Blocked by a connection

A ticket that needs a login that expired.

- [ ] **Blocked tickets** — DEMO-0053 (databricks-prod auth expired) and DEMO-0054 (gcloud-billing wrong identity). Page: `/ticket/DEMO-0054`.
- [ ] **Start agent is refused** — Actions → Start agent is off with the reason. Page: `/ticket/DEMO-0053`.
- [ ] **Re-login from Today** — Connections group: copy the login hint, Run check again (Demo). Page: `/`.
- [ ] **Unblocked** — After Run check again for gcloud-billing, DEMO-0054 no longer says Blocked and Start agent works. Page: `/ticket/DEMO-0054`.

### 19. Mandates (preview)

How the wide mandate (owner decision 10 Oct evening) would look. A non-functional preview: nothing signs.

- [ ] **Preflight** — Agents → Mandates: the four prerequisites are "not available in this build", so issuing is blocked. Page: `/agents?tab=mandates`.
- [ ] **Issue a mandate** — Show the mandate anyway (preview), then Issue a mandate…: "Steers the whole workspace in your name", what it may do (approve waiting agents and gates including code review, land on main (owners may since 11 Oct; never more than its owner), first send to a linked workspace, publish publicly, restore/purge, unblock tickets, enable factories and start their runs including Deliver, issue grants), what stays yours (settings, addons, members and roles, devices, relay pairing, secrets and connections, protected paths, another mandate), a length up to 30 days; "Sign mandate (preview — nothing is signed)". Page: `/agents?tab=mandates`.
- [ ] **Renew** — Renew… in the mandate: pick a new length (up to 30 days) from now; revision 2, same scope, no total renewal ceiling. Weekly check-in due on the named day; Still OK is preview-only. After 3 unconfirmed days it pauses, not revokes. Page: `/agents?tab=mandates`.
- [ ] **The banner** — One calm line on every page: Mandate md_3 · for Severin · whole workspace · 10 decisions · until Tue. Details opens the tab; × hides it for this session. Page: `/board`.
- [ ] **Decided for you** — Today, below the real queue: one folded row. Open it for up to 3 decisions (a grant, a factory run, a permit … with Looks right, Veto), the refused items (protected path, your veto, an addon install and a second mandate, both refused) and Show all; "Revoke mandate and void" sits in its header. Page: `/`.
- [ ] **Stop** — Stop in the banner (optionally also stop agents): "Stopping…", then "Stopped at #1842". Page: `/agents?tab=mandates`.
- [ ] **Revoke and void** — Lists the 8 decisions it would void and the 1 on landed work it only lists for review. Page: `/agents?tab=mandates`.
- [ ] **Off again** — Demo data → Preview: mandates (or Reset demo): Today and the shell look exactly as before. Page: `/`.

Quickest way in: **Demo data → Preview: mandates** turns the preview on with mandate md_3 already in force (seeded as if it had run for three days). Off by default; Reset demo turns it off.

### 20. Factory full run

Owner decision 10 Oct evening (D61 option): one signed request, the factory goes to Preview or all the way to Deliver, with a hold window and Stop.

- [ ] **A run on hold** — Demo data → Busy day, then AI Factory: "Delivering in 28 min · Publish campaign to the newsletter list" above the tabs with Stop delivery, the same line in the shell, and a needs-you item on Today. Page: `/addon/factory/factory`.
- [ ] **Stop the delivery** — Stop in the shell line (or Stop delivery on the page): core's prompt names the run, the destination and the end of the window; Send answer. "Stopped by Severin …: nothing went out." Full runs now offers Deliver now… and Deliver with a new hold…: choose, then sign the run, destination and new hold; no work is redone. Page: `/addon/factory/factory`.
- [ ] **Request a full run** — Full runs tab: Goal, "How far may the factory go on its own?" → All the way to Deliver, What Deliver means (required), Hold window (0 no hold / 15 min default / 30 min / 1 h / 4 h / 24 h) → Review request → Sign and start (or Fill in a demo request). The prompt lists every value in core lines; zero says “No hold: delivers as soon as its checks pass”. Page: `/addon/factory/factory`.
- [ ] **Watch the steps** — Plan, then Requirements / Build and test / Validate / Evidence per child, then Preview: each reads "via the factory full run you signed on … — no person reviewed this step". About 30 s later it holds before Deliver. Page: `/addon/factory/factory`.
- [ ] **Delivered** — Simulate: let the hold time pass (demo) ends the hold now: "Delivered: Deploy to production at …". The epic's History has the run's milestones. Page: `/addon/factory/factory`.

In the normal demo, install AI Factory (Settings → Addons → Browse addons), then Full runs → Fill in a demo request → Sign and start: it reaches the hold in about 30 s. The mock clock runs in real time, so the default 15-minute hold really takes 15 minutes; Simulate: let the hold time pass (demo) ends it now. Proposal: `docs/factory-full-run-proposal.md`.

## What is simulated

Nothing here talks to a real system. Each simulation says so where you meet it (a "Simulated" or "Demo" chip).

| Thing | In the mockup |
|---|---|
| Touch ID and signatures | Every human signature shows core's sign dialog with what you sign; "Touch ID" is a 600 ms pause. Signatures, hashes and key fingerprints are made up. No OS prompt. |
| Terminals (PTY) | A scripted fake shell in xterm.js. `orch status`, `orch show`, `orch wait`, `git status`, `git log`, `ls`, `help` and a few more answer from the demo state; nothing runs on your machine. |
| Claude Code and Codex in the dock | Scripted CLI screens. Your own session answers each prompt with a short canned reply marked simulated; agent sessions are replayed transcripts that end on the ticket's real blocker. |
| Agent runs | "Start agent" plays a script on a timer: claim after 1.5 s, a task lease, a log line, T1 done, then a question to you. The Busy day adds a live script (an event every 4–8 s). AI Factory "Run demo activity" and the Landing "Run the worker (demo)" are scripts too. |
| GitHub | Pull requests and issues live in the addon's demo state. "Review on GitHub" opens a github.com address; approve and import change only the demo. |
| Relay and devices | No network. Link states, pairing codes, the QR (it encodes nothing) and the sync queue are simulated; owners get buttons to simulate a dropped link or a phone scanning. |
| Drop and Publish | Uploads are sample files; apps "start" and "stop" in the demo state; share links are made up and shown once. |
| Records | A pretend git remote: the second push without a pull is rejected on purpose. |
| Connections and the secrets file | Checks return seeded results (one expired login, one wrong identity, one service down). "Run check again" on Today assumes you logged in again. Secret values never appear; masked output reads `•••• (NAME)`. |
| Dictation | Always simulated: the microphone is never used; Stop fills in a sample sentence. |
| Time | The demo clock starts at Friday 9 Oct 2026, 11:30 UTC when the demo loads or resets, then runs in real time. "5 min ago" is measured against that clock, not your computer's date. |
| Usage and costs | Generated numbers (last 7 days pinned to CHF 31.40). |
| Mandates (preview) | A non-functional preview of a proposed feature (`docs/concept-mandates.md`, Step 1 pilot). Nothing is signed and no Touch ID runs; the preflight honestly reports that none of the four prerequisites exists in this build. The decision log is seeded; Stop's acknowledgement is a 1.5 s pause. Its state is kept apart from the demo's logs (browser key `orch.preview.mandates`). |

## Known polish items

Round R4 is built and merged: ticket page density (first 2 widgets, then "N more"; one Terminal panel; addon panels
in one section), the prototype widget caption, widgets gallery details, breadcrumb from where you came, Usage tiles
with one period each, Size vs Points, Publish vs Drop wording, and a richer Busy day (every widget type, every
artifact kind, Usage that scales).

Known and left for later:

- The addon settings drawer resets your unsaved edits if someone else saves the same settings meanwhile; it needs an
  "Updated elsewhere — Reload / Keep mine" design.
- In a very narrow terminal the simulated Claude welcome box is cut with "…" (terminal content, allowed to clip).

## Decided (owner, 10 Oct 2026)

The open questions of the first review, with the owner's answers. Each is applied in the mockup and logged in
DECISIONS-LOG.md ("Owner decisions 2026-10-10").

1. **Landing (D53): the verdict signs the commit; an opt-in code review gate.** The verify gate's hash includes the
   branch head (`source_sha`); the verdict dialog says "Pass on <commit> · +A −D" and its covers name the commit. Any
   new commit after the verdict voids it (core: `gate.invalidated`, "New commits after the verdict: <sha>") and the
   ticket goes back to Testing. The land worker uses the signed commit; a clean rebase keeps the approval, a conflict
   voids it. Testing shows the diff (Changes tab) next to the evidence. The `code` gate is off by default; on per
   workspace or per ticket type (Settings → Gates), it follows the verdict, signs the same commit, is never an
   assignee's, and landing needs it.
2. **Widgets.** `timeline`, `progress` and the multi-series `series` stay, marked "Proposed — not in orch.widgets.v1
   yet"; the aliases `ok` and `note` are gone (one name per thing). Proposal for widgets.md:
   `docs/widgets-v1-proposal.md`.
3. **Relay/Drop: strict D54.** Drop is orch-only; no download page for outsiders. Revisit later.
4. **Connections: as built.**
5. **Agent grants: members may grant themselves** — their own signature, the tickets they may work on, at most the
   workspace default (8 h); owners revoke any grant; viewers still cannot.
6. **AI Factory charter approves everything, verdicts included** ("Verdict: via the factory charter — no person
   reviewed this", on the ticket and the factory page). With the code review gate on for factory tickets, that review
   stays a person's (Settings → Gates explains how).
7. **AI Factory "Watch live": as built** (an explicit button).
8. **Schedules "Run now": as built.** The real host checks `spawn_agent` and the person's grant before any run that
   starts an agent (HANDOVER).

Calls made for you (each logged in DECISIONS-LOG.md with how to revert):

- Board: epic lanes first, "No epic" last, at most 5 cards per cell then "+N more"; Group by epic is the default.
- Dense addon items (board card chips, table cells) show the addon once per surface, not an A on every chip.
- Quick ticket: Enter creates at once (at least two words), with Undo on the toast; dictation is always simulated.
- Addon install is one signed step ("Grant and turn on"); an update is one signature showing the diff.
- An unknown email in Add member is refused ("inviting by email comes later").
- Usage has no date-range picker; every number names its period.
- Any member may revoke a Drop link; only the sender or an owner may extend one.
- The terminal dock docks right only when the page keeps at least 720 px; beside the dock the sidebar becomes the rail.
- The sidebar shows at most 6 addon pages; the pin choice is kept per browser (a stand-in for a workspace setting).
- Addon settings open in a right-hand drawer (you asked for "a drawer below"; a right drawer fits forms better).

## Repos preview (U3)

Open `/w/DEMO/addon/repos/repos` as Severin. Structure shows the workspace folder, "Clones as" (the `gh` connection,
the bot account) and the seven repos of `settings.repos` plus the untracked `sandbox`. Expand rows for remote, path,
branches, ahead/behind, changes, fetch, size, worktrees and linked tickets; `acme-energy-dbt` has no remote (the
format's own shape). `web-portal` links DEMO-0046; follow "Tickets linking web-portal" and the ticket panel's link back.

Clone billing-api: core's cover shows the full URL, the target folder and the git login. Wait six seconds for queued →
cloning → present. In Busy day, Clone all missing includes private-api: it fails once, then Retry works. Fetch keeps
dirty work. Checks → Check now; Activity records who and when.

Declare a repo (owners only) → a fake HTTPS or SSH remote → Review repo → Sign and declare: core's
`settings.changed` appears in the workspace log. Try a credential-bearing remote, `git@-oProxyCommand=x:y`, `..` or an
existing folder. Declare `sandbox`. Remove shared-lib (files stay); web-portal needs "Remove anyway". As Mara
(maintainer) the declaration controls are gone; as Tom (viewer) only reading. Today shows "5 of 7" and a Clone
decision for billing-api. Settings → Repos: interval, fetch on check, git login. No git or network commands run.
## Decided (owner, 10 Oct 2026, evening)

D58, D59, D60 confirmed. Two changes, both shown in the mockup and logged in DECISIONS-LOG.md ("Owner decisions
2026-10-10 (evening)"):

- **D61, factory full run** (scenario 20): one signed request; the factory goes up to Preview (made and checked,
  visible only in the workspace) or all the way to Deliver (it goes out), with a hold window and Stop. Proposal:
  `docs/factory-full-run-proposal.md`.
- **D62, wide mandate** (scenario 19): it steers the whole workspace in your name except settings, addons, members
  and roles, devices, relay pairing, secrets and connections and protected paths; up to 30 days, renewable with one
  new signature; it may start full runs that Deliver; it never issues another mandate. Concept:
  `docs/concept-mandates.md` (open points in §6).

## P2 M0 server review (2026-10-11)

Use [server/README.md](server/README.md) to start a dev-marked P1 stub and the local launcher.
Check the dark read-only workspace switcher, Today, Tickets and Identity; unavailable host
features must disappear, and no mock/demo controls or signing UI should appear. Confirm that
launch strips the URL fragment, a second tab joins, logout clears both, and a restart requires
another launch. Run the existing layout guard on the default Vite mock app. Real-browser
origin enforcement and S1/S2 need the dev-kit/real host; stub tests are not issue acceptance.
## Owner answers, 11 Oct morning (U8)

- Mandates preview: issue, inspect code-review approval and the allowed land-on-main decision, then “Still OK” beside the weekly due day.
  Nothing signs. Renewals have no total ceiling; 3 unconfirmed days pauses instead of revoking.
- Factory Full runs: hold choices 0 (no hold), 15 min (default), 30 min, 1 h, 4 h, 24 h. Inspect the zero-hold signing
  cover, then stop a positive hold and try Deliver now… / Deliver with a new hold…; sign the new delivery without
  redoing work. Only owners can do this. Phone push + Face ID Stop belongs to orch-mobile.
- Busy day factory: expand “n permits waiting · Review”; the same permits still wait on Today.
- Repos: declare an SSH URL with literal git user; another user/password is refused. No primary flag;
  remote/default_branch remain proposed for the format amendment (owner agreed 11 Oct).
- Verify names the last gate; addon log tabs say Activity; the ticket tab still says History. Confirmation-only
  buttons have no ellipsis; input dialogs retain it.

D33 follow-up: as Severin, set Landing → Allowed targets to `main, develop`, open an approved unlanded
ticket and Add to the landing queue. Check Covers shows ticket, target `main`, and approved commit,
then Sign and run; run the demo worker. As Mara or a member main enqueue is refused. For red checks
on main, take it off the queue and sign again. Other targets keep their existing roles.

## Try P2 M1 live updates (dev stub, own terminal)

Use a disposable P1 workspace that already contains a workspace-visible ticket, prepared
with the existing dev-kit or P1 CLI. All terminals use the same marked state and workspace.
Build once from this checkout; the Vite mock remains independent and does not use SSE.
The following example uses short real paths, not symlink aliases. On macOS `/tmp` is a symlink to
`/private/tmp`, so the example spells out `/private/tmp` (on Linux `/tmp/orch-m1` is already real; `~/.orch-dev-run`
also works if it is a real directory and the socket path stays under 100 bytes). Never point these at `~/.config/orch`.
A workspace that was not created by `orch init` also needs its workspace signing key (WSK) under
`$ORCH_STATE_DIR/hosts/<workspace-id>/keys`; without it `orch log` is read-only and cannot append.

```sh
# Set these in every terminal; substitute your disposable workspace and person id.
export ORCH_DEV=1
export ORCH_WORKSPACE=/private/tmp/orch-m1/workspace
export ORCH_STATE_DIR=/private/tmp/orch-m1/state
export ORCH_HOST_RUN_DIR=/private/tmp/orch-m1/state/run
mkdir -p "$ORCH_STATE_DIR"
chmod 700 "$ORCH_STATE_DIR"
touch "$ORCH_STATE_DIR/.orch-dev" "$ORCH_WORKSPACE/.orch-dev"

# From the checkout root, install/grant the dashboard package in the disposable workspace.
mkdir -p "$ORCH_WORKSPACE/.orch/addons/dashboard"
cp packages/addons/dashboard/orch-addon.json "$ORCH_WORKSPACE/.orch/addons/dashboard/orch-addon.json"
cd packages/addons/dashboard/server
uv sync --locked
uv run orch addon grant dashboard
cd ..
export PATH=/Users/severin/.nvm/versions/node/v24.21.0/bin:$PATH
npm run build
server/.venv/bin/python -m devhost "$ORCH_WORKSPACE" --person p_YOUR_PERSON_ID
```

In terminal 2, export the same four variables, then from `packages/addons/dashboard/server`:

```sh
uv run orch-dashboard open /w/DEMO/tickets
# If browser confinement blocks the redirect file:
uv run orch-dashboard open /w/DEMO/tickets --print-url
```

In terminal 3, export the same variables, then from `packages/addons/dashboard/server` append a real
P1 unattended log note (replace `DEMO-0001` with a workspace-visible ticket's key):

```sh
unset ORCH_GRANT
export ORCH_SESSION=s_01J9ZP0000000000000000000S
uv run orch log 'M1 live-update check' --ref DEMO-0001
```

The stub polls the log within 200 ms and emits an event item plus needs invalidation; the
Tickets table refetches and its updated ordering changes where applicable. In browser DevTools,
inspect `/api/events?ws=…` for `ready`, then `items`; inspect the follow-up ticket request.
Open two tabs, disconnect one via DevTools Offline, append another note, restore networking:
it should reconnect with its own Last-Event-ID and resync. Three failed streams switch that
tab to the 10-second head poll. Log out in one tab: both tabs sharing the token must clear,
and the server streams must close. Stop the launcher with `uv run orch-dashboard --stop`.

Visibility/gone and slow-client bounds have automated signed-store/in-memory coverage. For
real-host acceptance, run S3/S4 with a second member, hide a ticket from that member while its
view is open, restart the host during a stream, and verify no queued ticket metadata leaks.
Run browser layout/cross-origin tests in Chromium, Firefox and WebKit with real listeners.
These live-port acceptance steps were not run in the implementation sandbox.

## Try P2 M2 reads (dev stub, own terminal)

Read-only: Today, Board, Tickets, a ticket page (Overview, Acceptance, Changes, Questions, Artifacts, History,
Raw) and live updates, from a disposable P1 workspace through the dev stub. Follow the M1 section above for the
environment and the stub, but give the workspace a ticket with a branch (the Changes tab needs a linked repo whose
branch the host observed) and open the dashboard on `/w/DEMO` after `npm run build`. Nothing signs: no Review,
Answer, Give verdict, New ticket, Settings, Artifacts or Agents appears; typing `/w/DEMO/settings/general`
leads to the board.

Look for: Today rows with "Answer in your terminal" and the question text read from the ticket; the board in
its five columns (no Waiting column content: waiting is derived); filters on the Tickets list (status, label,
type, search); the ticket page's log as events with the host's one-line text and "checked by the host"; the
Changes tab per file, and a refusal sentence when the branch moved after the host observed it (commit on the
branch in the dev repo: the diff is refused until the host observes the new head); fields the host does not
serve are named in Raw. Append a log note in a terminal (`orch log ... --ref DEMO-0001`): the open ticket page
updates within a second. Hide a ticket from this person (visibility change) while its page is open: the page
closes with a toast and the board no longer lists it.

### M2 on host contract v1.4 (what changed to try)

The dev stub now serves the v1.4 read shapes and lists `read_ext/1` and `activity/1`. Also try it once with
`ORCH_DEV_READ_EXT=0` in the stub's environment: the same pages must show no progress bar, lock or blocked marker
and Raw lists what is unknown (nothing reads as zero).

Look for: Today's "Recently" (ticket events only, no member or device items); question rows with their age; a
need on a closed ticket marked "ticket closed"; a gate nobody can approve as a notice row, not a verdict row.
Close a ticket in the workspace (`orch close DEMO-0004 --resolution duplicate --duplicate-of DEMO-0001`): its page
opens (no error) with a Closed notice, `DEMO-0001` as plain text; the Tickets list shows it only under the
Closed chip. A task leased by an agent reads as "doing". History › Changes lists who edited a section and the hash
(no diff). A long log note (over 1 KiB) reads "(shortened by the host)", with no "…" and no "more" button. The
browser console shows no CSP error (sonner's styles are in the built CSS).

## Try P2 M3 actions via the terminal hand-off (dev stub, own terminals)

1. Prepare a disposable dev workspace whose person key is in the passphrase backend (`<state>/hosts/<ws>/person`), the dashboard addon granted, `.orch-dev` in workspace and state, and the WSK under `<state>/hosts/<ws>/keys`. Start the stub and the server as in `server/README.md` and open a ticket with a waiting gate.
2. Press "Approve requirements": the dialog shows the command `orch sign XXXX-XXXX`, a fingerprint, an expiry countdown and a preview from the host.
3. In a terminal with the same `ORCH_DEV=1 ORCH_STATE_DIR=...` run `python -m devhost sign XXXX-XXXX` (from `packages/addons/dashboard`). Compare the fingerprint, type the ticket key, then the passphrase. The dialog turns to "Signed".
4. Try Cancel, waiting past 10 minutes (Expired), approving twice (Already done), changing the ticket while the code is open (the warning), Comment/Add label/Close/Reopen from the command palette, and New ticket (title and type). Move, Ask and the quick ticket are absent.

## Try P2 M4 settings and the switcher (dev stub, own terminals)

1. Start the stub with two workspaces that share the prefix DEMO (two disposable dev workspaces, each with its own person):
   `python -m devhost WS1 WS2 --person p_one --person p_two` (same `ORCH_DEV=1`, `ORCH_STATE_DIR`, `.orch-dev` rules as before), then
   `uv run python -m orch_dashboard open`.
2. Open `/w/DEMO`: the page asks which workspace. Pick one; the address gains `?ws=<8 hex>`. Open the switcher: both rows show their short id.
3. Settings: change the grant length (hand-off; sign with `python -m devhost sign CODE` or `python -m orch sign CODE`: the real signer refuses
   `settings.set` until R5). Add a member from two files (a public key and a device certificate), change a role, revoke a grant, edit the code
   review policy (the assignee and independence boxes are locked).
4. With the stub's `ORCH_DEV_SETTINGS=0` the settings show "This host does not say..." and no value.
5. `touch <run>/<uuid>/stopped` takes a workspace down: the switcher shows "Host not running", the other keeps working; remove the file to
   bring it back. Open a hand-off and switch workspace (Back): it is cancelled.


## Try the dashboard on a REAL host (RH)

1. From `packages/addons/dashboard`: `npm run build`, then `cd server && uv sync --locked`. Make a disposable demo with the real CLI and host:
   `.venv/bin/python tests/realhost_kit.py up ~/orch-rh-demo` (creates a workspace with `orch init` in a pty, seeds tickets in every status,
   starts `orch host run --foreground`; about 20 s; state and HOME stay under that folder, never `~/.config/orch`).
2. `.venv/bin/python tests/realhost_kit.py open ~/orch-rh-demo` prints a single-use link (30 s): open it. Walk Today, Board, Tickets, a ticket
   (Overview, Acceptance and tasks, Artifacts with digests, History), Settings and the switcher.
3. Press a hand-off (for example "Approve requirements" on the "Audit log" ticket). The dialog shows `orch sign XXXX-XXXX`. In a terminal with
   `ORCH_STATE_DIR=~/orch-rh-demo/state HOME=~/orch-rh-demo/home XDG_CONFIG_HOME=~/orch-rh-demo/home/.config ORCH_DEV=1`, run
   `.venv/bin/python -m orch sign XXXX-XXXX` (passphrase `orange-pelican-vault-3917-demo`, type the key shown). The dialog turns to "Signed".
4. The verify gate needs someone other than the worker: with one person it reads "no eligible approver". Set the verify policy first
   (`orch sign` signs `policy.set`; the browser cannot prepare it until stream A fixes RH-3, see HANDOVER "P2 on the real host").
5. `.venv/bin/python tests/realhost_kit.py down ~/orch-rh-demo` stops the host by its PID and the dashboard server.
Automatic checks: `uv run pytest -m realhost -q` (server) and the Playwright scenarios in orch-dev-kit `e2e/dashboard/`.

## Try click-to-sign (M5, real host, own terminal)

On a disposable workspace (never your real `~/.config/orch`), with the real signer. On macOS the convenience key lives in the **login
keychain**; to avoid that use a Linux machine or the tests (`tests/test_realhost_click.py`, which use the 0600 file store of the throwaway
state). Steps: bring up the demo (`tests/realhost_kit.py up BASE`), then in your terminal with the demo's environment
`orch host convenience on --workspace DEMO` and `orch keys custody convenience --workspace DEMO` (asks the passphrase once), restart
`python -m orch_dashboard`, open the dashboard. You should see: Settings, General shows a "Signing" card with the risk line; any action
dialog (Approve, Comment, Move) offers **Sign** and **Sign in the terminal instead**. Sign opens the signer's review (risk line first, the
terminal review lines, fingerprint, expiry); Sign turns on after half a second and again after you switch windows; a policy or member
change asks you to type the word shown. Try "Sign in the terminal instead" (it cancels the click request and shows an `orch sign` code), and
`orch host convenience off --workspace DEMO` (the buttons disappear on the next refresh). Details: HANDOVER "Click-to-sign (H5c)".

### T3 security regression checks (#444 / #447 I-6)

Run the targeted dashboard HTTP, click and auth pytest files, plus bootstrap and foreign
navigation vitests. Foreign shell loads must be 200 with the marker (including empty,
duplicate and unknown site values); absent/none/same-origin loads remain untagged.
The launcher's actual cross-site navigate/document headers plus its fragment code must
produce a session through a subsequent same-origin auth exchange. A foreign PoC tab with
no code has no server session and cannot join the token channel, even with inherited storage
or token offers. Check the terminal/bookmark guidance. Untagged bootstrap behavior remains
covered. Signing regressions cover uncertain-then-definite retries and non-dict status child
cleanup. These checks use in-process HTTP, jsdom and fake signers: no ports, browser or keychain.

## Try quick tasks and records (M6, real host, own terminal)

On a disposable workspace (`tests/realhost_kit.py up BASE`; never your real `~/.config/orch`). In the demo workspace folder with the demo's
environment run `orch quick add "Fix the typo"` twice and `orch records init`, then restart `python -m orch_dashboard` and open the dashboard.
You should see: "Quick tasks" in the sidebar listing both tasks with "counts unknown"; Limits (kept in this browser) and Add, which says
"two signatures" and signs the new task, then its label. Settings, Records shows what is waiting to commit; **Commit now** commits locally and
shows the commit. The four terminal-only commands appear as text with no button. With a remote (use a local bare repo only) the export warning
shows verbatim before **Commit and push**. `orch runner enable` (terminal) makes Settings, General show the "Dashboard execution" line.

M6 follow-up (2026-10-14): In the disposable workspace, open Commit and push and check the current destination and private assurance. Change the local remote URL, reopen, and check the mismatch against the last push. Layout fixtures `/__layout/records-push-failed` and `/__layout/records-push-mismatch` exercise long current URLs. Convert a ticket with only `quick`: the dialog says “Labels kept: none”, and the signed set removes its last label.

## Try terminals (M7a, real host, own terminal)

The end-to-end check is in-process (a subprocess host admits only the runner unit's main PID, and `orch runner enable` installs a
launchd/systemd unit, which must not happen on this shared laptop): `cd packages/addons/dashboard/server && uv run pytest -m realhost
tests/test_realhost_terminals.py` (core's host and runner in this process, a real `/bin/sh`). On a disposable real host
(`tests/realhost_kit.py up BASE`) with the `terminals` addon granted but no runner, the dock shows (feature `terminals`) and
**New terminal** shows the host's preview with its `blocked` reason; nothing starts. With a runner, what you should see: the preview
(command, folder, "acts as you, not as an agent", the D-P2-31 line), **Open terminal** opens a shell you can type in; another tab shows
it read-only with **Take control**; taking it there shows **Take back** here; **Stop** ends it ("Some processes may still be running"
when cleanup was incomplete); an ended run whose output is gone says "Output no longer kept". Layout: `/__layout/terminal-dock` (and
`-preview`, `-readonly`, `-take`, `-exited`, `-stop`) in the dev build.

## Try start agent (M7b, real host, own terminal)

In-process end to end (core's host, runner and click signer, a fake `codex`): `cd packages/addons/dashboard/server && uv run pytest -m realhost
tests/test_realhost_agents.py`. On a disposable real host with `start-agent` granted, dashboard execution on and a runner: the dock's
**New terminal → Start agent on KEY** (or the ticket's **Actions → Start agent**) opens core's dialog. What you should see: the D-P2-31
line; the host's preview (command, "A worktree for KEY in REPO will be created at …" or "the repository's shared working copy (not a
separate worktree)", env names, warnings); without a grant the **Runner grant** card with the owner's risk sentence, hours (1–12),
tickets (workable/all) and **Issue runner grant** (off, with "Turn on dashboard execution (orch runner enable) or use the terminal",
while execution is off). Issuing shows the signer's View with the deposit line and the word `PREFIX~WS8/GRANT`; after Sign: "Your runner
holds the grant until …" and the preview no longer blocks; **Start agent** adds the run to the dock. With a contract-1 core the card
says "This orch version can't hand a grant to the runner; update orch." **Forget runner grant** asks first. Layout:
`/__layout/agent-dialog` (and `-blocked`, `-grant`, `-outcomes`) in the dev build.

## Try pairing (fixtures)

Nothing reaches a host: the `pair/1` ops (contract v1.9) are agreed but not served yet. On a real host Settings → **Devices** says "Pairing a phone is not available on
this host yet." To see the page, run `npm run dev` and open the dev-only scenarios:

- `/__layout/pair-unavailable`: the not-available line.
- `/__layout/pair-blocked-convenience`: `sign.convenience_readers` (flag on) with the host's message and "Turn click-to-sign off first: orch
  host convenience off --workspace DEMO". Click-to-sign history alone is only a warning, never a block.
- `/__layout/pair-offer`: the warning about click-to-sign, **Create pairing link** → waiting with a countdown and nothing secret on screen; **Show QR and link**
  shows the QR (scan it: `orch://pair?relay=…#v2.…` with demo values), **Copy link** (the native link; cleared from the clipboard when the
  link is dropped) and the https fallback as selectable text (not a link); **Cancel pairing**.
- `/__layout/pair-pending`: type the phone's code. Reject has the focus; Approve stays off until the code matches (`K7Q2MA`; `k7q-2ma`
  works too); a different code says "The codes differ: reject this phone." and counts an attempt: after 3 wrong codes the phone is refused.
- `/__layout/pair-cert`: two terminal steps with copyable commands (primary approval with the cert code; `orch sign <CODE>`). `/__layout/pair-failed`: the reason and "remove the device".
- `/__layout/pair-devices`: the list from `member.list` (the Mac itself is not listed; a two-line label; an empty one shows "phone");
  **Remove this device** opens the CLI command as text (removal is CLI only this round).

## Try Drop (fixtures)

Nothing reaches a host: the `drop/1` ops are proposed, not agreed. On a real host `/w/<PREFIX>/drop` says "Drop is not available on this
host yet." (no sidebar entry until the server lists `drop`). To see the page, run `npm run dev` and open the dev-only scenarios:

- `/__layout/drop-unavailable`: the not-available line.
- `/__layout/drop-list`: Received / Sent tabs, 8 per page (**Older** / **Newer**). Rows show name, kind and type, size, author (person,
  device, "from a phone" or a "sealed by an agent" mark), created, expiry and state. Pruned, Expired and Deleted rows and the Unclaimed
  one have Download off with the reason. Expired and Deleted rows also show “Disappears from this list on” with the removal date. **Send a file** (or drop a file on the dashed box): over 25 MiB is refused before anything is
  uploaded; otherwise progress in bytes and percent, then "Sealing and sending…", then "Sent …" and the file shows under Sent.
  **Download** saves a file (the fixture's content is HTML with a script: it must arrive as a download, never open). **Delete** asks
  "Delete this file for everyone?" (removed for everyone at once); **Delete for everyone** turns the row into a Deleted tombstone. Delete sends the listed version and head; a `drop.version_conflict` keeps the dialog open, explains that the item changed, and refreshes the list (fixture option `retireConflict: true`).
- `/__layout/drop-long-names`: very long names and types, `<img src=x onerror=alert(1)>.html` as text, a right-to-left override shown as
  `\u{202e}`, a newline and a NUL shown escaped, a name that can't be shown, a long person name and device id.
- `/__layout/drop-upload-progress`: an upload stalled at 1.5 MiB of 3 MiB (50 %); **Cancel** says nothing was sent.
- `/__layout/drop-errors`: every error text with its code (too large, three quotas, version cap, version conflict, expired, pruned,
  unavailable, not visible, busy with retry time); sending or downloading there fails with the workspace quota.
- `/__layout/drop-versions`: a document at 1024 of 1024 versions (cap line, pruned versions' downloads off, 12 per page) and "New versions
  of a document are saved from a phone. This host sends files only." Open **Versions** on meeting-notes.md for 37 of 1024.


## Try reader levels and click-to-sign refusals (core H12d-2)

Fixtures only (`npm run dev`):

- `/__layout/readers-devices`: Settings → Devices. "Reader levels" lists ten devices by label and member (never by id); four are
  below the required level 1 and say "Needs an update of the orch app" and come first; the line above says why level 1 is
  required. "Work phone / second line" shows its line break as text. Pairing above still says it is not available.
- `/__layout/convenience-refusals`: every reason of `sign.convenience_unavailable` and `sign.convenience_readers`, an unknown
  reason (the general sentence, the reason never shown), `reader.decl_invalid`, and the signer's `readers_old`. The devices each
  names appear by label ("Work phone…", "a device of Mara with no name", "a device this page does not know").

On a real host: Settings → General's Signing card says why click-to-sign is paused (`readers_old`) with **See which devices need
an update**; Settings → Devices shows the levels when the host serves `reader/1`.

B10 phone approvals (phase 1): on a host with phone/1, pair a phone and allow Phone in workspace sign methods. Set How I approve to Phone, open a requirements/plan approval or request-changes dialog, and press Approve on your phone. Review/choose on the phone; the dashboard shows the ten-minute countdown and eventual Approved / Changes requested result. Try Cancel, Other ways, generation changes, expiry and an in-flight decision after cancellation. Code and verdict keep the disabled coming-soon method. The demo has no paired phone; component tests supply the host responses.
## B9 setup wizard (16 Oct 2026)

Open through `orch setup` after the parallel core branch is integrated. `/setup#launch=<single-use code>` uses the setup process, not mock workspace storage. A normal dashboard launch token is intentionally refused. In development, `/setup` uses the same API; tests provide an in-process fetch fake and a real private Unix socket contract fake.

Review Welcome → You → Agents may → Approval → Secret words → Review, then progress and Done. Check One click disables relay with its reason, recovery words form a numbered four-column/six-row grid, Print includes only recovery words, and confirmed words disappear. Check three-day runner access is one plain click and needs the matching terminal fingerprint. Existing workspaces say “Finish setup” and skip secret screens. Failures show per-step status and a “Finish setup later” path.

### B9 core-aligned setup (2026-10-16)

From a supported installed checkout, run `orch setup` in a new project folder. The browser opens plain `/setup`; the server consumes the private setup.code file and claims its listening port. A second tab cannot claim again. Review GRANT for runner access or AGENTS for extended access, terminal confirmation, and waiting/failure text. Finish navigates to `/` as core currently specifies; a normal dashboard session is not created by setup. Automated coverage includes the real core socket boundary and the UI contract.

### Wiki read server (#603)

With Wiki enabled, use a workspace repository containing committed `wiki/hello.md`. Read the Wiki list, page,
search and backlinks endpoints as a viewer. Stage a different page body and confirm the committed body still
appears. Turn Wiki off and confirm all four endpoints refuse. Compare backlinks as two people with different
restricted-ticket visibility. The mock UI remains unchanged; frontend integration is a separate task.
Server regression coverage is in `server/tests/test_wiki.py`; the driver runs it outside the restricted HOME.

### Manual acceptance (2026-10-19)

As a ticket owner/reviewer or workspace owner/maintainer on a live host, open Acceptance: accept a criterion with an optional note, review the host statement, then sign. Withdraw requires a reason. An earlier-wording acceptance offers Accept again. Mark done explains that no criterion is proved and warns when it ends an agent lease. Verify review lists current manual acceptance and stale wording. Check that viewers and unrelated members have no controls, progress follows host coverage, and History names acceptance/withdrawal plainly.

Tests were added for the lead to run; this task does not run Vitest or pytest.

## Try the hub pages (V3 A7)

Open `/w/DEMO/settings/hub?hub=selective` (also `standalone`, `full`, `offline`, `nottold`): the chip in the top bar, the three Hub pages, "Choose what to share" with its live preview, the switch to full access (type "full access"), Leave, and the Terminal step with its copy box. Tickets shows the Hub column in selective mode only. Nothing is signed in the page.

### D2 review (2026-10-28)

Create a ticket with “Create and start”. With missing/short access, verify the signer headline, ticket fields, eight-hour grant terms and inline deposit risk; waiting alone must never sign, and the second press must wait one second. With valid access of at least 15 minutes, verify no grant step or access copy outside “i”. Interrupt signing after creation: the draft remains and start is refused. Retry a lost launch response: the same request returns the same run.

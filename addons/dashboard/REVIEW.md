# Review: orch Mission Control mockup

For the owner. State on 9 Oct 2026: iterations 1–3, the UX rounds and the owner's wave-3 input (terminal dock, epics,
tabs, widgets, drawers, quick ticket, Artifacts, Relay, Drop, Landing, Skills and Connections, motion, 13" notebook)
are in. Branch `feat/dashboard-mockup`. Everything runs on simulated data in the browser; nothing reaches a server.

## Open the preview

- Local dev server: http://127.0.0.1:5180/ (live; it may be stale for a minute while branches are merged)
- Stable snapshot: http://127.0.0.1:5181/ (refreshed after each tested merge; use this one for a calm review)
- On your own machine: `cd addons/dashboard && npm install && npm run dev`, then open the address it prints.
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
- [ ] **Start an agent on it** — Actions → Start agent… on the new ticket; core shows what starts, then Start agent.
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
- [ ] **Decide a factory permit** — AI Factory → the permission request above the tabs (Busy day: the factory is installed there). Page: `/addon/factory/factory` as Mara, Busy day.

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
- [ ] **Start agent and model routing** — The model line in the start dialog; an invalid model name in Model routing blocks starts. Page: `/addon/start-agent/start`, Busy day.
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
- [ ] **Records** — Pending | History; one headline that says the next step. Page: `/addon/records/records`, Busy day.
- [ ] **Schedules** — Schedules | Runs; the finding above the tabs. Page: `/addon/schedules/schedules`, Busy day.
- [ ] **AI Factory** — The permit above the tabs. Page: `/addon/factory/factory`, Busy day.
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

- [ ] **Landing page** — Queues | Needs | History. Page: `/addon/land/landing`.
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
- [ ] **Start agent is refused** — Actions → Start agent… is off with the reason. Page: `/ticket/DEMO-0053`.
- [ ] **Re-login from Today** — Connections group: copy the login hint, Run check again (Demo). Page: `/`.
- [ ] **Unblocked** — After Run check again for gcloud-billing, DEMO-0054 no longer says Blocked and Start agent works. Page: `/ticket/DEMO-0054`.

### 19. Mandates (preview)

How the approved Step 1 pilot would look. A non-functional preview: nothing signs.

- [ ] **Preflight** — Agents → Mandates: the four prerequisites are "not available in this build", so issuing is blocked. Page: `/agents?tab=mandates`.
- [ ] **Issue a pilot mandate** — Show the pilot anyway (preview), then Issue a pilot mandate…: the fixed scope, the never list and the protected paths; "Sign mandate (preview — nothing is signed)". Page: `/agents?tab=mandates`.
- [ ] **The banner** — One calm line on every page: Mandate md_3 · for Severin · epic DEMO-0050 · 9 decisions · until Tue. Details opens the tab; × hides it for this session. Page: `/board`.
- [ ] **Decided for you** — Today, below the real queue: one folded row. Open it for up to 3 decisions (Looks right, Veto), the refused or skipped items and Show all; "Revoke mandate and void…" sits in its header. Page: `/`.
- [ ] **Stop** — Stop in the banner (optionally also stop agents): "Stopping…", then "Stopped at #1842". Page: `/agents?tab=mandates`.
- [ ] **Revoke and void** — Lists the 7 decisions it would void and the 2 on landed work it only lists for review. Page: `/agents?tab=mandates`.
- [ ] **Off again** — Demo data → Preview: mandates (or Reset demo): Today and the shell look exactly as before. Page: `/`.

Quickest way in: **Demo data → Preview: mandates** turns the preview on with mandate md_3 already in force (seeded as if it had run for three days). Off by default; Reset demo turns it off.

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
dirty work. Checks → Check now; Activity log records who and when.

Declare a repo (owners only) → a fake HTTPS or SSH remote → Review repo → Sign and declare: core's
`settings.changed` appears in the workspace log. Try a credential-bearing remote, `git@-oProxyCommand=x:y`, `..` or an
existing folder. Declare `sandbox`. Remove shared-lib (files stay); web-portal needs "Remove anyway". As Mara
(maintainer) the declaration controls are gone; as Tom (viewer) only reading. Today shows "5 of 7" and a Clone
decision for billing-api. Settings → Repos: interval, fetch on check, git login. No git or network commands run.
